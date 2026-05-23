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

### Known limitations

- Polling cadences shorter than ~10 minutes hit the device's
  post-session refusal-to-reconnect state frequently. Default 1 h is
  the recommended setting.
- `dataType → standard OBIS code` mapping is meter-model-specific and
  not shipped — see README.
