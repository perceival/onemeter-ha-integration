"""Static constants for the OneMeter integration."""
from __future__ import annotations

DOMAIN = "onemeter"

# OneMeter device's GATT service / characteristic UUIDs.
EPSI_SERVICE_UUID = "ac040001-7214-e7b9-3c20-16caac2007b0"
EPSI_TX_CHAR = "ac040002-7214-e7b9-3c20-16caac2007b0"      # writes (commands)
EPSI_RX_CHAR = "ac040003-7214-e7b9-3c20-16caac2007b0"      # indicate (responses)
EPSI_STATUS_CHAR = "ac040004-7214-e7b9-3c20-16caac2007b0"  # read + notify
EPSI_UUID_CHAR = "ac040006-7214-e7b9-3c20-16caac2007b0"    # plaintext device UUID

# Config entry data keys.
CONF_KEY = "key"
CONF_IV = "iv"
CONF_ADDRESS = "address"

# Timings.
# --- Power-budget knobs ---
#
# The integration operates in polled mode: connect -> login -> read
# everything -> disconnect, repeat every POLL_INTERVAL_S. On CR2032, the
# default 1 h cadence is estimated to give roughly a year of battery
# life vs ~3 months for a persistent connection. A manual-poll button
# is exposed so the user can force a refresh between scheduled polls
# without changing the interval.
CONF_POLL_INTERVAL = "poll_interval"
CONF_METER_PROTOCOL = "meter_protocol"
# "Prosumer" (producer+consumer) — Polish energy-market term for a
# household that also feeds energy back to the grid (e.g. solar).
# Controls whether the energy_export_total sensor (OBIS 2.8.0) is
# enabled by default: most installs are consumer-only, where the
# export register just holds a static near-zero calibration artifact,
# not real production data — see obis_map.py.
CONF_PROSUMER = "is_prosumer"

# Optional slot-2 credentials for passive (advertisement) reading. The
# broadcast channel is encrypted with a *different* AES key from the GATT
# session protocol: the device's "slot 2" pair, stored immediately after
# mobKey/IV in the same flash page and read by the same SWD extraction tool
# (tools/extract_credentials.py). Leaving them blank simply means passive
# reading is unavailable and every read goes over GATT, as before.
CONF_PASSIVE_KEY = "passive_key"
CONF_PASSIVE_IV = "passive_iv"
# Options-flow switch that stops *preferring* passive data without having to
# forget the keys — useful to A/B the two paths when diagnosing.
CONF_PASSIVE = "passive_reading"

# Sentinel for "don't send cmd 0x14 to the device — leave its setting as is".
# Stored as a string in the options because voluptuous-serialize doesn't
# round-trip None cleanly through the frontend.
METER_PROTOCOL_UNCHANGED = "unchanged"

DEFAULT_POLL_INTERVAL_S = 3600   # 1 hour
MIN_POLL_INTERVAL_S = 300        # 5 min — below this, battery life suffers significantly
MAX_POLL_INTERVAL_S = 21600      # 6 h

# --- Passive-first (advertisement reading) policy ---
#
# While advertisements are being decoded successfully the integration does not
# connect at all: the device broadcasts its energy registers on its own
# schedule, so there is nothing to gain from a connect/login/drain cycle, and
# skipping it is what buys the battery life. If no *readable* advertisement
# arrives for PASSIVE_FALLBACK_S, one active session runs and refreshes
# everything the broadcast does not carry (battery, comm stats, identity, FS
# params, the full cached-OBIS set) — which is also what re-arms the device's
# broadcast, so the two paths sustain each other.
PASSIVE_FALLBACK_S = 1800.0   # 30 min without a decodable data advert → one active session
PASSIVE_WAIT_S = 60.0         # sleep slice while passive; short enough to notice a manual poll
DEFAULT_PASSIVE = True        # prefer passive data whenever the keys are present
# Hard ceiling on how long the broadcast may keep us off the GATT link, counted
# from the last *successful* session, whatever the advertisements look like.
# Without it, radio input could suppress the active path indefinitely: a
# captured advertisement re-verifies forever because the CCM nonce is the
# device's static IV, so anyone in range can replay one every 30 minutes. It
# also bounds how stale the data the broadcast does not carry (battery, comm
# stats, identity, FS params, the full cached-OBIS set) is allowed to get.
PASSIVE_MAX_SESSION_GAP_S = 21600.0   # 6 h — still 6x fewer connects than the 1 h default
# Minimum spacing between advertisement-driven state writes. Handling runs per
# advertisement, and a spoofed flood of keyless 9-byte adverts would otherwise
# cause a recorder row plus an entity re-render per advert (~50/s at legacy
# advertising rates). Real register content changes about every 15 minutes, so
# coalescing costs nothing; the counters still count every advert.
ADVERT_NOTIFY_MIN_INTERVAL_S = 5.0

# Timings on the BLE side (NOT user-tunable).
WRITE_TIMEOUT_S = 2.0          # individual GATT write timeout
LOGIN_TIMEOUT_S = 6.0          # whole 3-frame login must finish under this
RECONNECT_COOLDOWN_S = 10.0    # post-disconnect cooldown before any reconnect
RECONNECT_BACKOFF_MAX_S = 300.0 # cap on exponential backoff between reconnect attempts
# When the device rejects login with 0xFF 0x01, that's the post-session
# cooldown state. Rapid retries achieve nothing — wait at least this long
# before the next attempt, regardless of what `backoff` would say.
# Sequence becomes 60, 120, 240, 300, 300... over a 15-min device-side
# cooldown that's about 5 attempts vs ~15 with the old 10 s floor.
REJECTION_BACKOFF_FLOOR_S = 60.0
# RX deduplication window: the device sends every rejection notification
# twice (~1 ms apart, same ciphertext) — likely a BLE indication-vs-
# notification quirk. Drop the duplicate when we see the same ciphertext
# within this window. Functionally identical to processing both, just
# halves the visible reject counter + log noise.
RX_DEDUP_WINDOW_S = 0.15
# Threshold for declaring "the credentials are wrong, ask the user to re-enter".
# The 0xFF rejection from the device is generic — it can mean wrong cipher key,
# CRC error, OR a transient state-machine mismatch (device not yet ready after
# a recent disconnect). We use a high threshold so transient state issues don't
# escalate to the user; if the key really is wrong, all attempts fail and we
# get here eventually anyway.
AUTH_FAIL_THRESHOLD = 10
POST_CONNECT_SETTLE_S = 1.5    # wait after BLE link-up before subscribing / writing
POST_SUBSCRIBE_SETTLE_S = 1.5  # wait after start_notify before reading UUID + login
INTER_LOGIN_FRAME_S = 0.5      # spacing between the 3 login frames (0xAA -> 0x13 -> 0x23)

# Force-fresh-read drain parameters.
# After enabling live mode (cmd 0x23 [0x01]), the device streams cmd 0x25
# headers + cmd 0x20 records. We drain frames until QUIET_S elapses without
# a new frame, OR MAX_S total hits the hard cap.
FORCE_FRESH_QUIET_S = 2.0
FORCE_FRESH_MAX_S = 30.0

# Discovery filter — must match manifest.json's bluetooth section.
ADV_NAME_PREFIX = "OM "
ADV_MFR_ID = 0xFFFF

# Marker for the read probes we run once per session (after login) to
# populate identity / fs-params / etc.
ONE_SHOT_PROBES = (0x87, 0x36, 0x82, 0x21, 0x1D)  # CMD_IDENTITY, CMD_COMM_STATS, CMD_FS_PARAMS, CMD_LAST_OBIS, CMD_DEVICE_TIME
