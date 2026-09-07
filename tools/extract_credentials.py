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

The script halts the CPU, reads three values:
  - FICR DEVICEADDR (BLE MAC) — to confirm you're talking to the right device
  - mobKey  (16 bytes at flash 0x3F024)
  - IV      (16 bytes at flash 0x3F034)
…and prints them. Halts are short and the script resumes the CPU before
exiting.

It does not write to the device. It does not modify flash. It is purely
a read-out tool.
"""

from __future__ import annotations

import argparse
import json
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


class OpenOCDError(RuntimeError):
    pass


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


def read_word_via_gadget(
    sock: socket.socket, addr: int, gadget_pc: int, gadget_reg: str
) -> int:
    """Read a word from a CRP-protected flash address via the bypass gadget.

    The gadget is a single instruction in unprotected flash that does
    `LDR {reg}, [{reg}, #0]; BX LR`. Setting PC to the gadget and the
    register to the target address, then single-stepping, leaks one
    word into the same register.

    This call DOES NOT preserve the CPU's prior PC/register state.
    Only safe to call when the CPU is halted at reset / known-idle.
    """
    _tncmd(sock, f"reg pc 0x{gadget_pc:x}")
    _tncmd(sock, f"reg {gadget_reg} 0x{addr:x}")
    _tncmd(sock, "step")
    resp = _tncmd(sock, f"reg {gadget_reg}")
    # OpenOCD may interleave unrelated lines (e.g. "SWD DPIDR 0x...") after a
    # reset; only accept the value from the "<reg> (/32): 0x..." line itself.
    matches = re.findall(
        rf"{re.escape(gadget_reg)}\s*\(/32\):\s*(0x[0-9a-fA-F]+)", resp
    )
    if len(matches) != 1:
        raise OpenOCDError(
            f"expected one hex value reading {gadget_reg}, got {matches} from {resp!r}"
        )
    return int(matches[0], 16)


def read_word_verified(
    sock: socket.socket, addr: int, gadget_pc: int, gadget_reg: str
) -> int:
    """Read a word VERIFY_READS times and require agreement; retry up to MAX_RETRIES."""
    for _attempt in range(MAX_RETRIES):
        reads = [
            read_word_via_gadget(sock, addr, gadget_pc, gadget_reg)
            for _ in range(VERIFY_READS)
        ]
        if len(set(reads)) == 1:
            return reads[0]
    raise OpenOCDError(
        f"could not get {VERIFY_READS} agreeing reads at 0x{addr:08x} "
        f"after {MAX_RETRIES} attempts (last reads: {reads})"
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
    p.add_argument("--json", action="store_true",
                   help="Emit the result as a single JSON object on stdout instead of text.")
    p.add_argument("--output", type=Path, default=None,
                   help="Write JSON output to this path in addition to printing.")
    p.add_argument("--skip-reset", action="store_true",
                   help="Don't issue 'reset halt' first. Use if the device is already halted.")
    p.add_argument("--no-resume", action="store_true",
                   help="Leave the CPU halted on exit (debug aid).")
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

        # Plausibility sanity checks
        warnings: list[str] = []
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

        result = {
            "ble_mac": ble_mac,
            "device_id": device_id,
            "mobkey_hex": mobkey.hex(),
            "iv_hex": iv.hex(),
            "mobkey_offset": f"0x{args.mobkey_offset:08x}",
            "iv_offset": f"0x{args.iv_offset:08x}",
            "warnings": warnings,
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
            if warnings:
                print("WARNINGS:")
                for w in warnings:
                    print(f"  - {w}")
                print()
            print("Copy the BLE MAC, mobKey, and IV into the integration's")
            print("config flow. These bytes are sensitive — treat them like a")
            print("password.")
            print()

        if args.output:
            args.output.write_text(json.dumps(result, indent=2))
            print(f"wrote JSON to {args.output}", file=sys.stderr)

        if warnings:
            return 1
        return 0
    finally:
        try:
            if not args.no_resume:
                _tncmd(sock, "resume")
            sock.close()
        except Exception:  # pragma: no cover — best-effort cleanup
            pass


if __name__ == "__main__":
    sys.exit(main())
