# OneMeter — Home Assistant integration (unofficial)

Home Assistant custom integration for the **OneMeter** BLE energy-meter
optical reader (the Polish nRF51822-based device). Talks to the stock
device firmware over BLE through an ESPHome `bluetooth_proxy` (or any
other Home-Assistant-supported Bluetooth adapter), decrypts the
proprietary protocol, and exposes the device — and when a real meter
is attached, its meter-register readings — as native HA sensors.

> **Unofficial — not affiliated with OneMeter sp. z o.o.**

## Backstory

When OneMeter went bankrupt and turned off their cloud, I got a little
bit annoyed and decided to build my own integration. It works — but the
main limitation is that you need to extract your device's AES key + IV
from its flash over SWD before you can use it. That's definitely not
super easy for a beginner. A friendlier extraction utility may come
later; for now SWD is the only path.

## Status

Pre-MVP, but functional on one tested device. The protocol layer has
~83 unit tests, and the HA glue has been exercised end-to-end against
a live device. Per-meter behaviour will vary — see the "Discovering
your meter's registers" section below.

## What you get

| Entity | What it shows |
|---|---|
| `sensor.<name>_battery_voltage` | CR2032 voltage, scaled from the device's ADC channel |
| `sensor.<name>_serial_number` | u32 integer from the device's identity blob (`cmd 0x87`) |
| `sensor.<name>_ble_mac` | The BLE MAC the device advertises with |
| `sensor.<name>_last_seen` | UTC timestamp of the most recent successful frame |
| `sensor.<name>_connection_state` | One of `disconnected`/`connecting`/`handshaking`/`authenticated`/`draining`/`cooling`/`sleeping` |
| `sensor.<name>_rx_frames` | Total BLE notifications received this session |
| `sensor.<name>_rx_errors` | Frame-level errors (CRC, framing) |
| `sensor.<name>_rx_cipher_rejections` | Times the device returned the canned `0xFF 0x01` rejection |
| `sensor.<name>_meter_reads_succeeded` | Device-side counter (`succeededTotal` from cmd 0x36). Non-zero = the device has talked to a meter. |
| `sensor.<name>_meter_reads_failed` | Device-side counter |
| `sensor.<name>_meter_day_cycles_completed` | Device-side counter |
| `sensor.<name>_configured_meter_protocol` | The protocol (IEC / SML / Blink / DLMS) the integration last sent to the device via `cmd 0x14`. `unknown` if you've left it at "Leave unchanged". |
| `sensor.<name>_detected_meter` | Result of the last `Auto-detect meter` button press. |
| `button.<name>_poll_now` | Triggers an immediate session — refresh all sensors right now, including a brief listen for live meter pushes. |
| `button.<name>_auto_detect_meter` | Asks the device to probe the optical port (`cmd 0x19`). Disabled until the device has reported successful meter reads at least once. |

Plus, **once a meter is attached and pushes data**, the integration
auto-creates per-register sensors as it discovers them:

