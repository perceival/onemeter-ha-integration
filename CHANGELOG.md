# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Passive reading: the integration can decode the device's broadcast
  advertisements and, while they keep arriving, stay off the GATT link
  entirely instead of running a connect/login/drain session each poll. It
  falls back to a normal session when they go quiet, which also re-arms the
  broadcast, so the two paths sustain each other. Needs the device's slot-2
  key/IV — a different pair from the mobKey/IV, read by
  `tools/extract_credentials.py` and entered as optional fields in the setup
  and reauth forms. Adds `sensor.<name>_data_source`,
  `sensor.<name>_advertised_device_clock`, `sensor.<name>_advertisements_decoded`,
  `sensor.<name>_advertisements_undecodable`, a `passive` value for
  `sensor.<name>_connection_state`, and a "Read data passively when possible"
  option to switch the preference off without removing the keys. Coverage is
  partial by design: the broadcast carries the energy registers and the device
  clock only, so everything else still comes from a session. While the broadcast
  is healthy no sessions run at all — the session-only values (battery, comm
  stats, identity, FS params) are then refreshed by the fallback session at
  least every six hours, which is the ceiling on their staleness, rather than by
  the poll interval (that interval only ever makes the gap shorter). Enter a
  single `-` in the passive key/IV fields on the reauth form to remove a stored
  pair.
- `sensor.<name>_cached_registers` — diagnostic entity listing every OBIS
  register the device has reported: the state is how many there are, and the
  attributes map each `A.B.C.D` code to its raw value, including codes that
  have no sensor of their own. Makes it possible to identify what a given
  meter exposes from the UI, without downloading the diagnostics dump. On
  devices with passive reading configured the same entity also shows the
  broadcast-only tags under `sensor.<name>_data_source`'s attributes when they
  have no OBIS mapping yet.
- `button.<name>_poll_now` — triggers an immediate session. Includes
  a brief drain phase that captures any live `cmd 0x25` / `cmd 0x20`
  frames the meter pushes before disconnect.
- `button.<name>_auto_detect_meter` — sends `cmd 0x19` to ask the
  device to probe its optical port. Disabled until the device reports
  successful meter communication at least once.
- Meter-protocol option in config flow + options flow (IEC / SML /
  Blink / DLMS / Leave unchanged). Selecting a real value writes the
  device's flash via `cmd 0x14` on the next session.
- Dynamic per-`dataType` register sensors auto-created when live meter
  data arrives.
- `cmd 0x36` comm-stats fully decoded — `meter_reads_succeeded`,
  `meter_reads_failed`, `meter_day_cycles_completed` exposed as
  diagnostic sensors.
- "Polite-close" `cmd 0x23 ()` sent before every disconnect.
- Stuck-state persistent notification after 10 consecutive login
  failures, suggesting the user wait rather than re-enter credentials.
- `tools/dump_last_obis.py` — standalone diagnostic for cmd 0x21
  (cached OBIS registers), independent of the HA coordinator. Also
  demonstrates the wait-for-reassembly-completion pattern manual
  scripts need but the coordinator gets for free.
- `sensor.<name>_energy_import_total` (`0.1.8.0`) and
  `sensor.<name>_energy_export_total` (`0.2.8.0`, disabled by default).
  Previously only the combined `energy_total` (`0.15.8.0`, "sum
  active energy") sensor existed, which is the wrong input for HA's
  Energy dashboard once local generation is involved (it nets import
  and export together instead of reporting them separately). These
  are the correct sources for "Grid consumption" / "Return to grid."
- **"I'm a prosumer" option** (setup screen + options flow,
  `CONF_PROSUMER`) — controls whether `energy_export_total` is enabled
  by default. Off by default: on a plain consumer-only meter, the
  export register just holds a static near-zero calibration artifact,
  not real production data, so it stays hidden unless the user
  explicitly declares they have local generation.

### Validated

- `cmd 0x21` (cached OBIS registers) decoded and cross-checked against
  a real Apator NORAX 3 meter for the first time: `energy_total`
  (`0.15.8.0`) matched the sum of import (`0.1.8.0`) and export (`0.2.8.0`)
  registers within rounding, and `last_meter_read` (`255.1.1.4`)
  decoded to the correct current date. Confirms the `obis_map.py`
  scale factor (0.01) and the `energy_total`/`last_meter_read` sensors
  are correct against real hardware, not just synthetic test data.
  Regression test added in `tests/test_decode.py` using the real
  captured payload.

### Known limitations

- Polling cadences shorter than ~10 minutes hit the device's
  post-session refusal-to-reconnect state frequently. Default 1 h is
  the recommended setting.
- `dataType → standard OBIS code` mapping is meter-model-specific and
  not shipped — see README.
