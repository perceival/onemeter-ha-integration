#!/usr/bin/env python3
"""Dump a device's cached OBIS registers (cmd 0x21 / LAST_OBIS) over BLE.

Useful for: confirming a meter is actually attached and has accumulated
data (a device with no meter returns a single frame of all-0xFF sentinel
entries), and for correlating raw OBIS codes against your meter's LCD
display when building an `obis_map.py` entry for a new meter model.

# Why this exists

`OneMeterSession.feed_rx()` returns `None` while a multi-fragment
response (which cmd 0x21 always is, once real data is present) is still
being reassembled. If the caller moves on to the next command before
reassembly completes, the reassembler's chained-IV state does not reset
on its own, and every following probe's response frames get
misinterpreted as continuation data of the abandoned response, which
manifests as a run of `None`s followed by a `CRC check failed` error.

This script waits for each probe to either complete or time out (in
which case it explicitly calls `session.reset_reassembler()`) before
sending the next one. The HA coordinator's real polling loop already
does the equivalent by construction (it waits for `_last_ack_cmd` to
update per-command); this script exists because one-off/manual
diagnostics don't get that for free.

Usage:
    python3 tools/dump_last_obis.py AA:BB:CC:DD:EE:FF <mobkey_hex> <iv_hex>
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))

from bleak import BleakClient, BleakScanner  # noqa: E402
from onemeter.const import EPSI_RX_CHAR, EPSI_TX_CHAR, EPSI_UUID_CHAR  # noqa: E402
from onemeter.protocol.session import OneMeterSession  # noqa: E402


async def send_and_wait(client, session, events, cmd, name, payload=b"", wait_s=12.0):
    """Send one probe and block until its response fully reassembles.

    Returns the decoded event, or None on timeout (after resetting the
    reassembler so the next probe starts clean).
    """
    events.clear()
    await client.write_gatt_char(EPSI_TX_CHAR, session.build_probe(cmd, payload), response=True)
    start = time.time()
    seen = 0
    result = None
    while time.time() - start < wait_s:
        await asyncio.sleep(0.3)
        while seen < len(events):
            ev = session.feed_rx(events[seen])
            seen += 1
            if ev is not None:
                result = ev
        if result is not None:
            break
    if result is None:
        print(f"{name}: timed out after {wait_s}s ({seen} frames, incomplete) -- resetting")
        session.reset_reassembler()
    return result


async def main(address: str, mobkey_hex: str, iv_hex: str, readout_seconds: float) -> None:
    mobkey = bytes.fromhex(mobkey_hex)
    iv = bytes.fromhex(iv_hex)
    events: list[bytes] = []

    def on_notify(_handle, data: bytearray) -> None:
        events.append(bytes(data))

    print(f"Scanning for {address} ...")
    dev = await BleakScanner.find_device_by_address(address, timeout=30.0)
    if not dev:
        print("NOT FOUND -- check the address, or move closer.")
        return

    async with BleakClient(dev, timeout=30.0) as client:
        await client.start_notify(EPSI_RX_CHAR, on_notify)
        await asyncio.sleep(1.5)

        peripheral_uuid = await client.read_gatt_char(EPSI_UUID_CHAR)
        session = OneMeterSession(key=mobkey, iv=iv)

        events.clear()
        for frame in session.build_login(bytes(peripheral_uuid), int(time.time())):
            await client.write_gatt_char(EPSI_TX_CHAR, frame, response=True)
            await asyncio.sleep(0.5)
        await asyncio.sleep(1.5)
        for raw in events:
            session.feed_rx(raw)  # LOGIN/TIME_SYNC/START_READOUT acks
        events.clear()
        session.reset_reassembler()

        result = await send_and_wait(client, session, events, 0x21, "LAST_OBIS")
        if result is None:
            print("No cached OBIS data (timed out) -- is a meter actually attached?")
        elif isinstance(result, list) and result and result[0].value == 0xFFFFFFFF:
            print("Device responded but every entry is the 0xFFFFFFFF sentinel "
                  "-- no meter attached / no data accumulated yet.")
        else:
            print(f"\n{len(result)} cached OBIS entries:\n")
            for e in result:
                b = e.obis
                print(f"  {b[0]}.{b[1]}.{b[2]}.{b[3]:<3}  raw=0x{e.value:08X}  ({e.value})")

        if readout_seconds > 0:
            print(f"\nListening {readout_seconds:.0f}s for live readout frames ...")
            events.clear()
            await client.write_gatt_char(
                EPSI_TX_CHAR, session.build_probe(0x23, bytes([0x01])), response=True
            )
            await asyncio.sleep(1.0)
            events.clear()
            start = time.time()
            while time.time() - start < readout_seconds:
                await asyncio.sleep(2.0)
                for raw in events:
                    ev = session.feed_rx(raw)
                    if ev is not None:
                        print(f"  [{time.time()-start:.1f}s] {ev!r}")
                events.clear()
            await client.write_gatt_char(EPSI_TX_CHAR, session.build_probe(0x23), response=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("address", help="BLE MAC address, e.g. AA:BB:CC:DD:EE:FF")
    p.add_argument("mobkey_hex", help="32 hex chars")
    p.add_argument("iv_hex", help="32 hex chars")
    p.add_argument("--readout-seconds", type=float, default=0.0,
                    help="also listen this long for live cmd 0x20/0x25 frames")
    args = p.parse_args()
    asyncio.run(main(args.address, args.mobkey_hex, args.iv_hex, args.readout_seconds))
