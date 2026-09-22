#!/usr/bin/env python3
"""Extract a OneMeter device's mobKey + IV from its nRF51822 flash via SWD.

The OneMeter device stores its per-device AES key and IV in protected
flash. This script reads them via OpenOCD + the nRF51 code-readout-
protection (CRP) bypass gadget, then prints the values to paste into the
HA integration's config flow.

# WARNING — UNTESTED

This script has been derived from analysis but **has not yet been run
end-to-end against a live OneMeter device.** The flash offsets, the
bypass-gadget PC, and the gadget register mapping below match the
specific firmware we analysed; other firmware revisions may differ.

If it doesn't work for your device, see the troubleshooting section in
`EXTRACTING_CREDENTIALS.md` — in particular, the manual "find the load
instruction" step from the upstream nrf51-extractor README is the
fallback.

References:
- https://github.com/grappeq/nrf51-extractor  (general-purpose extractor)
- https://www.pentestpartners.com/security-blog/nrf51822-code-readout-protection-bypass-a-how-to/

Usage:
    1. Connect a SWD programmer (CMSIS-DAP, ST-Link, J-Link, etc.) to the device.
    2. Start OpenOCD listening on its telnet port 4444. Example:
         openocd -f interface/cmsis-dap.cfg \\
                 -c "transport select swd" \\
                 -c "adapter speed 5" \\
                 -c "reset_config none" \\
                 -f target/nrf51.cfg
    3. Run this script (no arguments needed in the common case).

The script halts the CPU and reads these values:
  - FICR DEVICEADDR (BLE MAC) — to confirm you're talking to the right device
  - mobKey  (16 bytes at flash 0x3F024)  — required by the integration
  - IV      (16 bytes at flash 0x3F034)  — required by the integration
  - the broadcast key/IV pair (0x3F044 / 0x3F054) — optional; only needed for
    the integration's passive-reading feature, and a device without it still
    works normally over GATT
…and prints them.

After a gadget read the script deliberately leaves the CPU **halted**: the
bypass gadget writes PC without saving the application's execution point, so
`resume` would continue from the gadget (or from wherever the last `step` left
it) and run garbage — which is how this tool used to fault devices. Power-cycle
the board afterwards, which you need anyway: an SWD halt also stops it
advertising over BLE until then.

It does not write to the device. It does not modify flash. It is purely
a read-out tool.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4444
SOCK_TIMEOUT_S = 5.0
PROMPT = b"> "

# nRF51822 flash offsets (per our analysed firmware)
FLASH_MOBKEY_OFFSET = 0x0003F024  # 16 bytes — AES-128 key
FLASH_IV_OFFSET     = 0x0003F034  # 16 bytes — initial cleartext / IV
# The identity block is stored TWICE, this far apart (three 1 KB flash pages).
# The firmware reads the primary; the second copy is a redundant record that is
# NOT kept in sync — a device whose identity was ever rewritten by writing the
# primary alone (e.g. after being reflashed with another unit's firmware and then
# re-personalised) still carries the *donor's* credentials here. So this offset
# is only ever used for a cross-check, never as a source of truth.
FLASH_IDENTITY_COPY_DELTA = 0x00000C00
# The advertisement broadcast (passive reading) uses a second AES-CCM key pair,
# stored immediately after mobKey/IV in the same configuration page. It is
# unrelated to the GATT session cipher above; the integration only needs it for
# the optional passive-reading feature, and a device without it still works
# exactly as before over GATT.
FLASH_PASSIVE_KEY_OFFSET = 0x0003F044  # 16 bytes — broadcast AES-128 key
FLASH_PASSIVE_IV_OFFSET  = 0x0003F054  # 16 bytes — broadcast IV / CCM nonce

# FICR (factory information, unprotected — readable via plain mdw)
FICR_DEVICEADDRTYPE = 0x100000A0  # 4 bytes
FICR_DEVICEADDR     = 0x100000A4  # 6 bytes (low 48 bits of two consecutive words)
FICR_DEVICEID       = 0x10000060  # 8 bytes (two words)

# Default CRP-bypass gadget. These match the OneMeter firmware we analysed.
# If your device has a different firmware revision, see the upstream
# nrf51-extractor README for how to find the right values manually.
DEFAULT_GADGET_PC = 0x6D4
DEFAULT_GADGET_REG = "r4"   # the script loads target addr into this reg and
                            # reads the loaded value back from the same reg

# Number of times to read each word and require agreement before accepting
VERIFY_READS = 2
# Retries per word before giving up
MAX_RETRIES = 3
# Pause between read attempts. `step` resumes the core for one instruction and
# the halt that ends it lands asynchronously, so retrying instantly just re-hits
# the same window.
RETRY_SETTLE_S = 0.2
# How long to wait for that halt to land after a `step`, and how often to ask.
WAIT_HALT_TIMEOUT_S = 2.0
WAIT_HALT_POLL_S = 0.05

# Set the moment a gadget read writes PC, and never cleared. The gadget saves no
# context, so from then on the core's original execution point is gone and the
# device must not be resumed — the cleanup path reads this to decide between
# resuming and leaving the target halted with an explanation.
_pc_hijacked = False


class OpenOCDError(RuntimeError):
    pass


def identity_backup_matches(
    primary: tuple[bytes, bytes], backup: tuple[bytes | None, bytes | None]
) -> bool:
    """Whether the redundant copy of the identity block agrees with the primary.

    True when the backup could not be read — an unreadable copy is not evidence
    of a mismatch, and warning about it would train the operator to ignore the
    warning. False means the device's identity was rewritten without updating the
    backup, so the backup holds the *previous* credentials. Pure, so the rule is
    testable without a device (tests/test_extract_gadget.py).
    """
    if backup[0] is None or backup[1] is None:
        return True
    return backup == primary


def _should_resume(*, pc_hijacked: bool, no_resume_flag: bool) -> bool:
    """Whether the cleanup path is allowed to resume the CPU.

    Only when a gadget read never took over PC. Resuming a hijacked core
    continues from the gadget (or wherever the last `step` left it) rather than
    from the application, which runs garbage. `--no-resume` is kept as an
    accepted legacy no-op now that leaving the target halted is unconditional.

    Pure and split out so the rule can be tested without a debugger — it is the
    rule whose absence used to fault devices (see tests/test_extract_gadget.py).
    """
    return not pc_hijacked and not no_resume_flag


def _connect(host: str, port: int) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=SOCK_TIMEOUT_S)
    sock.settimeout(SOCK_TIMEOUT_S)
    _drain_until_prompt(sock)
    return sock


def _drain_until_prompt(sock: socket.socket) -> bytes:
    """Read until the openocd prompt appears."""
    buf = b""
    deadline = time.time() + SOCK_TIMEOUT_S
    while PROMPT not in buf:
        if time.time() > deadline:
            raise OpenOCDError(
                f"timed out waiting for openocd prompt; got {buf[-200:]!r}"
            )
        try:
            chunk = sock.recv(4096)
        except socket.timeout as exc:
            raise OpenOCDError("socket timeout reading openocd output") from exc
        if not chunk:
            raise OpenOCDError("openocd connection closed unexpectedly")
        buf += chunk
    return buf


def _tncmd(sock: socket.socket, cmd: str) -> str:
    sock.sendall((cmd + "\n").encode("ascii"))
    return _drain_until_prompt(sock).decode("ascii", errors="strict")


_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")


def read_word_direct(sock: socket.socket, addr: int) -> int:
    """Read a word via openocd's mdw — works for FICR/UICR/RAM, not protected flash."""
    resp = _tncmd(sock, f"mdw 0x{addr:08x} 1")
    # Format: '0xADDR: VVVVVVVV '
    m = re.search(r"0x[0-9a-fA-F]+:\s+([0-9a-fA-F]+)", resp)
    if not m:
        raise OpenOCDError(f"unexpected mdw response for {addr:#010x}: {resp!r}")
    return int(m.group(1), 16)


