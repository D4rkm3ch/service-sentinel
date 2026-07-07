"""format_summary()'s "Last check" status text includes when the check ran, not just what it
found -- requested after Stage 5 shipped automatic scheduling, since knowing whether a check
happened "just now" or "yesterday at 6am" matters once checks aren't only ever triggered by a
fresh click. Reworded to a compact "Last checked: HH:MM, DD Mon YYYY • N checked • ..." format
per direct feedback, and fixed to actually convert the stored UTC timestamp into the
configured TZ rather than displaying raw UTC -- the original version showed "22:26" when the
configured timezone's real local time was 08:26.

The timezone itself is DB-backed (db.get_timezone() -- Stage 5c, editable from the Settings
page), not the TZ env var directly, so these tests patch db.get_timezone() rather than
app.config.settings.tz."""

from unittest.mock import patch

from app import check_state, db

db.init_db()


def test_summary_format_for_updates():
    state = {
        "last_result": {"checked": 59, "updates_found": 17, "errors": 0},
        "last_run_at": "2026-07-07T22:26:00+00:00",
    }
    with patch("app.check_state.db.get_timezone", return_value="UTC"):
        summary = check_state.format_summary("updates", state)
    assert summary == "Last checked: 22:26, 07 Jul 2026 • 59 checked • 17 updates found"


def test_summary_converts_utc_to_the_configured_timezone():
    """The actual bug reported: the stored timestamp is always UTC, but the display must
    show the configured local time, not the raw UTC value."""
    state = {
        "last_result": {"checked": 59, "updates_found": 17, "errors": 0},
        "last_run_at": "2026-07-07T22:26:00+00:00",
    }
    with patch("app.check_state.db.get_timezone", return_value="Australia/Sydney"):
        summary = check_state.format_summary("updates", state)
    # 22:26 UTC on Jul 7 is 08:26 AEST on Jul 8 -- both the time and the date roll over.
    assert summary.startswith("Last checked: 08:26, 08 Jul 2026")


def test_summary_falls_back_to_utc_for_an_unrecognized_timezone_name():
    state = {
        "last_result": {"checked": 1, "updates_found": 0, "errors": 0},
        "last_run_at": "2026-07-07T22:26:00+00:00",
    }
    with patch("app.check_state.db.get_timezone", return_value="Not/A_Real_Zone"):
        summary = check_state.format_summary("updates", state)  # must not raise
    assert summary.startswith("Last checked: 22:26, 07 Jul 2026")


def test_summary_shows_errors_when_present():
    state = {
        "last_result": {"checked": 10, "updates_found": 0, "errors": 3},
        "last_run_at": "2026-07-07T22:26:00+00:00",
    }
    with patch("app.check_state.db.get_timezone", return_value="UTC"):
        summary = check_state.format_summary("updates", state)
    assert summary.endswith("• 3 errors")


def test_summary_format_for_logs_and_compose():
    with patch("app.check_state.db.get_timezone", return_value="UTC"):
        state = {"last_result": {"checked": 5, "findings_found": 2, "errors": 0}, "last_run_at": "2026-01-01T00:00:00+00:00"}
        assert check_state.format_summary("logs", state) == "Last checked: 00:00, 01 Jan 2026 • 5 checked • 2 findings found"

        state = {"last_result": {"checked": 3, "reviewed": 3, "findings_found": 1, "errors": 0}, "last_run_at": "2026-01-01T00:00:00+00:00"}
        assert check_state.format_summary("compose", state) == "Last checked: 00:00, 01 Jan 2026 • 3 checked • 3 reviewed • 1 finding found"


def test_summary_falls_back_gracefully_with_no_timestamp():
    state = {"last_result": {"checked": 1, "updates_found": 0, "errors": 0}, "last_run_at": None}
    summary = check_state.format_summary("updates", state)
    assert "unknown time" in summary


def test_summary_still_handles_no_check_and_disabled_states():
    assert check_state.format_summary("updates", {"last_result": None}) == "No check has run yet."
    assert check_state.format_summary("updates", {"last_result": {"skipped": True}}) == "Disabled."


def test_set_finished_and_get_state_round_trip_produces_a_real_timestamp(monkeypatch):
    """End-to-end through the real state machine (not just hand-built dicts above) -- proves
    the timestamp set_finished() records is exactly what format_summary() ends up showing."""
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    monkeypatch.setattr("app.check_state.db.set_last_check_result", lambda *a, **k: None)

    check_state.set_finished("updates", {"checked": 2, "updates_found": 1, "errors": 0})
    state = check_state.get_state("updates")
    summary = check_state.format_summary("updates", state)

    assert state["last_run_at"] is not None
    assert check_state._local_timestamp(state["last_run_at"]) in summary
