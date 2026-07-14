"""Regression test: the Logs and Compose "Regenerate AI Response" bulk actions
(main._run_claimed_logs_bulk_regenerate / _run_claimed_compose_bulk_regenerate) are plain
sequential loops, not routed through persist.py's _run_concurrent_phase like Updates' bulk
regenerate is -- a real-world report showed clicking Cancel mid-run had no effect, because
these two loops never checked check_state.is_cancel_requested() at all. Fixed by checking it
once per subject/file, same "stop before starting the next one" contract as everywhere else
Cancel is wired in."""

from unittest.mock import patch

from app import check_state, db, main

db.init_db()


def _reset():
    for feature in check_state.FEATURES:
        check_state.set_running(feature)
        check_state.release_running(feature)


def setup_function(_):
    _reset()


def teardown_function(_):
    _reset()


def test_logs_bulk_regenerate_stops_after_cancel_is_requested():
    subjects = [f"bulkregen-log-{i}" for i in range(5)]
    for s in subjects:
        db.upsert_finding("logs", s, "issue a", "error", "warning", "desc")
        db.upsert_finding("logs", s, "issue b", "error", "warning", "desc")

    calls = []

    def fake_overview(source, subject, display_name, findings, force=False):
        calls.append(subject)
        if len(calls) == 2:
            check_state.request_cancel("logs")
        return "overview"

    try:
        with patch("app.main.db.all_log_watch_states_with_status",
                   return_value=[{"name": s} for s in subjects]), \
             patch("app.main._get_or_build_overview", side_effect=fake_overview), \
             patch("app.main.stacks.run_log_stack_analysis_pass") as mock_stack_pass:
            main._run_claimed_logs_bulk_regenerate()

        assert len(calls) < len(subjects), f"expected cancellation to stop the loop early, got all {len(calls)}"
        mock_stack_pass.assert_not_called()
    finally:
        for s in subjects:
            with db.get_conn() as conn:
                conn.execute("DELETE FROM findings WHERE source = 'logs' AND subject = ?", (s,))


def test_compose_bulk_regenerate_stops_after_cancel_is_requested():
    subjects = [f"bulkregen-compose-{i}.yml" for i in range(5)]
    for s in subjects:
        db.upsert_finding("compose", s, "issue a", "reliability", "warning", "desc")
        db.upsert_finding("compose", s, "issue b", "reliability", "warning", "desc")

    calls = []

    def fake_overview(source, subject, display_name, findings, force=False):
        calls.append(subject)
        if len(calls) == 2:
            check_state.request_cancel("compose")
        return "overview"

    try:
        with patch("app.main.db.all_compose_file_states_with_status",
                   return_value=[{"name": s} for s in subjects]), \
             patch("app.main._get_or_build_overview", side_effect=fake_overview):
            main._run_claimed_compose_bulk_regenerate()

        assert len(calls) < len(subjects), f"expected cancellation to stop the loop early, got all {len(calls)}"
    finally:
        for s in subjects:
            with db.get_conn() as conn:
                conn.execute("DELETE FROM findings WHERE source = 'compose' AND subject = ?", (s,))
