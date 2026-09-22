"""Tests for the SWD extraction tool's OpenOCD telnet layer.

The tool talks to OpenOCD over a plain socket, so its whole protocol can be
driven with a fake socket and scripted replies — no device, no debugger, no
`homeassistant`. That matters more than usual here: the two bugs these tests pin
(a register read racing the halt that ends `step`, and a `resume` from a
hijacked PC) only ever showed up against real hardware, which is precisely why
they survived so long.

The module is loaded by path because `tools/` is not on the pytest pythonpath —
it is a standalone script, not part of the integration package.
"""
import importlib.util
import pathlib

import pytest

_TOOL_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "tools" / "extract_credentials.py"
)
_spec = importlib.util.spec_from_file_location("extract_credentials", _TOOL_PATH)
extract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract)

HALTED = "     nrf51.cpu  cortex_m  little  swd  halted\n"
RUNNING = "     nrf51.cpu  cortex_m  little  swd  running\n"
HEADER = "       TargetName   Type     Endian  TapName  State\n"
HALTED_WORD = 0x12345678


class FakeOpenOCD:
    """A socket stand-in that records commands and replays scripted replies.

    `replies` maps a command to the text OpenOCD would print (the prompt is
    appended). A value may be a list, consumed one entry per call with the last
    entry repeating — that is how a flaky read is expressed — or a callable
    taking the call count, for replies that must keep varying. OpenOCD echoes the
    command over telnet, so the fake does too.
    """

    def __init__(self, replies=None, running_polls=0):
        self.replies = {}
        for key, value in (replies or {}).items():
            if callable(value) or isinstance(value, list):
                self.replies[key] = value
            else:
                self.replies[key] = [value]
        self.running_polls = running_polls
        self.commands: list[str] = []
        self.resumed = False
        self._pending = b""
        self._closed = False

    def sendall(self, data: bytes) -> None:
        cmd = data.decode().strip()
        self.commands.append(cmd)
        if cmd == "targets" and self.running_polls > 0:
            self.running_polls -= 1
            text = RUNNING
        elif cmd in self.replies:
            reply = self.replies[cmd]
            if callable(reply):
                text = reply(self.commands.count(cmd))
            else:
                text = reply.pop(0) if len(reply) > 1 else reply[0]
        elif cmd == "targets":
            # A real openocd always reports the current state, so an unscripted
            # `targets` means "halted" — the steady state for this tool.
            text = HALTED
        else:
            text = ""
        self._pending += (cmd + "\n").encode() + text.encode() + extract.PROMPT

    def recv(self, _n: int) -> bytes:
        chunk, self._pending = self._pending, b""
        return chunk

    def settimeout(self, _t) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:
        self._closed = True


