"""Passive-first data-source policy.

Pure decision logic, deliberately free of Home Assistant and BLE imports so it
can be unit-tested directly (see tests/test_policy.py). The coordinator's run
loop asks this module what to do next, which is what makes "when do we touch
the radio" testable without an HA runtime — the bugs that matter here (a
broadcast that latches the gate open, a gate that overrides the retry policy)
are all in this decision, not in the plumbing around it.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum

from .protocol.decode import SENTINEL_NO_VALUE


class Action(Enum):
    """What the run loop should do on this iteration."""

    PASSIVE = "passive"   # the broadcast is carrying the load — stay off the link
    SESSION = "session"   # open a GATT session now
    WAIT = "wait"         # nothing to do yet — sleep for the returned duration


def next_action(
    *,
    passive_enabled: bool,
    passive_fresh: bool,
    since_last_session_s: float,
    force_active: bool,
    last_attempt_failed: bool,
    poll_interval_s: float,
    max_passive_gap_s: float,
    passive_wait_s: float,
) -> tuple[Action, float]:
    """Decide the next action, and for WAIT how long to sleep.

    Order matters:

    1. A manual poll or an auto-detect request always opens a session.
    2. A failed attempt also takes precedence over staying off the link. Without
       this, one failed session would park the loop in PASSIVE until the cap
       expired, and — because the cap is counted from the last *success* — PASSIVE
       would then be unreachable until a session succeeded, so a device that kept
       refusing connections would be retried every backoff cap indefinitely with
       its stuck-device notification pushed from ~40 min to hours. Retries are
       paced by whatever the caller already waited (backoff or rejection floor);
       the moment a session succeeds this branch stops applying.
    3. The broadcast keeps the link closed only while it is genuinely delivering
       (`passive_fresh`) *and* it has not already carried the load for longer
       than `max_passive_gap_s`. That second half is what stops radio input from
       suppressing the active path indefinitely: a captured advertisement
       re-verifies forever, because the CCM nonce is the device's static IV, so a
       replay every few minutes would otherwise hold the gate open and silently
       freeze everything the broadcast does not carry.
    4. Otherwise fall back to a session — but for a passive install, and only
       when the previous attempt *succeeded*, defer it to `poll_interval_s` after
       the last success, so a broadcast that goes quiet right after a good
       session cannot cause back-to-back connections. The deferral is clamped by
       the same cap, so `max_passive_gap_s` remains a true ceiling even if
       someone configures a longer poll interval. Non-passive installs skip this
       branch entirely: there the post-session wait is the only pacer, which is
       what keeps the existing backoff and rejection-floor retry behaviour
       intact.
    """
    if force_active:
        return Action.SESSION, 0.0
    if (
        passive_enabled
        and passive_fresh
        and not last_attempt_failed
        and since_last_session_s < max_passive_gap_s
    ):
        return Action.PASSIVE, passive_wait_s
    defer_until_s = min(max_passive_gap_s, poll_interval_s)
    if (
        passive_enabled
        and not last_attempt_failed
        and since_last_session_s < defer_until_s
    ):
        return Action.WAIT, defer_until_s - since_last_session_s
    return Action.SESSION, 0.0


def is_fresh(*, stamp: float | None, now: float, window_s: float) -> bool:
    """True while a monotonic stamp is within `window_s` of `now`.

    Monotonic, not wall-clock: an NTP step on the proxy must not be able to make
    stale broadcast data look current.
    """
    if stamp is None:
        return False
    return (now - stamp) < window_s


def should_notify(
    *, last_notify_at: float | None, now: float, min_interval_s: float
) -> bool:
    """True when a coalesced advertisement notification is due.

    Advertisement handling runs per advertisement, so without coalescing a
    flood of (spoofable, keyless) adverts becomes a state write each — a
    recorder row plus a re-render of every entity. Real register content only
    changes about every 15 minutes, so coalescing costs nothing; the counters
    still count every advert, they are just published less often.
    """
    if last_notify_at is None:
        return True
    return (now - last_notify_at) >= min_interval_s


def merge_cached(
    cached: Mapping[bytes, int], values: Mapping[bytes, int]
) -> dict[bytes, int]:
    """The subset of `values` that a passive merge is allowed to write.

    Never moves a register backwards. Every mapped code is a counter and the
    broadcast carries no ordering information, so a replayed (older)
    advertisement must not be able to make a TOTAL_INCREASING sensor regress and
    log a meter reset in HA's statistics.

    The device's "no value" sentinel is handled in both directions: a *cached*
    sentinel means "nothing known", so a real reading replaces it; an *incoming*
    one carries no information and never displaces a real reading. (The
    coordinator's `Advertisement.obis_values()` already drops incoming
    sentinels, so that half is belt-and-braces — but this is a public helper and
    its contract should not depend on the caller's filter.)
    """
    out: dict[bytes, int] = {}
    for obis, value in values.items():
        if value == SENTINEL_NO_VALUE:
            continue
        current = cached.get(obis)
        if current is not None and current != SENTINEL_NO_VALUE and value < current:
            continue
        out[obis] = value
    return out


def raw_obis_candidates(
    cached_codes: Iterable[bytes],
    known_codes: Iterable[bytes],
    limit: int | None = None,
) -> tuple[bytes, ...]:
    """OBIS codes that deserve a raw entity of their own.

    A code with a descriptor already has a scaled sensor, so a raw twin would
    just duplicate it; what is missing is the ability to see — and opt into —
    the codes a particular meter reports that this integration has no mapping
    for. Sorted, so the order entities are created in is stable across restarts.

    `limit` caps how many are returned (None = no cap). The caller passes the
    slots it has left, because the ceiling that matters is the total number of
    entities created over the entry's lifetime, not the size of one batch.
    """
    known = set(known_codes)
    codes = sorted({code for code in cached_codes if code not in known})
    return tuple(codes if limit is None else codes[: max(limit, 0)])


def raw_obis_value(value: int | None) -> int | None:
    """The value to expose for a raw OBIS register, or None when there is none.

    The device reports 0xFFFFFFFF for a register it holds no value for; passing
    that through would show 4294967295 as if it were a reading.
    """
    if value is None or value == SENTINEL_NO_VALUE:
        return None
    return value


def session_value(
    *, live_seen: bool, live: object | None, restored: object | None
) -> tuple[object | None, str | None]:
    """The value a session-sourced sensor shows, and where it came from.

    Registers outside the broadcast only reach this runtime through a GATT
    session (the cached register set, cmd 0x21). A restart empties that cache
    and a passive install can run hours before its next session, so without a
    stand-in every restart blanks these sensors. The rule:

    - once this runtime holds an entry for the register, that entry is the
      truth, even when the device reports it holds no value (`live` None) — a
      value saved before the restart must never outvote the device;
    - until then, the value saved at shutdown (`restored`) stands in, labelled
      "restored" so it can't pass for a fresh reading.

    `live` None with `live_seen` covers both the device's no-value sentinel and
    a present-but-unparsable value (e.g. an out-of-range timestamp): either way
    there is no reading to show, so the sensor is unavailable.

    Returns (value, origin); origin is "live" (this runtime's own data, whether
    from a session or the broadcast), "restored", or None (no value).
    """
    if live_seen:
        return (live, "live") if live is not None else (None, None)
    if restored is not None:
        return restored, "restored"
    return None, None


def should_create_raw_obis(
    *,
    obis: bytes,
    known_codes: Iterable[bytes],
    value: int | None,
    added_count: int,
    limit: int,
) -> bool:
    """Whether a discovered OBIS code should get a raw entity of its own.

    Pure so the decision the device can influence is testable: the codes come
    from the device, and every yes here becomes a permanent entity-registry
    entry, so each condition matters. False when the code already has a scaled
    sensor, when the device holds no reading for it (an entity that can only
    say "unknown" is noise), or when the entry has spent its allowance — the
    ceiling is per entry, across the whole run, not per batch.
    """
    if obis in set(known_codes):
        return False
    if raw_obis_value(value) is None:
        return False
    return added_count < limit