| Entity | What it shows |
|---|---|
| `sensor.<name>_meter_register_<N>` | Raw u32 value for OBIS register N (where N is the device's internal `dataType`) |
| `sensor.<name>_meter_register_<N>_timestamp` | Block timestamp from the matching `cmd 0x25` header |

The mapping from `<N>` (the device's `dataType`) to standard OBIS codes
is meter-dependent — see [Discovering your meter's
registers](#discovering-your-meters-registers) below.

## Requirements

- Home Assistant **2024.6** or newer
- A Bluetooth source HA can use: either a USB BLE adapter on the host,
  or an [ESPHome bluetooth_proxy](https://esphome.github.io/bluetooth-proxies/)
  node in range of the OneMeter device. Through-proxy is the tested
  configuration.
- Your OneMeter device's `mobKey` and `IV` — **16 bytes each, hex** —
  extracted from the device's flash. See
  [Extracting credentials](#extracting-credentials) below. This is the
  hardest part of installation; the integration cannot work without it.

## Installation

### Via HACS

1. Add this repository as a custom integration in HACS:
   *HACS → Integrations → ⋮ → Custom repositories → URL:* this repo's
   URL, *Category:* Integration.
2. Search for **OneMeter** in HACS Integrations, install.
3. Restart Home Assistant.
4. *Settings → Devices & Services → Add Integration → OneMeter.*

### Manual

```bash
git clone https://github.com/grappeq/onemeter-ha-integration.git
cp -r onemeter-ha-integration/custom_components/onemeter \
      /path/to/your/homeassistant/config/custom_components/
# restart HA
```

## Configuration

After install, HA should auto-discover any OneMeter device advertising
within range of a configured Bluetooth source. The discovery card asks
for the device's **AES key** and **IV** (each 32 hex chars — colons /
spaces are OK and stripped), plus an optional **meter protocol**
override.

Meter protocol options:

| Setting | Effect |
|---|---|
| Leave unchanged *(default, safe)* | Integration never sends `cmd 0x14` — device keeps whatever protocol was set at registration |
| IEC 62056-21 mode D | Sends `cmd 0x14 [0x01, 0]` — **writes to device flash** |
| SML | `cmd 0x14 [0x01, 1]` |
| Blink (LED pulse) | `cmd 0x14 [0x01, 2]` |
| DLMS | `cmd 0x14 [0x01, 3]` |

Selecting anything other than "Leave unchanged" tells the integration
to write the device's flash on the next poll. Changeable later via
**Configure** on the integration page.

### Configurable options (after install)

*Settings → Devices & Services → OneMeter <name> → Configure*

| Option | Range | Default | Notes |
|---|---|---|---|
| Poll interval (seconds) | 300 – 21600 | 3600 (1 h) | Shorter intervals significantly shorten battery life and may trigger the [device-side cooldown](#known-limitations) more often. |
| Meter protocol | as above | Leave unchanged | Changing this triggers a flash write on the next session. |

## Extracting credentials

The integration needs your device's per-device AES key + IV (16 bytes
each). These were written into the OneMeter's flash by the
manufacturer's cloud during the device's initial registration with the
OneMeter mobile app. They aren't available anywhere outside the
device itself, so you have to read them out over SWD.

A helper script is provided at
[`tools/extract_credentials.py`](tools/extract_credentials.py).
See [`tools/EXTRACTING_CREDENTIALS.md`](tools/EXTRACTING_CREDENTIALS.md)
for the full step-by-step procedure, including how to wire up SWD,
how to run OpenOCD, and how to recover if the defaults don't match
your firmware revision.

> **⚠️ Untested:** the script has been derived from analysis of one
> specific device's firmware but **has not yet been run end-to-end
> against a live device.** If you try it, please file an issue with
> your results — both successes and failures are useful data.

In short, the procedure is:

1. Open the device to expose the nRF51822's SWD pads.
2. Wire a SWD programmer (CMSIS-DAP, ST-Link, J-Link, Black Magic
   Probe — anything OpenOCD supports).
3. Start OpenOCD with an appropriate `target/nrf51.cfg`.
4. Run `python tools/extract_credentials.py`. The script halts the
   CPU, reads the BLE MAC + mobKey + IV using a CRP-bypass gadget,
   resumes the CPU, and prints the values.
5. Paste the BLE MAC, mobKey, and IV into the integration's config
   flow.

Opening the device, soldering to debug pads, and using a SWD
programmer are non-trivial. If you've never done embedded work
before, find a friend who has.

## Known limitations

### Device-side post-session "cooldown"

After every successful session ends, the OneMeter device enters a
state where it refuses the next BLE connection for some period of
time. We don't fully understand the mechanism (see the reverse-
engineering notes for details — multiple suspected gates at both the
application-cmd layer and the SoftDevice BLE-stack layer; SWD reset is
the only reliable manual clear).

**Practical impact:**
- At the default **1-hour poll cadence** the device appears to be
  stable in field testing. Every poll succeeds.
- At cadences shorter than ~10 min the integration will frequently
  hit this and recover via exponential backoff — but you'll see
  reject counts climb.
- The integration handles this gracefully: keeps retrying with backoff
  capped at 5 min. After 10 consecutive failed logins it surfaces a
  persistent notification suggesting you wait for the device to
  recover. It will NOT trigger HA's "Reauth needed" flow on this
  basis — the `0xFF 0x01` rejection is the same regardless of cause.

If the integration appears truly stuck (notification stays up for a
day), the credentials may genuinely be wrong — open **Configure** and
re-enter them.

### Single-session-per-power-cycle for short intervals

In practice, polling more often than ~5–10 min will be unreliable
because of the above. Stick with 30 min or longer for stable
operation. The default 1 h is the recommended setting.

### Discovering your meter's registers

When you first connect a meter and press **Poll now**, the integration
opens a brief drain window listening for live meter data. As it
arrives, new `sensor.<name>_meter_register_<N>` entities are created
on the fly — one per `dataType` (the device's internal OBIS register
identifier).

The `<N>` to standard-OBIS mapping is meter-dependent. For our
reference (an Apator SK 16-072 MI-003, Polish 3-tariff residential):

| OBIS (meter LCD) | What it is |
|---|---|
| `15.8.0` | Total active energy across all tariffs (kWh) |
| `15.8.1` / `15.8.2` / `15.8.3` | Energy per tariff |
| `0.9.1` / `0.9.2` | Internal clock / date |
| `0.2.2` | Current tariff index |

You'll need to correlate the `dataType` integers that appear as
sensors with the values shown on your meter's display to build the
mapping for your specific model. Once you know the mapping you can
customise the entities in HA (rename, set `device_class`/`state_class`
via the entity-customisation UI, hide the ones you don't care about).

## Troubleshooting

### "Discovered device" card doesn't appear

- Confirm a Bluetooth source is configured in HA and reaches the
  OneMeter. `Settings → Devices & Services → Bluetooth` should show
  at least one scanner; the OneMeter should appear in any nearby-BLE
  list.
- The device advertises as `OM <4 digits>` (e.g. `OM 6863`) with
  manufacturer ID `0xFFFF`. The integration's discovery matcher keys
  off both.

### Battery sensor stays `unknown`

- The first poll hasn't finished yet — wait ~30 s post-discovery.
- If `connection_state` stays in `cooling`/`connecting`: see
  [the cooldown section](#device-side-post-session-cooldown).

### "OneMeter — connection stuck" persistent notification

- The device has rejected ≥ 10 consecutive login attempts. Recovery is
  usually time-based; the integration keeps retrying. If it never
  recovers within a day, the AES key / IV may be wrong — re-enter
  them via **Configure**.

### Per-register sensors don't appear

- Confirm `sensor.<name>_meter_reads_succeeded > 0`. If it stays at
  zero with the meter physically connected, the device isn't reading
  the meter — check optical-port alignment, or try setting the meter
  protocol explicitly (config option).
- Press **Poll now** explicitly — the drain phase is when most live
  data is captured.
- Per-register sensors are only created on first sighting and are
  disabled by default for any `dataType` we don't recognise. Re-press
  **Poll now** after enabling them.

### Diagnostics

*Settings → Devices & Services → OneMeter <name> → ⋮ → Download
diagnostics* — produces a JSON dump of all state with the key/IV
redacted. Safe to attach to bug reports.

## Development

```bash
git clone <this repo>
cd onemeter-ha-integration
python3 -m venv .venv && . .venv/bin/activate
pip install pytest cryptography
python -m pytest
```

The protocol layer in `custom_components/onemeter/protocol/` is pure
Python — no HA imports, no BLE imports. Run the test suite without an
HA instance or a real device.

Layout:
- `custom_components/onemeter/protocol/` — wire-protocol layer
  (cipher, framing, decoders, session state machine). Standalone.
- `custom_components/onemeter/coordinator.py` — owns the BLE
  connection lifecycle. Drives `OneMeterSession` with bytes.
- `custom_components/onemeter/{sensor,button,config_flow}.py` — HA
  entity platforms + the install / configure flow.
- `tests/` — pytest, against synthetic vectors derived from documented
  protocol observations. No live device required for the test suite.

## License

[MIT](LICENSE).