@pytest.fixture(autouse=True)
def _fast_halt_wait(monkeypatch):
    """Keep the halt-wait timeout/poll tiny so the failure test stays quick."""
    monkeypatch.setattr(extract, "WAIT_HALT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(extract, "WAIT_HALT_POLL_S", 0.001)


@pytest.fixture(autouse=True)
def _reset_hijack_flag():
    """_pc_hijacked is module-level and never cleared in normal use."""
    extract._pc_hijacked = False
    yield
    extract._pc_hijacked = False


# --- the halt-wait parsing ---------------------------------------------------


@pytest.mark.parametrize(
    "output, expected",
    [
        (HALTED, True),
        (RUNNING, False),
        (HEADER, False),
        ("", False),
        ("Could not read register 'r4'\n", False),
        (HEADER + RUNNING + HALTED, True),
        (HEADER + HALTED + RUNNING, True),
    ],
)
def test_target_is_halted_parses_the_state_column(output, expected):
    assert extract._target_is_halted(output) is expected


# --- the halt-wait behaviour -------------------------------------------------


def test_wait_halted_polls_until_the_target_stops():
    """The fix for the reported bug: `step`'s halt is asynchronous, so the read
    must wait for it rather than firing immediately."""
    sock = FakeOpenOCD(running_polls=3)
    extract._wait_halted(sock)
    assert sock.commands.count("targets") == 4


def test_wait_halted_raises_when_the_target_never_stops():
    sock = FakeOpenOCD(running_polls=10_000)
    with pytest.raises(extract.OpenOCDError, match="did not halt"):
        extract._wait_halted(sock)


# --- the gadget read ---------------------------------------------------------


def test_gadget_read_waits_for_the_halt_before_reading_the_register():
    """Regression guard for the 'Could not read register' failure: a `targets`
    poll must appear between `step` and the read-back."""
    sock = FakeOpenOCD(
        replies={"reg r4": f"r4 (/32): 0x{HALTED_WORD:08x}\n"},
        running_polls=2,
    )
    value = extract.read_word_via_gadget(sock, 0x3F024, 0x6D4, "r4")
    assert value == HALTED_WORD
    step_at = sock.commands.index("step")
    read_at = len(sock.commands) - 1 - sock.commands[::-1].index("reg r4")
    assert "targets" in sock.commands[step_at:read_at]


def test_gadget_read_leaves_pc_marked_hijacked_even_on_success():
    """A *successful* read also leaves PC pointing into the gadget — nothing
    restores the application's execution point — which is why the cleanup path
    must not resume afterwards."""
    sock = FakeOpenOCD(replies={"reg r4": f"0x{HALTED_WORD:08x}\n"})
    extract.read_word_via_gadget(sock, 0x3F024, 0x6D4, "r4")
    assert extract._pc_hijacked is True


def test_gadget_read_raises_on_a_refused_register_read():
    sock = FakeOpenOCD(replies={"reg r4": "Could not read register 'r4'\n"})
    with pytest.raises(extract.OpenOCDError, match="expected one hex value"):
        extract.read_word_via_gadget(sock, 0x3F024, 0x6D4, "r4")
    assert extract._pc_hijacked is True


def test_verified_read_retries_a_transient_refusal():
    """A single refusal used to abort the whole 16-byte block (and the run).
    The first attempt fails, the second gets two agreeing reads."""
    sock = FakeOpenOCD(
        replies={"reg r4": ["Could not read register 'r4'\n", f"0x{HALTED_WORD:08x}\n"]}
    )
    assert extract.read_word_verified(sock, 0x3F024, 0x6D4, "r4") == HALTED_WORD


def test_verified_read_gives_up_after_max_retries():
    sock = FakeOpenOCD(replies={"reg r4": "Could not read register 'r4'\n"})
    with pytest.raises(extract.OpenOCDError, match="could not get 2 agreeing reads"):
        extract.read_word_verified(sock, 0x3F024, 0x6D4, "r4")


def test_verified_read_rejects_disagreeing_reads():
    """Always-disagreeing reads must fail rather than pick one: the whole point
    of the gadget is that a single read can silently be garbage."""
    sock = FakeOpenOCD(
        replies={"reg r4": lambda n: "0x00000001\n" if n % 2 else "0x00000002\n"}
    )
    with pytest.raises(extract.OpenOCDError, match="could not get 2 agreeing reads"):
        extract.read_word_verified(sock, 0x3F024, 0x6D4, "r4")


def test_block_read_returns_little_endian_words():
    sock = FakeOpenOCD(replies={"reg r4": f"0x{HALTED_WORD:08x}\n"})
    blob = extract.read_block_via_gadget(sock, 0x3F024, 8, 0x6D4, "r4")
    assert blob == HALTED_WORD.to_bytes(4, "little") * 2


def test_block_read_rejects_a_length_that_is_not_word_aligned():
    with pytest.raises(ValueError, match="multiple of 4"):
        extract.read_block_via_gadget(FakeOpenOCD(), 0x3F024, 6, 0x6D4, "r4")


# --- the unprotected (mdw) path ---------------------------------------------


def test_read_word_direct_parses_mdw_output():
    sock = FakeOpenOCD(replies={"mdw 0x100000a4 1": "0x100000a4: deadbeef\n"})
    assert extract.read_word_direct(sock, 0x100000A4) == 0xDEADBEEF


def test_read_word_direct_rejects_an_unexpected_reply():
    sock = FakeOpenOCD(replies={"mdw 0x100000a4 1": "target not halted\n"})
    with pytest.raises(extract.OpenOCDError, match="unexpected mdw response"):
        extract.read_word_direct(sock, 0x100000A4)


def test_read_ble_mac_assembles_the_six_bytes():
    """FICR DEVICEADDR is a 48-bit little-endian value split across two words;
    the printed MAC is those bytes most-significant first."""
    sock = FakeOpenOCD(
        replies={
            "mdw 0x100000a4 1": "0x100000a4: 33221100\n",   # low word
            "mdw 0x100000a8 1": "0x100000a8: 0000c0de\n",   # high 16 bits only
        }
    )
    assert extract.read_ble_mac(sock) == "C0:DE:33:22:11:00"


# --- the resume rule --------------------------------------------------------


def test_resume_is_allowed_only_when_pc_was_never_hijacked():
    assert extract._should_resume(pc_hijacked=False, no_resume_flag=False) is True


def test_resume_is_refused_after_a_gadget_read():
    """The bug this pins: resuming a hijacked PC runs garbage into a HardFault."""
    assert extract._should_resume(pc_hijacked=True, no_resume_flag=False) is False


def test_resume_flag_is_still_honoured():
    assert extract._should_resume(pc_hijacked=False, no_resume_flag=True) is False
