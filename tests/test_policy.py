"""Data-source policy tests.

This is the logic that decides when the integration touches the radio, so it is
where the dangerous failure modes live: a broadcast that latches the active path
off, a gate that overrides the retry/backoff policy, and a passive merge that
moves a counter backwards. All three are covered explicitly below.
"""
from onemeter.policy import (
    Action,
    is_fresh,
    merge_cached,
    next_action,
    raw_obis_candidates,
    raw_obis_value,
    should_create_raw_obis,
    should_notify,
)

# Representative real-world values (const.py defaults).
POLL = 3600.0
MAX_GAP = 21600.0
PASSIVE_WAIT = 60.0
FALLBACK_FRESH_WINDOW = 1800.0
SENTINEL = 0xFFFFFFFF
IMPORT = bytes([0, 1, 8, 0])
EXPORT = bytes([0, 2, 8, 0])


def _decide(
    *,
    passive_enabled=True,
    passive_fresh=True,
    since_last_session_s=100.0,
    force_active=False,
    last_attempt_failed=False,
    poll_interval_s=POLL,
    max_passive_gap_s=MAX_GAP,
):
    return next_action(
        passive_enabled=passive_enabled,
        passive_fresh=passive_fresh,
        since_last_session_s=since_last_session_s,
        force_active=force_active,
        last_attempt_failed=last_attempt_failed,
        poll_interval_s=poll_interval_s,
        max_passive_gap_s=max_passive_gap_s,
        passive_wait_s=PASSIVE_WAIT,
    )


# --- the decision ---------------------------------------------------------


def test_fresh_broadcast_keeps_the_link_closed():
    action, wait_s = _decide()
    assert action is Action.PASSIVE
    assert wait_s == PASSIVE_WAIT


def test_stale_broadcast_falls_back_to_a_session():
    """The benign case the whole design promises: data adverts stop, so the
    active path resumes."""
    action, _ = _decide(passive_fresh=False, since_last_session_s=POLL)
    assert action is Action.SESSION


def test_broadcast_cannot_hold_the_gate_open_past_the_gap_cap():
    """A captured advertisement re-verifies forever (the CCM nonce is a static
    IV), so 'fresh' alone must never be able to suppress the active path
    indefinitely."""
    action, _ = _decide(passive_fresh=True, since_last_session_s=MAX_GAP)
    assert action is Action.SESSION


def test_fresh_broadcast_just_under_the_gap_cap_still_defers():
    action, _ = _decide(passive_fresh=True, since_last_session_s=MAX_GAP - 1)
    assert action is Action.PASSIVE


def test_stale_broadcast_waits_out_the_poll_interval():
    """No back-to-back connections when a broadcast goes quiet right after a
    session."""
    action, wait_s = _decide(passive_fresh=False, since_last_session_s=600.0)
    assert action is Action.WAIT
    assert wait_s == POLL - 600.0


def test_stale_broadcast_at_the_interval_boundary_runs_a_session():
    action, _ = _decide(passive_fresh=False, since_last_session_s=POLL)
    assert action is Action.SESSION


def test_first_run_ever_runs_a_session():
    """No session yet: nothing has been read, so connect regardless."""
    action, _ = _decide(passive_fresh=False, since_last_session_s=float("inf"))
    assert action is Action.SESSION


def test_forced_request_always_runs_a_session():
    """The manual poll / auto-detect buttons must work even while the broadcast
    is carrying everything."""
    action, _ = _decide(passive_fresh=True, since_last_session_s=1.0, force_active=True)
    assert action is Action.SESSION


def test_forced_request_wins_over_a_recent_session():
    action, _ = _decide(passive_fresh=False, since_last_session_s=1.0, force_active=True)
    assert action is Action.SESSION


def test_poll_interval_deferral_does_not_apply_without_passive_keys():
    """Regression guard: installs without passive keys must keep the original
    timing, where the post-session wait is the only pacer. Deferring here would
    swallow the backoff/rejection-floor retry after a failure that happened
    shortly after a success — e.g. making a manual poll look dead, or pushing
    the 'connection stuck' notification out by an hour."""
    action, _ = _decide(passive_enabled=False, passive_fresh=False, since_last_session_s=5.0)
    assert action is Action.SESSION


def test_without_passive_keys_a_session_runs_even_if_something_claims_fresh():
    action, _ = _decide(passive_enabled=False, passive_fresh=True, since_last_session_s=5.0)
    assert action is Action.SESSION


def test_failed_session_retry_is_not_deferred():
    """A failed attempt must not be deferred by the poll interval: the caller
    has already spaced it by the backoff or the rejection floor, and deferring
    again would push the retry out by the whole interval — which is how a
    manual poll ends up looking dead (the likely outcome of pressing it soon
    after a session, given the device's post-session cooldown)."""
    action, _ = _decide(
        passive_fresh=False, since_last_session_s=5.0, last_attempt_failed=True
    )
    assert action is Action.SESSION


