"""Stage 5: automatic scheduled Updates checks.

Investigation first: the Settings page already rendered a working-looking schedule picker for
Updates, and saving it wrote to the database correctly (db.set_feature_schedule /
db.set_feature_uses_master_schedule) -- but scheduler.py's _JOBS dict deliberately excluded
"updates" since Stage 1, so nothing was ever actually registered with APScheduler. Not a bug
so much as a Stage 1 removal that was never reconnected; this is that reconnection.

These tests prove three things: (1) "updates" is a real registered job now, (2) the scheduled
job shares the exact same check_state "running" guard the UI uses -- so a scheduled firing
that lands while a manual check is still going gets skipped rather than running two overlapping
checks, and (3) it gets the same Stage 4 exception safety net.
"""

from unittest.mock import patch

import pytest

from app import check_state, db, persist, scheduler
from app.docker_client import TrackedContainer

db.init_db()


@pytest.fixture(autouse=True)
def clean_state(client):
    """Depends on `client` purely so the real scheduler is actually started (its background
    thread starts on FastAPI's startup event) -- APScheduler accumulates a stale duplicate in
    its own "pending jobs" list across repeated add_job(replace_existing=True) calls for the
    same id on a scheduler that was never started, an artifact that never happens in
    production (the scheduler is always running there) but reliably breaks apply_schedules()
    tests that call it more than once per test file otherwise."""
    # Logs/Compose are explicitly disabled too: 2+ enabled features sharing the master schedule
    # now get grouped into one combined sequential job (see scheduler.py's apply_schedules),
    # which would hide "updates" own periodic_updates_check job id that these tests assert on
    # directly (all test files share one physical SQLite database -- see conftest.py).
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    db.reset_updates_data()
    db.set_feature_enabled("updates", True)
    db.set_feature_enabled("logs", False)
    db.set_feature_enabled("compose", False)
    yield
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    db.reset_updates_data()
    db.set_feature_enabled("updates", True)
    db.set_feature_enabled("logs", False)
    db.set_feature_enabled("compose", False)


@pytest.fixture(autouse=True)
def no_real_release_notes_fetch():
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        yield


def test_updates_job_is_registered_by_apply_schedules():
    scheduler.apply_schedules()
    job = scheduler._scheduler.get_job("periodic_updates_check")
    assert job is not None, "apply_schedules() must register a job for updates, not just logs/compose"


def test_disabling_the_feature_removes_its_periodic_job():
    """Regression test for a real-world report: toggling a feature off on the Overview page
    was supposed to stop its automatic schedule, but updates never checked the toggle anywhere
    at all, so turning it off did nothing whatsoever -- confirmed still running after being
    switched off. apply_schedules() is now the one place this toggle is enforced."""
    scheduler.apply_schedules()
    assert scheduler._scheduler.get_job("periodic_updates_check") is not None

    db.set_feature_enabled("updates", False)
    scheduler.apply_schedules()
    assert scheduler._scheduler.get_job("periodic_updates_check") is None


def test_re_enabling_the_feature_restores_its_periodic_job():
    db.set_feature_enabled("updates", False)
    scheduler.apply_schedules()
    assert scheduler._scheduler.get_job("periodic_updates_check") is None

    db.set_feature_enabled("updates", True)
    scheduler.apply_schedules()
    assert scheduler._scheduler.get_job("periodic_updates_check") is not None


def test_disabling_the_feature_never_blocks_a_manual_check():
    """The toggle only ever controls the automatic schedule -- run_updates_check() itself
    (what both the scheduled job and a manual Check now ultimately call) must keep working
    with the feature toggled off, exactly as it always did (updates had no internal gate to
    remove here, unlike logs/compose -- see log_watcher.py/compose_reviewer.py)."""
    db.set_feature_enabled("updates", False)
    containers = [TrackedContainer(name="sonarr", image_repo="owner/sonarr", tag="latest",
                                    current_digest="sha256:old", labels={})]

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:new"):
        scheduler.run_updates_check()

    rows = db.list_tracked_containers_with_status()
    assert len(rows) == 1
    assert rows[0]["status"] == "update_available"


def test_toggle_route_re_applies_schedules_immediately(client):
    db.set_feature_enabled("updates", True)
    scheduler.apply_schedules()
    assert scheduler._scheduler.get_job("periodic_updates_check") is not None

    resp = client.post("/settings/toggle/updates")
    assert resp.status_code == 200
    assert db.get_feature_enabled("updates") is False
    assert scheduler._scheduler.get_job("periodic_updates_check") is None

    client.post("/settings/toggle/updates")  # restore for other tests
    assert db.get_feature_enabled("updates") is True


def test_scheduled_check_runs_and_persists_like_a_real_check():
    containers = [TrackedContainer(name="sonarr", image_repo="owner/sonarr", tag="latest",
                                    current_digest="sha256:old", labels={})]

    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:new"):
        scheduler.run_updates_check()

    rows = db.list_tracked_containers_with_status()
    assert len(rows) == 1
    assert rows[0]["container_name"] == "sonarr"
    assert rows[0]["status"] == "update_available"
    assert check_state.get_state("updates")["running"] is False


def test_scheduled_check_is_skipped_while_a_check_is_already_running():
    check_state.set_running("updates")

    with patch("app.persist.run_and_persist_check") as mock_run:
        scheduler.run_updates_check()

    mock_run.assert_not_called()


def test_ui_triggered_check_blocks_a_scheduled_one_fired_at_the_same_moment():
    """The actual scenario Stage 5 is guarding against: a scheduled firing landing while the
    user's own Check now click is still in flight."""
    assert persist.try_start_updates_check() is True  # simulates the UI's click claiming it

    with patch("app.persist.run_and_persist_check") as mock_run:
        ran = scheduler.run_updates_check()

    # run_updates_check() itself has no return value contract to assert on directly, but the
    # underlying check must never have been invoked.
    mock_run.assert_not_called()
    assert ran is None


def test_scheduled_check_recovers_from_a_failure_instead_of_getting_stuck():
    with patch("app.persist.run_and_persist_check", side_effect=RuntimeError("boom")):
        scheduler.run_updates_check()

    assert check_state.get_state("updates")["running"] is False

    # And a subsequent trigger (scheduled or manual) must still be able to run.
    containers = [TrackedContainer(name="c", image_repo="owner/c", tag="latest",
                                    current_digest="sha256:old", labels={})]
    with patch("app.reconcile.list_tracked_containers", return_value=containers), \
         patch("app.reconcile.get_latest_digest", return_value="sha256:old"):
        scheduler.run_updates_check()

    assert len(db.list_tracked_containers_with_status()) == 1
