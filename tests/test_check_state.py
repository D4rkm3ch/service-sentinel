"""format_summary()'s "Last check" status text now includes when the check ran, not just what
it found -- requested after Stage 5 shipped automatic scheduling, since knowing whether a
check happened "just now" or "yesterday at 6am" matters once checks aren't only ever triggered
by a fresh click."""

from app import check_state


def test_summary_includes_last_run_timestamp_for_updates():
    state = {
        "last_result": {"checked": 59, "updates_found": 17, "errors": 0},
        "last_run_at": "2026-07-08T06:00:12.345678+00:00",
    }
    summary = check_state.format_summary("updates", state)
    assert summary == "Last check at 2026-07-08 06:00: 59 containers checked, 17 new updates"


def test_summary_includes_timestamp_for_logs_and_compose_too():
    state = {"last_result": {"checked": 5, "findings_found": 2}, "last_run_at": "2026-01-01T00:00:00+00:00"}
    assert check_state.format_summary("logs", state).startswith("Last check at 2026-01-01 00:00:")

    state = {"last_result": {"checked": 3, "reviewed": 3, "findings_found": 1}, "last_run_at": "2026-01-01T00:00:00+00:00"}
    assert check_state.format_summary("compose", state).startswith("Last check at 2026-01-01 00:00:")


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
    assert state["last_run_at"][:16].replace("T", " ") in summary