def test_failed_session_retry_wins_over_a_fresh_broadcast():
    """The subtle half: a fresh broadcast must not absorb a failure either.

    Otherwise one failed session parks the loop in PASSIVE until the cap, and
    since the cap counts from the last *success*, PASSIVE then becomes
    unreachable until a session succeeds — retrying a refusing device every
    backoff cap while pushing the stuck-device notification out by hours."""
    action, _ = _decide(
        passive_fresh=True, since_last_session_s=100.0, last_attempt_failed=True
    )
    assert action is Action.SESSION


def test_after_a_success_a_fresh_broadcast_takes_over_again():
    action, _ = _decide(
        passive_fresh=True, since_last_session_s=5.0, last_attempt_failed=False
    )
    assert action is Action.PASSIVE


def test_successful_session_still_defers_a_retry():
    """The complement of the above: after a *success*, a stale broadcast waits
    out the interval rather than reconnecting immediately."""
    action, _ = _decide(
        passive_fresh=False, since_last_session_s=5.0, last_attempt_failed=False
    )
    assert action is Action.WAIT


def test_cap_is_a_true_ceiling_even_with_a_longer_poll_interval():
    """The interval deferral is clamped by the cap, so a long configured poll
    interval cannot extend how long radio input keeps us off the link."""
    action, wait_s = _decide(
        passive_fresh=False,
        since_last_session_s=100.0,
        poll_interval_s=MAX_GAP * 4,
    )
    assert action is Action.WAIT
    assert wait_s == MAX_GAP - 100.0


# --- freshness -------------------------------------------------------------


def test_is_fresh_with_nothing_stamped():
    assert is_fresh(stamp=None, now=1000.0, window_s=FALLBACK_FRESH_WINDOW) is False


def test_is_fresh_within_the_window():
    assert is_fresh(stamp=1000.0, now=1000.0 + 60, window_s=FALLBACK_FRESH_WINDOW) is True


def test_is_fresh_at_the_window_boundary():
    assert is_fresh(
        stamp=1000.0, now=1000.0 + FALLBACK_FRESH_WINDOW, window_s=FALLBACK_FRESH_WINDOW
    ) is False


def test_is_fresh_after_the_window():
    assert is_fresh(
        stamp=1000.0, now=1000.0 + FALLBACK_FRESH_WINDOW + 1, window_s=FALLBACK_FRESH_WINDOW
    ) is False


# --- passive merge ---------------------------------------------------------


def test_merge_accepts_a_higher_value():
    assert merge_cached({IMPORT: 100}, {IMPORT: 101}) == {IMPORT: 101}


def test_merge_accepts_an_equal_value():
    assert merge_cached({IMPORT: 100}, {IMPORT: 100}) == {IMPORT: 100}


def test_merge_rejects_a_lower_value():
    """The replay case: a captured advertisement must not be able to walk a
    TOTAL_INCREASING register backwards (which would log a meter reset)."""
    assert merge_cached({IMPORT: 100}, {IMPORT: 99}) == {}


def test_merge_accepts_a_new_code():
    assert merge_cached({}, {EXPORT: 5}) == {EXPORT: 5}


def test_merge_replaces_the_no_value_sentinel():
    """The device's sentinel means "no value", not a huge one — a real reading
    must be able to take its place."""
    assert merge_cached({IMPORT: SENTINEL}, {IMPORT: 7}) == {IMPORT: 7}


def test_merge_keeps_a_sentinel_from_overwriting_a_real_value():
    """An incoming sentinel means "no value", not a huge one: it must not
    displace a real reading with "unknown"."""
    assert merge_cached({IMPORT: 7}, {IMPORT: SENTINEL}) == {}


def test_merge_is_per_code():
    assert merge_cached({IMPORT: 100, EXPORT: 5}, {IMPORT: 101, EXPORT: 4}) == {IMPORT: 101}


# --- notification coalescing -------------------------------------------------


def test_notify_is_due_the_first_time():
    assert should_notify(last_notify_at=None, now=1000.0, min_interval_s=5.0) is True


def test_notify_inside_the_interval_is_coalesced():
    """The mitigation for the advert-flood vector: without this, every
    advertisement — including keyless ones anyone in range can spoof — would be
    its own state write."""
    assert should_notify(last_notify_at=1000.0, now=1002.0, min_interval_s=5.0) is False


def test_notify_at_the_interval_boundary_is_due():
    assert should_notify(last_notify_at=1000.0, now=1005.0, min_interval_s=5.0) is True


# --- invariants ------------------------------------------------------------


def test_window_and_cap_constants_stay_coherent():
    """The policy takes these as parameters, but the coordinator passes const's
    values — keep them from drifting apart."""
    from onemeter.const import (
        ADVERT_NOTIFY_MIN_INTERVAL_S,
        MAX_POLL_INTERVAL_S,
        PASSIVE_FALLBACK_S,
        PASSIVE_MAX_SESSION_GAP_S,
        PASSIVE_WAIT_S,
    )

    assert FALLBACK_FRESH_WINDOW == PASSIVE_FALLBACK_S
    assert PASSIVE_MAX_SESSION_GAP_S > POLL, "the cap must exceed the default interval"
    # Otherwise the cap stops being the effective ceiling (policy clamps to the
    # smaller of the two) and a long poll interval would silently raise it.
    assert PASSIVE_MAX_SESSION_GAP_S >= MAX_POLL_INTERVAL_S
    assert PASSIVE_WAIT_S < PASSIVE_FALLBACK_S
    assert ADVERT_NOTIFY_MIN_INTERVAL_S < PASSIVE_WAIT_S


