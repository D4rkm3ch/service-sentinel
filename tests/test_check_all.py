"""The topbar's "Check All" button -- scheduler.run_check_all() runs Updates, then Logs, then
Compose's existing full-check job bodies strictly one after another (never bulk Regenerate AI
Response or Reset & re-check, both heavier/token-costlier actions), and POST /checks/check-all
is the thin HTTP route that backgrounds it. See scheduler.py's own docstring for why this reuses
_JOBS/_FEATURE_ORDER rather than duplicating the "run these in order" loop (it's the same shape
_run_chain already uses for the master schedule's grouped firing)."""

from unittest.mock import patch

from app import check_state, db, scheduler

db.init_db()


def _reset():
    for feature in check_state.FEATURES:
        check_state.set_running(feature)
        check_state.release_running(feature)


def setup_function(_):
    _reset()


def teardown_function(_):
    _reset()


def test_run_check_all_calls_each_features_job_body_in_nav_order():
    calls = []
    with patch.object(scheduler, "_JOBS", {
        "updates": (lambda: calls.append("updates"), "updates_job"),
        "logs": (lambda: calls.append("logs"), "logs_job"),
        "compose": (lambda: calls.append("compose"), "compose_job"),
    }):
        scheduler.run_check_all()
    assert calls == ["updates", "logs", "compose"]


def test_run_check_all_stops_the_chain_when_a_step_was_cancelled():
    """A Cancel clicked mid-Updates must stop the whole chain -- Logs/Compose must never start
    once Updates itself was the target of a cancel, even though check_state.is_cancel_requested
    doesn't get cleared until the next time that feature's mutex is claimed (see
    check_state.set_running/try_start), which is exactly the signal run_check_all reads."""
    calls = []

    def fake_updates():
        calls.append("updates")
        check_state.request_cancel("updates")

    with patch.object(scheduler, "_JOBS", {
        "updates": (fake_updates, "updates_job"),
        "logs": (lambda: calls.append("logs"), "logs_job"),
        "compose": (lambda: calls.append("compose"), "compose_job"),
    }):
        scheduler.run_check_all()
    assert calls == ["updates"]


def test_check_all_route_backgrounds_the_chain_and_returns_immediately(client):
    import time
    with patch("app.main.run_check_all") as mock_run:
        resp = client.post("/checks/check-all")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        # Threaded, not called inline on the request-handling thread -- give the background
        # thread a moment to actually start and call through.
        time.sleep(0.1)
    mock_run.assert_called_once_with()
