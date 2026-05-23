"""OneMeter BLE framing: build / parse 16-byte encrypted frames.

Plaintext layout for a single 16-byte frame:
    [cmd:1] [body: 13] [CRC16-CCITT-FALSE LE: 2]

Three response shapes:

1. **Empty ACK** — `byte[1] == 0x00 AND byte[2] == 0x00`.
   Used for cmd 0xAA / 0x13 / 0x23 login confirmations. No payload.

2. **Multi-fragment header + data** — `byte[1] == 0x00 AND byte[2] > 0`.
   Used for cmd 0x21 / 0x36 / 0x82 / 0x87 read probes.
     - Header frame layout: `[cmd][0x00][total_len][11 B zero pad][CRC]`,
       decrypted with the **constant IV**.
     - Data frame layout:   `[cmd][13 B chunk][CRC]`,
       decrypted with **CFB-128 chaining**: each data frame's IV is the
       previous frame's *ciphertext* (frame N's IV = ct of frame N-1,
       and frame 1's IV = ct of the header frame).
     - Reassembly: concatenate `pt[1:14]` (13-byte slices) from each data
       frame, then trim to `total_len` bytes.

3. **Direct single-frag response** — `byte[1] != 0x00`.
   Used for cmd 0x18 battery. Layout: `[cmd][payload bytes][zero pad][CRC]`,
   decrypted with the **constant IV**. The decoder for each specific cmd
   knows the expected payload length; the reassembler returns the entire
   13-byte body slice [1:14] and lets the decoder slice further.

REQUESTS always use the constant IV.
"""
from __future__ import annotations

from dataclasses import dataclass

from .cipher import OneMeterCipher
from .commands import assert_allowed
from .crc import crc16_ccitt_false

FRAME_LEN = 16
BODY_LEN = 14
PAYLOAD_MAX = 13  # 1 cmd byte + 13 payload + 2 CRC = 16


class FrameError(ValueError):
    """Raised on malformed frames (length, CRC, etc.)."""


def build_plain_frame(cmd: int, payload: bytes) -> bytes:
    """Construct the 16-byte plaintext frame for `cmd` + `payload`."""
    assert_allowed(cmd)
    if len(payload) > PAYLOAD_MAX:
        raise ValueError(f"payload too long: {len(payload)} > {PAYLOAD_MAX}")
    body = bytes([cmd]) + payload + b"\x00" * (PAYLOAD_MAX - len(payload))
    crc = crc16_ccitt_false(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def encrypt_request(cipher: OneMeterCipher, cmd: int, payload: bytes) -> bytes:
    """Build + encrypt a request frame using the constant IV."""
    pt = build_plain_frame(cmd, payload)
    return cipher.apply_constant_iv(pt)


def check_crc(plain_frame: bytes) -> bool:
    """Validate the 2-byte LE CRC trailer of a 16-byte plaintext frame."""
    if len(plain_frame) != FRAME_LEN:
        return False
    expected = crc16_ccitt_false(plain_frame[:BODY_LEN])
    got = plain_frame[BODY_LEN] | (plain_frame[BODY_LEN + 1] << 8)
    return expected == got


# --- Multi-fragment reassembly ----------------------------------------------

@dataclass
class Reassembled:
    """Final result of a complete response.

    For multi-fragment responses, `payload` is the concatenated and trimmed
    data. For direct single-frag responses (cmd 0x18 etc), `payload` is the
    body slice `[1:14]` (13 bytes) — the decoder for that cmd knows how to
    slice further.

    `is_ack` is True iff this was an empty ACK (length 0). In that case
    `payload` is empty.
    """

    cmd: int
    payload: bytes
    is_ack: bool


@dataclass
class Rejected:
    """Plaintext frame whose cmd byte is 0xFF (the canned auth-error)."""

    length: int
    raw: bytes


class Reassembler:
    """Stateful response reassembler.

    Feed encrypted 16-byte frames in order. Returns a Reassembled when a
    response is complete, Rejected for 0xFF auth-error frames, or None
    when more frames are needed to complete a multi-fragment response.

    Raises ``FrameError`` on malformed input (length, bad CRC).
    """

    def __init__(self, cipher: OneMeterCipher) -> None:
        self._cipher = cipher
        self._reset()

    def _reset(self) -> None:
        self._cmd: int | None = None
        self._buf = bytearray()
        # When set, multi-fragment in progress: this is the total expected
        # payload length (from header byte[2]) and the previous frame's
        # ciphertext used as IV for the NEXT data frame.
        self._expected_len: int | None = None
        self._prev_ct: bytes | None = None

    def feed(self, ct_frame: bytes) -> Reassembled | Rejected | None:
        """Feed one encrypted 16-byte frame from the device."""
        if len(ct_frame) != FRAME_LEN:
            raise FrameError(f"frame length {len(ct_frame)} != {FRAME_LEN}")

        if self._prev_ct is None:
            # First frame of any response (or after a previous response
            # completed): constant IV.
            pt = self._cipher.apply_constant_iv(ct_frame)
        else:
            # Data frame of a multi-fragment response: chained IV.
            pt = self._cipher.apply_chained(ct_frame, self._prev_ct)

        if not check_crc(pt):
            prev_cmd = self._cmd
            self._reset()
            raise FrameError(
                f"CRC check failed on frame {pt.hex()} (prev_cmd={prev_cmd!r})"
            )

        cmd = pt[0]

        # Canned auth-error.
        if cmd == 0xFF:
            raw = bytes(pt)
            length = pt[1]
            self._reset()
            return Rejected(length=length, raw=raw)

        # Multi-fragment data frame continuing an in-progress reassembly.
        if self._expected_len is not None:
            assert self._cmd is not None
            if cmd != self._cmd:
                prev_cmd = self._cmd
                self._reset()
                raise FrameError(
                    f"cmd byte changed mid-stream: {prev_cmd:#x} -> {cmd:#x}"
                )
            # Each data frame contributes 13 bytes of payload (bytes [1:14]).
            self._buf.extend(pt[1:14])
            self._prev_ct = bytes(ct_frame)
            if len(self._buf) >= self._expected_len:
                payload = bytes(self._buf[: self._expected_len])
                self._reset()
                return Reassembled(cmd=cmd, payload=payload, is_ack=False)
            return None

        # First frame of a new response.
        b1 = pt[1]
        b2 = pt[2]

        if b1 == 0x00 and b2 == 0x00:
            # Empty ACK: cmd ack with no payload.
            self._reset()
            return Reassembled(cmd=cmd, payload=b"", is_ack=True)

        if b1 == 0x00 and b2 > 0:
            # Multi-fragment header: byte[2] is total_len. Data frames follow.
            self._cmd = cmd
            self._expected_len = b2
            self._buf.clear()
            self._prev_ct = bytes(ct_frame)
            return None

        # Direct single-frag response (e.g. cmd 0x18 battery). The full
        # 13-byte body slice is returned; the cmd-specific decoder slices
        # further based on its known payload length.
        self._reset()
        return Reassembled(cmd=cmd, payload=bytes(pt[1:14]), is_ack=False)
