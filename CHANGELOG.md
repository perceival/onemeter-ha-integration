# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
