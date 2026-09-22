"""Passive-first data-source policy.

Pure decision logic, deliberately free of Home Assistant and BLE imports so it
can be unit-tested directly (see tests/test_policy.py). The coordinator's run
loop asks this module what to do next, which is what makes "when do we touch
the radio" testable without an HA runtime — the bugs that matter here (a
broadcast that latches the gate open, a gate that overrides the retry policy)
are all in this decision, not in the plumbing around it.
"""
from __future__ import annotations

from collections.abc import Mapping
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