def _target_is_halted(targets_output: str) -> bool:
    """True when openocd's `targets` listing reports a halted target.

    The listing is a header row plus one row per target, whose last field is the
    state word; anything else (an error reply, or the header alone) is not a
    halt. Split out as a pure function so it can be unit-tested without a device
    — see tests/test_extract_gadget.py.
    """
    for line in targets_output.splitlines():
        fields = line.split()
        if fields and fields[-1] == "halted":
            return True
    return False


def _wait_halted(sock: socket.socket) -> None:
    """Block until openocd reports the target halted.

    `step` resumes the core for a single instruction and the halt that ends it
    lands asynchronously. A register read issued straight afterwards can catch
    the target still running, and openocd then answers "Could not read register
    'r4'" — which fails the whole extraction. This is exactly why the manual
    workaround (typing the same commands by hand) worked while the script did
    not: a human leaves about a second between commands, a socket leaves
    microseconds.
    """
    deadline = time.time() + WAIT_HALT_TIMEOUT_S
    while True:
        state = _tncmd(sock, "targets")
        if _target_is_halted(state):
            return
        if time.time() >= deadline:
            raise OpenOCDError(
                f"target did not halt within {WAIT_HALT_TIMEOUT_S:.1f}s of `step` "
                f"(last state: {state.strip()!r})"
            )
        time.sleep(WAIT_HALT_POLL_S)


