"""The topbar's "Regenerate AI" and "Reset & Re-Check" buttons -- the chained, all-three-features
counterparts of Check All (test_check_all.py), each feature's own mutex claimed and released
exactly like its single-feature button already does, one feature at a time so three features'
worth of AI calls never compete for the same provider rate limits at once. Regenerate All skips a
feature outright if its mutex is already claimed rather than waiting; Reset & Re-Check All uses
scheduler.run_single_check (the raw, synchronous job body) rather than the async-scheduling
TRIGGER_FUNCS the per-feature buttons use, since it needs each feature's re-check to actually
finish before wiping and starting the next one."""

import time
from unittest.mock import patch

from app import check_state, db, main, scheduler

db.init_db()


def _reset():
    for feature in check_state.FEATURES:
        check_state.set_running(feature)
        check_state.release_running(feature)


def setup_function(_):
    _reset()


def teardown_function(_):
    _reset()


# ---------------------------------------------------------------------------
# _run_global_bulk_regenerate
# ---------------------------------------------------------------------------

def test_run_global_bulk_regenerate_calls_each_features_bulk_regenerate_in_order():
    calls = []
    with patch("app.main.persist.try_start_updates_check", return_value=True), \
         patch("app.main.persist.run_claimed_bulk_regenerate", side_effect=lambda: calls.append("updates")), \
         patch("app.main._run_claimed_logs_bulk_regenerate", side_effect=lambda: calls.append("logs")), \
         patch("app.main._run_claimed_compose_bulk_regenerate", side_effect=lambda: calls.append("compose")):
        main._run_global_bulk_regenerate()
    assert calls == ["updates", "logs", "compose"]


def test_run_global_bulk_regenerate_skips_a_feature_whose_mutex_is_already_claimed():
    calls = []
    with patch("app.main.persist.try_start_updates_check", return_value=False), \
         patch("app.main.persist.run_claimed_bulk_regenerate", side_effect=lambda: calls.append("updates")), \
         patch("app.main._run_claimed_logs_bulk_regenerate", side_effect=lambda: calls.append("logs")), \
         patch("app.main._run_claimed_compose_bulk_regenerate", side_effect=lambda: calls.append("compose")):
        main._run_global_bulk_regenerate()
    # Updates was skipped (mutex already claimed elsewhere), but Logs/Compose still ran --
    # one feature already busy must not stall the other two.
    assert calls == ["logs", "compose"]


def test_run_global_bulk_regenerate_stops_the_chain_when_a_step_was_cancelled():
    calls = []

    def fake_logs_regenerate():
        calls.append("logs")
        check_state.request_cancel("logs")

    with patch("app.main.persist.try_start_updates_check", return_value=True), \
         patch("app.main.persist.run_claimed_bulk_regenerate", side_effect=lambda: calls.append("updates")), \
         patch("app.main._run_claimed_logs_bulk_regenerate", side_effect=fake_logs_regenerate), \
         patch("app.main._run_claimed_compose_bulk_regenerate", side_effect=lambda: calls.append("compose")):
        main._run_global_bulk_regenerate()
    assert calls == ["updates", "logs"]


def test_regenerate_all_route_backgrounds_the_chain_and_returns_immediately(client):
    with patch("app.main._run_global_bulk_regenerate") as mock_run:
        resp = client.post("/checks/regenerate-all")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        time.sleep(0.1)
    mock_run.assert_called_once_with()


# ---------------------------------------------------------------------------
# _run_global_reset_and_recheck
# ---------------------------------------------------------------------------

def test_run_global_reset_and_recheck_resets_and_rechecks_each_feature_in_order():
    calls = []
    with patch("app.main.db.reset_updates_data", side_effect=lambda: calls.append("reset-updates")), \
         patch("app.main.db.reset_logs_data", side_effect=lambda: calls.append("reset-logs")), \
         patch("app.main.db.reset_compose_data", side_effect=lambda: calls.append("reset-compose")), \
         patch("app.main.run_single_check", side_effect=lambda feature: calls.append("check-" + feature)):
        main._run_global_reset_and_recheck()
    assert calls == [
        "reset-updates", "check-updates",
        "reset-logs", "check-logs",
        "reset-compose", "check-compose",
    ]


def test_run_global_reset_and_recheck_stops_the_chain_when_a_step_was_cancelled():
    calls = []

    def fake_check(feature):
        calls.append("check-" + feature)
        if feature == "logs":
            check_state.request_cancel("logs")

    with patch("app.main.db.reset_updates_data"), patch("app.main.db.reset_logs_data"), \
         patch("app.main.db.reset_compose_data", side_effect=lambda: calls.append("reset-compose")), \
         patch("app.main.run_single_check", side_effect=fake_check):
        main._run_global_reset_and_recheck()
    assert calls == ["check-updates", "check-logs"]
    assert "reset-compose" not in calls


def test_reset_and_recheck_all_route_backgrounds_the_chain_and_returns_immediately(client):
    with patch("app.main._run_global_reset_and_recheck") as mock_run:
        resp = client.post("/checks/reset-and-recheck-all")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        time.sleep(0.1)
    mock_run.assert_called_once_with()


# ---------------------------------------------------------------------------
# scheduler.run_single_check
# ---------------------------------------------------------------------------

def test_run_single_check_calls_only_the_named_features_job_body():
    calls = []
    with patch.object(scheduler, "_JOBS", {
        "updates": (lambda: calls.append("updates"), "updates_job"),
        "logs": (lambda: calls.append("logs"), "logs_job"),
        "compose": (lambda: calls.append("compose"), "compose_job"),
    }):
        scheduler.run_single_check("logs")
    assert calls == ["logs"]
