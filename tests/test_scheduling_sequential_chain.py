"""An explicit ask: when 2 or more features are all following the master/general schedule,
they should run one after another at the shared trigger time, not all at once (competing for
CPU/network/AI rate limits simultaneously). apply_schedules() groups any 2+ enabled,
master-schedule-following features into one combined APScheduler job (periodic_master_schedule_
chain) that calls each feature's check function in a fixed order and waits for each to finish
before starting the next, instead of registering their independent periodic_*_check jobs. A
single feature on the master schedule (nothing else enabled/sharing it) has nothing to sequence
against and keeps its own ordinary individual job, unaffected."""

import pytest

from app import db, scheduler


@pytest.fixture(autouse=True)
def clean_state(client):
    """Depends on `client` so the real scheduler is started -- see test_stage5_scheduler.py's
    fixture docstring for why an unstarted scheduler makes repeated apply_schedules() calls
    unreliable. All three features start disabled and on the master schedule; each test opts
    the features it needs back in."""
    for feature in ("updates", "logs", "compose"):
        db.set_feature_enabled(feature, False)
        db.set_feature_uses_master_schedule(feature, True)
    yield
    for feature in ("updates", "logs", "compose"):
        db.set_feature_enabled(feature, True)
        db.set_feature_uses_master_schedule(feature, True)
    scheduler.apply_schedules()


def test_two_master_schedule_features_are_grouped_into_one_combined_job():
    db.set_feature_enabled("updates", True)
    db.set_feature_enabled("logs", True)
    scheduler.apply_schedules()

    assert scheduler._scheduler.get_job(scheduler._MASTER_CHAIN_JOB_ID) is not None
    assert scheduler._scheduler.get_job("periodic_updates_check") is None
    assert scheduler._scheduler.get_job("periodic_logs_check") is None


def test_all_three_master_schedule_features_are_grouped_together():
    for feature in ("updates", "logs", "compose"):
        db.set_feature_enabled(feature, True)
    scheduler.apply_schedules()

    job = scheduler._scheduler.get_job(scheduler._MASTER_CHAIN_JOB_ID)
    assert job is not None
    for job_id in ("periodic_updates_check", "periodic_logs_check", "periodic_compose_check"):
        assert scheduler._scheduler.get_job(job_id) is None


def test_a_solo_master_schedule_feature_keeps_its_own_individual_job():
    db.set_feature_enabled("updates", True)
    scheduler.apply_schedules()

    assert scheduler._scheduler.get_job("periodic_updates_check") is not None
    assert scheduler._scheduler.get_job(scheduler._MASTER_CHAIN_JOB_ID) is None


def test_a_feature_on_its_own_custom_schedule_is_excluded_from_the_group():
    """Two features share the master schedule; a third has its own override -- only the two
    sharing the master schedule get grouped, the third keeps its own independent job."""
    db.set_feature_enabled("updates", True)
    db.set_feature_enabled("logs", True)
    db.set_feature_enabled("compose", True)
    db.set_feature_uses_master_schedule("compose", False)
    db.set_feature_schedule("compose", {"mode": "hourly", "interval_hours": 3, "start_hour": 0})
    scheduler.apply_schedules()

    assert scheduler._scheduler.get_job(scheduler._MASTER_CHAIN_JOB_ID) is not None
    assert scheduler._scheduler.get_job("periodic_compose_check") is not None
    assert scheduler._scheduler.get_job("periodic_updates_check") is None
    assert scheduler._scheduler.get_job("periodic_logs_check") is None


def test_dropping_back_to_one_master_schedule_feature_disbands_the_group():
    db.set_feature_enabled("updates", True)
    db.set_feature_enabled("logs", True)
    scheduler.apply_schedules()
    assert scheduler._scheduler.get_job(scheduler._MASTER_CHAIN_JOB_ID) is not None

    db.set_feature_enabled("logs", False)
    scheduler.apply_schedules()

    assert scheduler._scheduler.get_job(scheduler._MASTER_CHAIN_JOB_ID) is None
    assert scheduler._scheduler.get_job("periodic_updates_check") is not None


def test_the_combined_job_runs_each_feature_in_order_and_waits_for_each_to_finish():
    """The actual point of grouping: proves the chain runs sequentially (each call fully
    completes before the next begins), not just that a combined job exists."""
    calls = []

    def fake_updates():
        calls.append("updates-start")
        calls.append("updates-end")

    def fake_logs():
        calls.append("logs-start")
        calls.append("logs-end")

    scheduler._run_chain([fake_updates, fake_logs])

    assert calls == ["updates-start", "updates-end", "logs-start", "logs-end"]
