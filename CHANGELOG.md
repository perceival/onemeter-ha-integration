# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Session-only readings survive a Home Assistant restart. Every cached-OBIS
  sensor (energy, power, timestamps) and the raw OBIS sensors only refresh in a
  GATT session, so on a passive install a restart used to leave them
  unavailable for hours. They now show the value saved at shutdown until the
  next session delivers a fresh one. While they have a value, a
  `value_origin` attribute reads `restored` until then and `live` afterwards,
  and a device report always wins over the saved value.
- `LEGAL.md`: legal notice in Polish and English — no affiliation with the
  vendor, the interoperability basis, what the repository does not contain,
  and the no-warranty terms.
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
- Raw OBIS sensors for codes we have identified carry the name in parentheses —
  `OBIS 0.3.8.0 (reactive energy consumed)`, `OBIS 0.1.8.1 (active energy
  consumed, tariff 1)` — so the disabled-by-default list is readable without
  cross-referencing a code table. Codes we have *not* identified stay a bare
  `A.B.C.D`, because a label is a claim about what a register is. Labels cover
  the four energy families with their tariff slots, the meter-reading family's
  tariff-4 slot, the meter's own time and date registers, and four of the
  vendor-reserved `255.1.1.x` fields — `.6` and `.10` named from measured
  behaviour, `.11`/`.14` kept deliberately unnamed ("vendor-specific field")
  because the broadcast's timing was not enough to say what they carry: `.10`
  advances exactly one tick per second with an epoch-class magnitude, so it is the
  device's own clock; `.6` is that clock in 16 bits of quarter-hours, which its
  constant (non-zero) offset from `.10 // 900` establishes — so its deltas convert
  to time, its absolute value does not; `.11`/`.14` move with the broadcast and
  stop there. The meter's clock pair is measured the same way:
  `0.0.9.1` counts seconds within a day, `0.0.9.2` is a packed day counter that
  steps once a day. The energy names follow the Polish market's data-type
  catalogue (PSE), which calls the reactive pair "consumed"/"returned" rather than
  "inductive"/"capacitive" — that wording belongs to its quadrant codes. Codes
  that did not move within the observations made here, `1.67.1.0` among them,
  stay bare codes, because a label is a claim about what a register is. Note the
  label is part of the entity name, so a newly created entity's id includes it,
  while an entity that already existed for one of these codes is renamed on its
  next state write (its id does not change).
- `sensor.<name>_cached_registers` — diagnostic entity listing every OBIS
  register the device has reported: the state is how many there are, and the
  attributes map each `A.B.C.D` code to its raw value, including codes that
  have no sensor of their own. Makes it possible to identify what a given
  meter exposes from the UI, without downloading the diagnostics dump. On
  devices with passive reading configured the same entity also shows the
  broadcast-only tags under `sensor.<name>_data_source`'s attributes when they
  have no OBIS mapping yet.
- Raw sensors for discovered OBIS registers: every code the device holds a
  reading for and has no sensor of its own now gets one, **disabled by
  default**, carrying the device's raw u32 — no scale or unit is invented
  for a code whose meaning is unknown. Enable the interesting ones under Settings → Devices & Services →
  Entities and rename them there. This is the per-code counterpart to
  `sensor.<name>_cached_registers` (which shows every code at once, as
  attributes): codes that already have a scaled sensor are not duplicated, and
  the device's 0xFFFFFFFF "no value" sentinel reads as unknown rather than as
  4294967295. Codes the device holds no reading for are skipped rather than
  materialised as permanently-unknown entities, and creation is capped at
  `MAX_RAW_OBIS_ENTITIES` (64) per entry: the codes are device-chosen and each
  becomes a permanent entry in the entity registry, so they need a ceiling.
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

- The broadcast (advertisement) channel is AES-CCM in the vendor's own
  framing, not the session cipher: the message is `[0:19]` CCM
  ciphertext + `[19:23]` tag, with the quarter-hour index riding outside
  the encrypted message. The frame model was confirmed against real
  captures: every advertisement in the bench sample set authenticates and
  decrypts under its device's slot-2 key/IV — the only exceptions were 40
  from a unit still running its pre-restoration key at capture time — and
  the decoded registers agree with the session-read values to within a
  fraction of a percent. The bench sample set itself is not part of this
  repository. `tests/test_advert.py` pins a synthetic round-trip
  vector through the same AESCCM primitive the decoder uses — so layout
  drift fails fast — and covers the tamper, wrong-key, short-payload and
  unmapped-tag paths.
- `tools/extract_credentials.py` has been run end-to-end on a real
  device (FT232H + OpenOCD 0.12): the default flash offsets
  (`0x3F024`/`0x3F034`) and the default bypass gadget (PC `0x6D4`,
  register `r4`) were correct, and the read was internally consistent
  (repeat reads agreeing, high-entropy key material). Note that the
  FICR BLE address need not match the address the device advertises —
  see the extraction guide.

### Fixed

