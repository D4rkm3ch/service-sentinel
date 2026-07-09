"""The Overview page's per-feature toggle is meant to control only each feature's automatic
schedule -- a real-world report proved this was broken for Updates (no gate existed anywhere,
so switching it off did nothing) and, once fixed the naive way, would have been wrong for Logs
and Compose too (their check functions gated themselves, so a disabled toggle also silently
broke the manual Check now button, which must always work). Covers all three features'
apply_schedules() gating and confirms the manual/scheduled functions never self-gate."""

from unittest.mock import patch

import pytest

from app import compose_reviewer, db, log_watcher, scheduler

db.init_db()


@pytest.fixture(autouse=True)
def clean_state(client):
    """Depends on `client` purely so the real scheduler is actually started -- see
    test_stage5_scheduler.py's clean_state fixture for why an unstarted scheduler makes
    repeated apply_schedules() calls in the same test unreliable.

    All three start disabled here (rather than all enabled, as this file used to do) since 2+
    enabled features sharing the master schedule now get grouped into one combined sequential
    job (see scheduler.py's apply_schedules and test_scheduling_sequential_chain.py) -- each
    parametrized case below enables only the one feature it's actually testing, so its solo
    periodic_*_check job id is unambiguous."""
    for feature in ("updates", "logs", "compose"):
        db.set_feature_enabled(feature, False)
    yield
    for feature in ("updates", "logs", "compose"):
        db.set_feature_enabled(feature, True)
    scheduler.apply_schedules()


@pytest.mark.parametrize("feature,job_id", [
    ("updates", "periodic_updates_check"),
    ("logs", "periodic_logs_check"),
    ("compose", "periodic_compose_check"),
])
def test_disabling_any_feature_removes_its_periodic_job(feature, job_id):
    db.set_feature_enabled(feature, True)
    scheduler.apply_schedules()
    assert scheduler._scheduler.get_job(job_id) is not None

    db.set_feature_enabled(feature, False)
    scheduler.apply_schedules()
    assert scheduler._scheduler.get_job(job_id) is None


def test_run_log_check_never_skips_itself_when_the_feature_is_disabled():
    db.set_feature_enabled("logs", False)
    with patch("app.log_watcher.list_running_containers_for_logs", return_value=[]):
        result = log_watcher.run_log_check()
    assert result != {"skipped": True}


def test_run_compose_check_never_skips_itself_when_the_feature_is_disabled():
    db.set_feature_enabled("compose", False)
    with patch("app.compose_reviewer.list_compose_files", return_value=[]):
        result = compose_reviewer.run_compose_check()
    assert result != {"skipped": True}
