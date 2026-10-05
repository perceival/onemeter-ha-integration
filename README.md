# OneMeter — Home Assistant integration (unofficial)

Home Assistant custom integration for the **OneMeter** BLE energy-meter
optical reader (the Polish nRF51822-based device). Talks to the stock
device firmware over BLE through an ESPHome `bluetooth_proxy` (or any
other Home-Assistant-supported Bluetooth adapter), decrypts the
proprietary protocol, and exposes the device — and when a real meter
is attached, its meter-register readings — as native HA sensors.

> **Unofficial — not affiliated with OneMeter sp. z o.o.** See
> [LEGAL.md](LEGAL.md) for the legal notice (Polish and English).

## About this repository

This is a maintained continuation of
[grappeq/onemeter-ha-integration](https://github.com/grappeq/onemeter-ha-integration),
the original integration by Kacper Grabowski, released under the MIT
License. The original author's copyright notice is kept in
[LICENSE](LICENSE).

It adds, among other things, reading the device's encrypted broadcast
without connecting to it ([passive reading](#passive-reading-advertisement-broadcast)),
keeping session-only readings across Home Assistant restarts, labelled
raw OBIS sensors, and fixes to the credential tooling. See
[CHANGELOG.md](CHANGELOG.md) for the details.

The original author's backstory:

> When OneMeter went bankrupt and turned off their cloud, I got a little
> bit annoyed and decided to build my own integration. It works — but the
> main limitation is that you need to extract your device's AES key + IV
> from its flash over SWD before you can use it. That's definitely not
> super easy for a beginner.

## Status

Beta. In daily use on a device reading a real residential meter, and
tested on several more units on the bench. The test suite has 246 unit
tests, and the Home Assistant side has been exercised end-to-end against
live devices. Per-meter behaviour will vary — see the "Discovering your
meter's registers" section below.

## What you get

| Entity | What it shows |
|---|---|
| `sensor.<name>_battery_voltage` | CR2032 voltage, scaled from the device's ADC channel |
| `sensor.<name>_serial_number` | u32 integer from the device's identity blob (`cmd 0x87`) |
| `sensor.<name>_ble_mac` | The BLE MAC the device advertises with |
| `sensor.<name>_last_seen` | UTC timestamp of the most recent successful frame |
| `sensor.<name>_connection_state` | One of `disconnected`/`connecting`/`handshaking`/`authenticated`/`draining`/`cooling`/`sleeping`/`passive` |
| `sensor.<name>_rx_frames` | Total BLE notifications received this session |
| `sensor.<name>_rx_errors` | Frame-level errors (CRC, framing) |
| `sensor.<name>_rx_cipher_rejections` | Times the device returned the canned `0xFF 0x01` rejection |
| `sensor.<name>_meter_reads_succeeded` | Device-side counter (`succeededTotal` from cmd 0x36). Non-zero = the device has talked to a meter. |
| `sensor.<name>_meter_reads_failed` | Device-side counter |
| `sensor.<name>_meter_day_cycles_completed` | Device-side counter |
| `sensor.<name>_configured_meter_protocol` | The protocol (IEC / SML / Blink / DLMS) the integration last sent to the device via `cmd 0x14`. `unknown` if you've left it at "Leave unchanged". |
| `sensor.<name>_detected_meter` | Result of the last `Auto-detect meter` button press. |
| `sensor.<name>_data_source` | `active` if the last values came from a GATT session, `passive` if decoded from the broadcast. See [Passive reading](#passive-reading-advertisement-broadcast). |
| `sensor.<name>_advertised_device_clock` | The device's own clock as carried in its advertisement. It rides in the clear in the minimal advert, so it is unauthenticated — treat it as advisory, unlike the register values. |
| `sensor.<name>_advertisements_decoded` | Advertisements from this device that decoded successfully. |
| `sensor.<name>_advertisements_undecodable` | Advertisements from this device that failed to decode. Should stay at 0; a rising count means the broadcast changed shape or is being corrupted. (Other units never reach this handler — the callback filters on your device's address.) |
| `sensor.<name>_cached_registers` | Diagnostic: state is how many OBIS registers the device has reported, attributes map every one of them (`A.B.C.D` → raw value) including codes that have no sensor of their own. The quickest way to identify what a meter exposes. Note this is readable by any Home Assistant user, not just admins — unlike the diagnostics download. |
| `sensor.<name>_obis_<A>_<B>_<C>_<D>` | One sensor per discovered OBIS register that has no sensor of its own and that the device holds a reading for — the raw device value, with no invented scale. Identified codes carry their name in parentheses (e.g. `OBIS 0.3.8.0 (reactive energy consumed)`); the rest stay bare codes. Since Home Assistant derives a **new** entity's id from its name, a labelled one includes the label (`..._obis_0_3_8_0_reactive_energy_consumed`) — entities created before a label existed keep the id they were created with. **Disabled by default**; enable the ones you want under *Settings → Devices & Services → Entities*. |
| `button.<name>_poll_now` | Triggers an immediate session — refresh all sensors right now, including a brief listen for live meter pushes. |
| `button.<name>_auto_detect_meter` | Asks the device to probe the optical port (`cmd 0x19`). Disabled until the device has reported successful meter reads at least once. |

Plus, **once a meter is attached and has accumulated a cached reading**
(cmd 0x21 — polled every session), a fixed set of standard-OBIS sensors
work out of the box, no per-meter correlation needed:

| Entity | OBIS | What it shows |
|---|---|---|
| `sensor.<name>_energy_total` | `0.15.8.0` | Total active energy across all tariffs, kWh |
| `sensor.<name>_energy_tariff_1` | `0.15.8.1` | Energy on tariff 1, kWh |
| `sensor.<name>_energy_tariff_2` | `0.15.8.2` | Energy on tariff 2, kWh (disabled by default) |
| `sensor.<name>_energy_tariff_3` | `0.15.8.3` | Energy on tariff 3, kWh (disabled by default) |
| `sensor.<name>_energy_import_total` | `0.1.8.0` | Active energy imported from the grid (consumption), kWh |
| `sensor.<name>_energy_export_total` | `0.2.8.0` | Active energy exported to the grid (e.g. solar feed-in), kWh (disabled by default) |
| `sensor.<name>_last_meter_read` | `255.1.1.4` | Timestamp of the device's last successful meter read |

**Consumption vs. production:** `energy_total` (`0.15.8.0`) is a *sum*
register — for a plain meter with no local generation it's effectively
your consumption, but if you have solar/net-metering it nets import
and export together, which is the wrong input for Home Assistant's
Energy dashboard. Use `energy_import_total` (`0.1.8.0`) as the "Grid
consumption" source and `energy_export_total` (`0.2.8.0`) as the "Return
to grid" source instead.

`energy_export_total` is disabled by default, controlled by a
**"I'm a prosumer"** checkbox on the setup screen. Most installs have
no local generation, in which case the export register just holds a
static, near-zero calibration artifact — not real production data —
so it stays hidden unless you explicitly say otherwise. You can also
flip this later from the device's Configure page, but Home Assistant
only applies a changed default to sensors it hasn't created yet — if
`energy_export_total` already exists and is disabled, enabling
prosumer mode afterward won't un-hide it on its own; enable it once
manually under *Settings → Devices & Services → Entities*.

These read as `unavailable` until a real reading has been cached (a
device with no meter attached returns the `0xFFFFFFFF` sentinel for
every entry). The scale factor (0.01, i.e. each register unit is
10 Wh) and the `energy_total`/`energy_import_total`/`energy_export_total`/
`last_meter_read` codes are confirmed against a real Apator NORAX 3 —
see `CHANGELOG.md`. Other meter
families may use different or additional OBIS codes; unrecognized
ones are logged (`OneMeter ...: cached OBIS entry ...`) but don't get
an entity. `tools/dump_last_obis.py` dumps the raw entries directly
over BLE if you want to see what your meter reports before extending
`obis_map.py`.

Separately, **once a meter is attached and pushes live data**, the
integration auto-creates per-register sensors as it discovers them:

| Entity | What it shows |
|---|---|
| `sensor.<name>_meter_register_<N>` | Raw u32 value for OBIS register N (where N is the device's internal `dataType`) |
| `sensor.<name>_meter_register_<N>_timestamp` | Block timestamp from the matching `cmd 0x25` header |

The mapping from `<N>` (the device's `dataType`) to standard OBIS codes
is meter-dependent — see [Discovering your meter's
registers](#discovering-your-meters-registers) below. This is a
different mechanism from the cached-OBIS sensors above: those use
standard OBIS codes shipped in `obis_map.py`, while these use the
device's own internal per-model register numbering and need manual
correlation.

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
git clone https://github.com/perceival/onemeter-ha-integration.git
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

### Passive reading (advertisement broadcast)

The device broadcasts its energy registers on its own schedule, and that
broadcast is encrypted with a **different** AES key pair from the GATT
session protocol — a per-device "slot 2" pair stored in the same flash page
as the mobKey/IV (see [Extracting credentials](#extracting-credentials)).

If you supply that pair (two optional fields in the setup and reauth forms),
the integration switches to **passive-first**: while advertisements are being
decoded it never opens a GATT link at all, and only connects once they go
quiet — or when you press `Poll now` / `Auto-detect meter`. This removes the
connect/login/drain cycle, which is where the battery cost is: the device
advertises regardless, so reading the broadcast costs it nothing extra.

Everything the broadcast does not carry — battery voltage, comm stats,
identity, FS params and the full cached-OBIS set — is refreshed by the
session that a quiet broadcast triggers, and that session is also what
re-arms the broadcast. The two paths therefore sustain each other: a device
whose broadcast stops still gets polled on the normal interval.

**What this means for the poll interval:** while the broadcast is healthy there
are no sessions at all, so the interval no longer governs how often the device
is contacted — it bounds how *stale* the session-only values may get instead.
Battery voltage, comm stats, identity and FS params therefore refresh about
every six hours rather than hourly (and immediately if you press `Poll now`),
while the energy registers and the device clock keep updating from the
broadcast. That ceiling is deliberate: a captured advertisement stays valid
forever — the CCM nonce is the device's static IV — so without it anyone within
radio range could replay one every few minutes and keep the integration off the
link indefinitely.

Values that only a session can refresh — every cached-OBIS sensor (energy,
power and timestamps) and the raw OBIS sensors — survive a Home Assistant
restart: the last value saved at shutdown is shown until the next session
delivers a fresh one. While such a sensor has a value it carries a
`value_origin` attribute: `restored` until then, `live` afterwards (other
sensors don't have this attribute). A device report always wins over the
saved value. Without this, a restart would leave them unavailable for hours
on a passive install.

`sensor.<name>_data_source` shows which path produced the current values;
`sensor.<name>_advertisements_decoded` / `_undecodable` and
`sensor.<name>_advertised_device_clock` confirm the broadcast is actually
being read. Leave the passive fields blank and nothing changes — every read
goes over GATT exactly as before.

### Configurable options (after install)

*Settings → Devices & Services → OneMeter <name> → Configure*

| Option | Range | Default | Notes |
|---|---|---|---|
| Poll interval (seconds) | 300 – 21600 | 3600 (1 h) | Shorter intervals significantly shorten battery life and may trigger the [device-side cooldown](#known-limitations) more often. |
| Meter protocol | as above | Leave unchanged | Changing this triggers a flash write on the next session. |
| Read data passively when possible | on / off | on | Only has an effect when the passive key/IV were entered at setup. Keeps the integration off the GATT link while advertisements are still being decoded; uncheck to always read over GATT. |

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

> **Validated on real hardware.** The script and its default offsets
> have been confirmed end-to-end on a real device (FT232H + OpenOCD
> 0.12). Other firmware revisions may still differ; if something
> doesn't match, please file an issue with your results — both
> successes and failures are useful data.

In short, the procedure is:

1. Open the device to expose the nRF51822's SWD pads.
2. Wire a SWD programmer (CMSIS-DAP, ST-Link, J-Link, Black Magic
   Probe — anything OpenOCD supports).
3. Start OpenOCD with an appropriate `target/nrf51.cfg`.
4. Run `python tools/extract_credentials.py`. The script halts the
   CPU, reads the BLE MAC from FICR (a wiring sanity check — it is
   *not* necessarily the address the device advertises, see the
   guide), then reads the mobKey + IV, plus the device's "slot 2"
   key/IV pair (flash offsets `0x3f044`/`0x3f054`), via the CRP-bypass
   gadget. It leaves the CPU **halted** on purpose (resuming from a
   hijacked PC runs garbage), so power-cycle the device when it
   finishes.
5. Paste the BLE MAC, mobKey, and IV into the integration's config
   flow. The slot-2 pair is optional: paste it into the passive
   reading fields to enable
   [passive reading](#passive-reading-advertisement-broadcast).

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

### Passive reading coverage (and what isn't known yet)

Passive reading is deliberately partial:

- **What the broadcast carries:** three tagged register records plus the
  device's own clock. Four energy registers are identified so far — active
  consumed and returned (`0.1.8.0`, `0.2.8.0`) and reactive consumed and
  returned (`0.3.8.0`, `0.4.8.0`) — plus two vendor fields whose only name is
  "vendor-specific field". A few tags seen on real hardware still have no OBIS
  mapping — those are kept raw in the
  [diagnostics](#diagnostics) dump and are not turned into entities.
- **What it does not carry:** battery voltage, comm stats, identity, FS
  params, and most of the 29-register cached-OBIS set. Those still require a
  session, which is exactly what the staleness fallback provides.
- **Open question:** whether the data-bearing advertisement keeps flowing
  when the device is never connected. Bench testing suggests it appears for a
  while after a session on some units. If it does stop, the fallback runs a
  session — which re-arms it — so the behaviour is self-correcting either way,
  but passive mode may not eliminate connections entirely on every device.

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

## License and legal notice

[MIT](LICENSE). The software comes without warranty; reading credentials
can leave a device unusable. Use it only on devices you own. The full
notice, including the interoperability basis this project relies on, is
in [LEGAL.md](LEGAL.md).
