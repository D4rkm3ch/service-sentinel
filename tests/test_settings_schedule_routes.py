"""Stage 5b: the Hourly/Daily/Weekly/Monthly schedule picker on the Settings page, replacing
the raw cron text box. Also covers a real pre-existing bug found while working in this file: a
missing `<script>` opening tag meant ~125 lines of JS (cron validation, schedule visibility
toggling, severity/apprise field gating) rendered as literal visible text on the page and never
executed at all -- almost certainly the actual root cause of "the Settings page's scheduling
might not currently be working correctly" reported before this stage, separate from Stage 5a's
"updates" was never registered with the scheduler at all.
"""

import pytest

from app import db


@pytest.fixture(autouse=True)
def updates_feature_enabled():
    """apply_schedules() only registers a feature's periodic job when its toggle is on (see
    scheduler.py) -- these tests are about the schedule picker itself, not that gate, so keep
    "updates" enabled regardless of what other test files leave it as (all test files share one
    physical SQLite database -- see conftest.py)."""
    db.set_feature_enabled("updates", True)
    yield
    db.set_feature_enabled("updates", True)


def test_settings_page_has_no_raw_javascript_leaking_as_text(client):
    """Regression test for the missing <script> tag: the whole point is that this text must
    never appear outside of a <script>...</script> pair."""
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "isValidCronItem" not in resp.text
    assert "function updateScheduleVisibility" in resp.text  # still present, just inside <script> now
    # "<script" (not "<script>") to also count base.html's <script src="..."> include.
    open_count = resp.text.count("<script")
    close_count = resp.text.count("</script>")
    assert open_count == close_count, "every <script> must have a matching </script>"


def test_settings_page_renders_all_four_modes(client):
    resp = client.get("/settings")
    assert 'value="hourly"' in resp.text
    assert 'value="daily"' in resp.text
    assert 'value="weekly"' in resp.text
    assert 'value="monthly"' in resp.text
    assert "cron" not in resp.text.lower()


def test_save_hourly_schedule(client):
    resp = client.post("/settings/schedule/master", data={
        "master_mode": "hourly", "master_interval_hours": "6", "master_start_hour": "21",
    })
    assert resp.status_code == 200
    assert db.get_master_schedule() == {"mode": "hourly", "interval_hours": 6, "start_hour": 21}


def test_save_daily_schedule(client):
    resp = client.post("/settings/schedule/master", data={"master_mode": "daily", "master_time": "06:30"})
    assert resp.status_code == 200
    assert db.get_master_schedule() == {"mode": "daily", "hour": 6, "minute": 30}


def test_save_weekly_schedule_with_multiple_days(client):
    resp = client.post("/settings/schedule/master", data={
        "master_mode": "weekly",
        "master_days_of_week": ["mon", "wed", "fri"],
        "master_time": "14:30",
    })
    assert resp.status_code == 200
    assert db.get_master_schedule() == {
        "mode": "weekly", "days_of_week": ["mon", "wed", "fri"], "hour": 14, "minute": 30,
    }


def test_save_weekly_schedule_normalizes_day_order_regardless_of_form_order(client):
    resp = client.post("/settings/schedule/master", data={
        "master_mode": "weekly",
        "master_days_of_week": ["fri", "mon", "wed"],  # posted out of order
        "master_time": "06:00",
    })
    assert resp.status_code == 200
    assert db.get_master_schedule()["days_of_week"] == ["mon", "wed", "fri"]


def test_save_monthly_schedule(client):
    resp = client.post("/settings/schedule/master", data={
        "master_mode": "monthly", "master_day_of_month": "15", "master_time": "03:00",
    })
    assert resp.status_code == 200
    assert db.get_master_schedule() == {"mode": "monthly", "day_of_month": 15, "hour": 3, "minute": 0}


def test_save_schedule_applies_immediately_to_the_real_scheduler(client):
    from app import scheduler

    client.post("/settings/schedule/master", data={
        "master_mode": "hourly", "master_interval_hours": "2", "master_start_hour": "0",
    })
    job = scheduler._scheduler.get_job("periodic_updates_check")
    assert job is not None
    hour_field = next(f for f in job.trigger.fields if f.name == "hour")
    assert str(hour_field) == "0,2,4,6,8,10,12,14,16,18,20,22"