def read_word_via_gadget(
    sock: socket.socket, addr: int, gadget_pc: int, gadget_reg: str
) -> int:
    """Read a word from a CRP-protected flash address via the bypass gadget.

    The gadget is a single instruction in unprotected flash that does
    `LDR {reg}, [{reg}, #0]; BX LR`. Setting PC to the gadget and the
    register to the target address, then single-stepping, leaks one
    word into the same register.

    This call DOES NOT preserve the CPU's prior PC/register state, and nothing
    restores it afterwards — see `_pc_hijacked`. Only safe to call when the CPU
    is halted at reset / known-idle.
    """
    global _pc_hijacked
    # Set before the write so a failure part-way through is covered too: from
    # this point the core must not be resumed.
    _pc_hijacked = True
    _tncmd(sock, f"reg pc 0x{gadget_pc:x}")
    _tncmd(sock, f"reg {gadget_reg} 0x{addr:x}")
    _tncmd(sock, "step")
    _wait_halted(sock)
    resp = _tncmd(sock, f"reg {gadget_reg}")
    matches = _HEX_RE.findall(resp)
    if len(matches) != 1:
        raise OpenOCDError(
            f"expected one hex value reading {gadget_reg}, got {matches} from {resp!r}"
        )
    return int(matches[0], 16)


def read_word_verified(
    sock: socket.socket, addr: int, gadget_pc: int, gadget_reg: str
) -> int:
    """Read a word VERIFY_READS times and require agreement; retry up to MAX_RETRIES.

    A failure inside an attempt is retried rather than propagated: one transient
    refusal used to abort the whole 16-byte block, and then the entire run.
    """
    last_reads: list[int] = []
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(RETRY_SETTLE_S)
        try:
            reads = [
                read_word_via_gadget(sock, addr, gadget_pc, gadget_reg)
                for _ in range(VERIFY_READS)
            ]
        except OpenOCDError as exc:
            last_error = exc
            continue
        if len(set(reads)) == 1:
            return reads[0]
        last_reads = reads
    raise OpenOCDError(
        f"could not get {VERIFY_READS} agreeing reads at 0x{addr:08x} after "
        f"{MAX_RETRIES} attempts (last reads: {last_reads}, last error: {last_error})"
    )


def read_block_via_gadget(
    sock: socket.socket,
    base: int,
    length: int,
    gadget_pc: int,
    gadget_reg: str,
) -> bytes:
    if length % 4 != 0:
        raise ValueError("length must be a multiple of 4")
    out = bytearray()
    for off in range(0, length, 4):
        w = read_word_verified(sock, base + off, gadget_pc, gadget_reg)
        out += w.to_bytes(4, "little")
    return bytes(out)