# --- raw OBIS entity candidates (disabled-by-default discovery sensors) -------


def test_raw_obis_candidates_skips_codes_that_already_have_a_sensor():
    """A code with a descriptor gets a scaled sensor already — a raw twin for the
    same code would show the same register twice under two names."""
    known = {bytes([0, 1, 8, 0]), bytes([0, 2, 8, 0])}
    cached = [bytes([0, 1, 8, 0]), bytes([0, 7, 8, 0]), bytes([0, 2, 8, 0])]
    assert raw_obis_candidates(cached, known) == (bytes([0, 7, 8, 0]),)


def test_raw_obis_candidates_is_sorted_and_deduplicated():
    cached = [bytes([0, 9, 8, 0]), bytes([0, 3, 8, 0]), bytes([0, 9, 8, 0])]
    assert raw_obis_candidates(cached, ()) == (
        bytes([0, 3, 8, 0]),
        bytes([0, 9, 8, 0]),
    )


def test_raw_obis_candidates_empty_when_nothing_cached():
    assert raw_obis_candidates([], {bytes([0, 1, 8, 0])}) == ()


def test_raw_obis_value_hides_the_no_value_sentinel():
    """The device's 0xFFFFFFFF sentinel means "no reading"; showing it as
    4294967295 in a sensor would look like data."""
    assert raw_obis_value(None) is None
    assert raw_obis_value(0xFFFFFFFF) is None


def test_raw_obis_value_passes_real_values_through_unchanged():
    """Raw means raw: no scale is applied, since an unmapped code's unit is
    unknown and guessing one would be worse than showing the device's number.

    The values here are deliberately synthetic. Published constants must never
    be derived from a captured reading — not even "shifted by one" — because the
    real captures live in tests/private/ precisely so they stay unpublished.
    """
    assert raw_obis_value(0) == 0
    assert raw_obis_value(12345678) == 12345678
    assert raw_obis_value(0xFFFFFFFE) == 0xFFFFFFFE


def test_should_create_raw_obis_accepts_a_new_code_with_a_reading():
    assert should_create_raw_obis(
        obis=bytes([0, 7, 8, 0]), known_codes=(), value=5, added_count=0, limit=64
    )


def test_should_create_raw_obis_rejects_a_code_that_already_has_a_sensor():
    known = {bytes([0, 1, 8, 0])}
    assert not should_create_raw_obis(
        obis=bytes([0, 1, 8, 0]), known_codes=known, value=5, added_count=0, limit=64
    )


def test_should_create_raw_obis_rejects_a_code_without_a_reading():
    """No reading — the sentinel, or nothing cached at all — must not
    materialise an entity that can only ever say "unknown". A device that
    reports many valueless codes would otherwise spend the entry's allowance
    on them."""
    for value in (None, 0xFFFFFFFF):
        assert not should_create_raw_obis(
            obis=bytes([0, 7, 8, 0]), known_codes=(), value=value, added_count=0, limit=64
        )


def test_should_create_raw_obis_stops_at_the_cap():
    """The ceiling is the entry's lifetime total, so the check is on the count
    already created — not on how many this batch contains."""
    args = dict(obis=bytes([0, 7, 8, 0]), known_codes=(), value=5, limit=64)
    assert should_create_raw_obis(added_count=63, **args)
    assert not should_create_raw_obis(added_count=64, **args)


def test_raw_obis_candidates_honours_a_limit_and_stays_sorted():
    codes = [bytes([0, i, 8, 0]) for i in range(5)]
    assert raw_obis_candidates(codes, (), limit=2) == (
        bytes([0, 0, 8, 0]),
        bytes([0, 1, 8, 0]),
    )
    assert raw_obis_candidates(codes, (), limit=0) == ()
    assert len(raw_obis_candidates(codes, ())) == 5  # uncapped by default


def test_raw_obis_candidates_accepts_a_mapping_like_the_real_call_sites():
    """Both call sites pass dicts — the coordinator's cache and `KNOWN_OBIS` —
    so the *keys* are what gets filtered. Pinned because a change that iterated
    values instead would silently stop filtering."""
    cache = {
        bytes([0, 1, 8, 0]): 7,
        bytes([0, 3, 8, 0]): 9,
        bytes([255, 1, 1, 11]): 3,
    }
    known = {bytes([0, 1, 8, 0]): object()}
    assert raw_obis_candidates(cache, known) == (
        bytes([0, 3, 8, 0]),
        bytes([255, 1, 1, 11]),
    )


def test_should_create_raw_obis_accepts_a_mapping_for_known_codes():
    known = {bytes([0, 1, 8, 0]): object()}
    assert not should_create_raw_obis(
        obis=bytes([0, 1, 8, 0]), known_codes=known, value=7, added_count=0, limit=64
    )