- `tools/extract_credentials.py` aborted with "bypass-gadget read
  failed" on OpenOCD 0.12 even when the gadget address and offsets were
  correct: after `reset halt`, the reply to `reg <r>` carries unrelated
  lines (`SWD DPIDR 0x...`) and a stray NUL ahead of the
  `<r> (/32): 0x...` line, so parsing every hex literal in the response
  saw two candidate values. The read is now anchored on the register's
  own line; regression test covers the interleaved output.
- `tools/extract_credentials.py` no longer resumes the CPU after a
  gadget read. The bypass gadget writes `PC` directly, saving no
  context, so `resume` continued from the gadget (or wherever the last
  `step` left the core) and ran garbage — the cause of the HardFaults
  seen after a read failed part-way. The tool now also waits for the
  halt that ends each `step` before reading the register (hand-typed
  commands worked because a human's delay absorbed it; the script's
  next command arrived too fast), retries a transient refusal instead of
  aborting the whole 16-byte block, leaves the target halted with an
  explanation, warns when the redundant second copy of the identity
  block disagrees (a re-personalised device still carries the donor's
  credentials there), and creates `--output` files as `0600` even when
  the file already existed.
- The passive key/IV pair could only be entered on the **reauth** form,
  which Home Assistant offers only once an entry is already flagged for
  reauthentication — and this integration deliberately never flags one,
  so a healthy entry had no way to switch passive reading on. The pair
  now lives on the Configure page, next to the passive-reading toggle.
- Credential fields accepted values that pass a 32-*character* check but
  decode to fewer bytes: `bytes.fromhex` silently skips tabs, newlines
  and other whitespace, so a key pasted out of a wrapped terminal could
  be stored 15 bytes long — and then raise inside HA's Bluetooth
  advertisement callback. Validation is now a strict 32-hex-character
  match after stripping separators, on the setup, reauth and options
  forms.
- A replayed or out-of-order broadcast could move a
  `total_increasing` energy register backwards; cached passive values
  now only ever move forward, and a sentinel value cannot overwrite a
  real reading.
- The bypass-gadget read of the **IV** was the one read not guarded
  against failure: it died with a raw traceback and exit 1 — which this
  tool documents as "success, but warnings printed" — while printing no
  credentials at all. It now reports the same clean error as the mobKey
  read and exits 4.
- A non-ASCII byte in a register reply — a bit-flip on the debug link —
  is now raised as a retryable `OpenOCDError` from the telnet helper. It
  previously escaped as a `UnicodeDecodeError` traceback, which is not
  the error type the retry loop catches. The byte is deliberately *not*
  replaced: substituting one inside a hex literal truncates it, so two
  reads would agree on the same wrong word and defeat `VERIFY_READS`.
- The register parse no longer assumes a 32-bit register name: it
  accepts any `(/N)` width and excludes a name preceded by an identifier
  character, so `--gadget-reg sp` can no longer match an `msp`/`psp` line
  in a listing. Both paths were fail-closed before (they errored out),
  but the documented `--gadget-reg` escape hatch now actually works for
  other firmware revisions.
- `--output` failures are reported instead of raising: a symlinked path, a
  file with extra hard links (which `O_NOFOLLOW` does not catch), a
  non-regular file such as a planted FIFO or `/dev/null`, an unwritable
  path, a short write, or an error after the truncate each exit 5 with a
  message rather than a traceback — and never leave a half-written file
  reported as success. The written-mode report also comes from `fstat` on
  the open descriptor, so a filesystem that silently ignores `chmod`
  (FAT/exFAT, CIFS/SMB) is reported honestly instead of claimed as 0600.
  The warning about that mode is now added before the result is printed, so
  `--json` output carries it too.

### Security

- Real captured device data has been removed from the tracked tree. (This is a
  statement about the tree, not about history: the blob in the branch's earlier
  commit still contains it, and the cmd-`0x21` payload's originating commit is
  already pushed to a fork.) The two advertisement plaintexts and the cmd-`0x21` payload come
  from a meter on a live household supply: the payload carries the metered
  import/export totals and the device's "last meter read" timestamp, which is
  consumption plus occupancy timing. They now live in the gitignored
  `tests/private/captures.json` — the ignore rule ships with the repo, so a
  clone gets the guard — and the tests that use them assert relationships
  (export < import, sum = import + export, timestamp decodes to the capture
  day) and skip when the file is absent. The published fixtures are synthetic:
  every value in them is invented, as are the two readings that used to be
  quoted in an `obis_map.py` comment.
- The credential example in `tools/EXTRACTING_CREDENTIALS.md` is now labelled
  illustrative, with patterned placeholder key bytes. It is upstream's own
  published example (present upstream since 2026-05) reproduced with its
  mobKey/IV replaced: the original pair sat under a BLE MAC and serial that
  *are* verifiable real-capture values, so a reader had no way to tell which
  part of the block was real. The pair matches none of the four devices this
  project has dumped.

### Known limitations

- Polling cadences shorter than ~10 minutes hit the device's
  post-session refusal-to-reconnect state frequently. Default 1 h is
  the recommended setting.
- `dataType → standard OBIS code` mapping is meter-model-specific and
  not shipped — see README.