def read_ble_mac(sock: socket.socket) -> str:
    """Read DEVICEADDR / DEVICEADDRTYPE from FICR. Returns the standard XX:XX:XX:XX:XX:XX form."""
    lo = read_word_direct(sock, FICR_DEVICEADDR)
    hi = read_word_direct(sock, FICR_DEVICEADDR + 4) & 0xFFFF
    raw = lo | (hi << 32)
    return ":".join(f"{(raw >> (8 * i)) & 0xFF:02X}" for i in range(5, -1, -1))


def read_deviceid(sock: socket.socket) -> str:
    a = read_word_direct(sock, FICR_DEVICEID)
    b = read_word_direct(sock, FICR_DEVICEID + 4)
    return f"{a:08X}{b:08X}"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Extract OneMeter mobKey + IV from device flash via OpenOCD + SWD."
    )
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"OpenOCD telnet host (default: {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"OpenOCD telnet port (default: {DEFAULT_PORT})")
    p.add_argument("--gadget-pc", type=lambda x: int(x, 0), default=DEFAULT_GADGET_PC,
                   help=f"PC value of the CRP-bypass gadget "
                        f"(default: 0x{DEFAULT_GADGET_PC:x}). See "
                        f"EXTRACTING_CREDENTIALS.md if reads fail.")
    p.add_argument("--gadget-reg", default=DEFAULT_GADGET_REG,
                   help=f"Register used by the gadget (default: {DEFAULT_GADGET_REG})")
    p.add_argument("--mobkey-offset", type=lambda x: int(x, 0), default=FLASH_MOBKEY_OFFSET,
                   help=f"Flash offset of mobKey (default: 0x{FLASH_MOBKEY_OFFSET:x})")
    p.add_argument("--iv-offset", type=lambda x: int(x, 0), default=FLASH_IV_OFFSET,
                   help=f"Flash offset of IV (default: 0x{FLASH_IV_OFFSET:x})")
    p.add_argument("--passive-key-offset", type=lambda x: int(x, 0),
                   default=FLASH_PASSIVE_KEY_OFFSET,
                   help="Flash offset of the broadcast key "
                        f"(default: 0x{FLASH_PASSIVE_KEY_OFFSET:x})")
    p.add_argument("--passive-iv-offset", type=lambda x: int(x, 0),
                   default=FLASH_PASSIVE_IV_OFFSET,
                   help="Flash offset of the broadcast IV "
                        f"(default: 0x{FLASH_PASSIVE_IV_OFFSET:x})")
    p.add_argument("--json", action="store_true",
                   help="Emit the result as a single JSON object on stdout instead of text.")
    p.add_argument("--output", type=Path, default=None,
                   help="Write JSON output to this path in addition to printing.")
    p.add_argument("--skip-reset", action="store_true",
                   help="Don't issue 'reset halt' first. Use if the device is already halted.")
    p.add_argument("--no-resume", action="store_true",
                   help="Accepted for backwards compatibility. The CPU is now always "
                        "left halted after a gadget read, because resuming from a "
                        "hijacked PC is unsafe (see the module docstring).")
    args = p.parse_args()

    print(f"connecting to OpenOCD at {args.host}:{args.port}…", file=sys.stderr)
    try:
        sock = _connect(args.host, args.port)
    except (ConnectionError, socket.error) as exc:
        print(f"ERROR: cannot connect to OpenOCD: {exc}", file=sys.stderr)
        print("Is `openocd` running and listening on the telnet port?", file=sys.stderr)
        return 2

    try:
        if not args.skip_reset:
            print("halting target via SWD (reset halt)…", file=sys.stderr)
            _tncmd(sock, "reset halt")
        else:
            _tncmd(sock, "halt")

        # FICR first (unprotected — sanity check we're talking to the chip)
        print("reading FICR (BLE MAC + DEVICEID)…", file=sys.stderr)
        try:
            ble_mac = read_ble_mac(sock)
            device_id = read_deviceid(sock)
        except OpenOCDError as exc:
            print(f"ERROR reading FICR: {exc}", file=sys.stderr)
            print("If this fails, your SWD connection isn't wired right — fix that "
                  "before worrying about the bypass gadget.", file=sys.stderr)
            return 3

        # Bypass-gadget reads of mobKey + IV
        print(f"reading mobKey (16 B @ 0x{args.mobkey_offset:08x}) via gadget at "
              f"PC=0x{args.gadget_pc:x}, reg={args.gadget_reg}…", file=sys.stderr)
        try:
            mobkey = read_block_via_gadget(
                sock, args.mobkey_offset, 16, args.gadget_pc, args.gadget_reg
            )
        except OpenOCDError as exc:
            print(f"ERROR: bypass-gadget read failed: {exc}", file=sys.stderr)
            print("Most likely the gadget PC or register is wrong for your firmware.",
                  file=sys.stderr)
            print("See EXTRACTING_CREDENTIALS.md → 'Finding the bypass gadget manually'.",
                  file=sys.stderr)
            return 4

        print(f"reading IV (16 B @ 0x{args.iv_offset:08x})…", file=sys.stderr)
        iv = read_block_via_gadget(
            sock, args.iv_offset, 16, args.gadget_pc, args.gadget_reg
        )

        # The broadcast pair lives in the same page. Read it with the same
        # gadget, but treat a failure as non-fatal: the GATT credentials above
        # are what the integration fundamentally needs, and a device whose
        # broadcast pair is unprogrammed simply can't do passive reading.
        print(f"reading broadcast key/IV (16 B each @ 0x{args.passive_key_offset:08x} "
              f"/ 0x{args.passive_iv_offset:08x})…", file=sys.stderr)
        try:
            passive_key = read_block_via_gadget(
                sock, args.passive_key_offset, 16, args.gadget_pc, args.gadget_reg
            )
            passive_iv = read_block_via_gadget(
                sock, args.passive_iv_offset, 16, args.gadget_pc, args.gadget_reg
            )
        except OpenOCDError as exc:
            print(f"WARNING: broadcast key/IV read failed ({exc}). The GATT "
                  f"credentials above are still valid; passive reading will be "
                  f"unavailable for this device.", file=sys.stderr)
            passive_key = passive_iv = None

        # Plausibility sanity checks. `warnings` are problems with the
        # credentials the integration *requires* (they set a non-zero exit);
        # `advisories` are notes about the optional broadcast pair, which must
        # not make a successful mobKey/IV extraction look like a failure to a
        # scripted caller.
        warnings: list[str] = []
        advisories: list[str] = []
        if mobkey == b"\xff" * 16:
            warnings.append(
                "mobKey is all-0xFF — flash slot is unprogrammed. "
                "Either the device was never registered, or the offset is wrong."
            )
        if iv == b"\xff" * 16:
            warnings.append("IV is all-0xFF — same caveat as above.")
        if mobkey == b"\x00" * 16:
            warnings.append(
                "mobKey is all-zero — that's not a valid AES key. "
                "Bypass-gadget reads may be silently failing; "
                "double-check the gadget PC/register."
            )
        if passive_key is None or passive_iv is None:
            advisories.append(
                "Broadcast key/IV could not be read — the optional passive "
                "reading feature won't work, everything else is unaffected."
            )
        elif passive_key == b"\xff" * 16 or passive_iv == b"\xff" * 16:
            advisories.append(
                "Broadcast key/IV is all-0xFF — this device has no passive "
                "reading pair; the integration will read everything over GATT."
            )

        # Cross-check against the redundant copy of the identity block. The
        # primary above is authoritative (it is what the firmware reads), but a
        # disagreement means this device's identity was rewritten without
        # updating the backup — i.e. it was re-personalised at some point, and
        # the backup holds someone else's credentials. Worth knowing before
        # trusting any value.
        try:
            backup_mobkey = read_block_via_gadget(
                sock, args.mobkey_offset + FLASH_IDENTITY_COPY_DELTA, 16,
                args.gadget_pc, args.gadget_reg,
            )
            backup_iv = read_block_via_gadget(
                sock, args.iv_offset + FLASH_IDENTITY_COPY_DELTA, 16,
                args.gadget_pc, args.gadget_reg,
            )
        except OpenOCDError as exc:
            backup_mobkey = backup_iv = None
            advisories.append(
                f"the backup copy of the identity block could not be read ({exc}); "
                f"the values above come from the primary copy and are unaffected."
            )
        if not identity_backup_matches(
            (mobkey, iv), (backup_mobkey, backup_iv)
        ):
            warnings.append(
                f"The backup copy of the identity block "
                f"(0x{args.mobkey_offset + FLASH_IDENTITY_COPY_DELTA:08x}) holds "
                f"DIFFERENT credentials from the primary. The primary is the one the "
                f"firmware uses, so the values above are correct — but this device has "
                f"been re-personalised at some point (e.g. after a firmware swap from "
                f"another unit), and the backup still carries the previous owner's "
                f"keys. If you did not expect that, confirm which set the device "
                f"actually accepts before using them."
            )

        result = {
            "ble_mac": ble_mac,
            "device_id": device_id,
            "mobkey_hex": mobkey.hex(),
            "iv_hex": iv.hex(),
            "mobkey_offset": f"0x{args.mobkey_offset:08x}",
            "iv_offset": f"0x{args.iv_offset:08x}",
            "passive_key_hex": passive_key.hex() if passive_key else None,
            "passive_iv_hex": passive_iv.hex() if passive_iv else None,
            "passive_key_offset": f"0x{args.passive_key_offset:08x}",
            "passive_iv_offset": f"0x{args.passive_iv_offset:08x}",
            "warnings": warnings,
            "advisories": advisories,
        }

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print()
            print("=" * 64)
            print("OneMeter device credentials")
            print("=" * 64)
            print(f"BLE MAC      : {ble_mac}")
            print(f"DEVICE ID    : {device_id}")
            print()
            print(f"mobKey (hex) : {mobkey.hex()}")
            print(f"IV     (hex) : {iv.hex()}")
            print()
            print("Optional — enables passive (broadcast) reading:")
            if passive_key and passive_iv:
                print(f"passive key  : {passive_key.hex()}")
                print(f"passive IV   : {passive_iv.hex()}")
            else:
                print("  (not available on this device)")
            print()
            if warnings:
                print("WARNINGS:")
                for w in warnings:
                    print(f"  - {w}")
                print()
            if advisories:
                print("NOTES (not an error):")
                for a in advisories:
                    print(f"  - {a}")
                print()
            print("Copy the BLE MAC, mobKey, and IV into the integration's")
            print("config flow. The passive pair is optional — it goes in the")
            print("passive key/IV fields. These bytes are sensitive — treat")
            print("them like a password.")
            print()

        if args.output:
            # These are credentials, so the file must not be group/world
            # readable. O_CREAT applies its mode only when the file is *created*,
            # so an existing file (e.g. one written 0644 by an older version of
            # this tool) has to be re-permissioned explicitly — and the message
            # reports the mode actually in effect, not the one requested.
            # O_NOFOLLOW refuses to write through a symlink planted at the path.
            payload = json.dumps(result, indent=2)
            fd = os.open(
                args.output,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "w") as fh:
                os.fchmod(fd, 0o600)
                fh.write(payload)
            mode = os.stat(args.output).st_mode & 0o777
            print(f"wrote JSON to {args.output} (mode {mode:04o})", file=sys.stderr)

        if warnings:
            return 1
        return 0
    finally:
        try:
            if _should_resume(pc_hijacked=_pc_hijacked, no_resume_flag=args.no_resume):
                _tncmd(sock, "resume")
            if _pc_hijacked:
                # Deliberately NOT resuming. The gadget writes PC without saving
                # the application's context, so `resume` would continue from the
                # gadget (or from wherever the last `step` left it) and run
                # garbage — that is how this tool used to fault devices. Leaving
                # the core halted is the safe end state.
                print(
                    "\nNOTE: the CPU has been left halted on purpose — the bypass "
                    "gadget does not restore the application's execution point, so "
                    "resuming it would run garbage and can fault the device.\n"
                    "Power-cycle the board (on most setups: disconnect and reconnect "
                    "the clamp) to resume normal operation. Until you do, it will not "
                    "advertise over BLE.",
                    file=sys.stderr,
                )
            sock.close()
        except Exception:  # pragma: no cover — best-effort cleanup
            pass


if __name__ == "__main__":
    sys.exit(main())
