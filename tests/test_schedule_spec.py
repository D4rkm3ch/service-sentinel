"""Stage 5b: the Hourly/Daily/Weekly/Monthly schedule picker, replacing the raw cron box.

The hourly case is the one non-obvious piece: APScheduler's cron hour field rejects a
"start/step" expression whenever start+step would run past 23 (e.g. hour="21/6" errors
outright, confirmed empirically before writing this), so "every N hours starting at H" has to
be expressed as an explicit wrapped-around list of hours instead. These tests prove the actual
fire times that produces, not just that build_trigger() doesn't crash.
"""

from datetime import datetime, timedelta

import pytz

from app import schedule_spec


def _fire_times(trigger, start, count):
    now = start
    times = []
    for _ in range(count):
        nxt = trigger.get_next_fire_time(None, now)
        times.append(nxt)
        now = nxt + timedelta(minutes=1)
    return times


def test_hourly_every_6_starting_at_21_wraps_correctly_across_a_day():
    spec = {"mode": "hourly", "interval_hours": 6, "start_hour": 21}
    trigger = schedule_spec.build_trigger(spec)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=pytz.UTC)
    fires = _fire_times(trigger, start, 4)
    assert [f.strftime("%H:%M") for f in fires] == ["03:00", "09:00", "15:00", "21:00"]


def test_hourly_every_12_starting_at_9():
    spec = {"mode": "hourly", "interval_hours": 12, "start_hour": 9}
    trigger = schedule_spec.build_trigger(spec)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=pytz.UTC)
    fires = _fire_times(trigger, start, 4)
    assert [f.strftime("%m-%d %H:%M") for f in fires] == [
        "01-01 09:00", "01-01 21:00", "01-02 09:00", "01-02 21:00",
    ]


def test_hourly_every_1_hour_fires_every_hour():
    spec = {"mode": "hourly", "interval_hours": 1, "start_hour": 0}
    trigger = schedule_spec.build_trigger(spec)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=pytz.UTC)
    fires = _fire_times(trigger, start, 3)
    assert [f.strftime("%H:%M") for f in fires] == ["00:00", "01:00", "02:00"]


def test_weekly_multi_day_fires_on_each_selected_day_only():
    spec = {"mode": "weekly", "days_of_week": ["mon", "wed", "fri"], "hour": 14, "minute": 0}
    trigger = schedule_spec.build_trigger(spec)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=pytz.UTC)  # a Thursday
    fires = _fire_times(trigger, start, 3)
    weekdays = [f.strftime("%A") for f in fires]
    assert weekdays == ["Friday", "Monday", "Wednesday"]
    assert all(f.strftime("%H:%M") == "14:00" for f in fires)


def test_weekly_single_day_still_works():
    spec = {"mode": "weekly", "days_of_week": ["mon"], "hour": 6, "minute": 0}
    trigger = schedule_spec.build_trigger(spec)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=pytz.UTC)
    fires = _fire_times(trigger, start, 2)
    assert all(f.strftime("%A") == "Monday" for f in fires)


def test_monthly_fires_on_the_configured_day():
    spec = {"mode": "monthly", "day_of_month": 15, "hour": 3, "minute": 30}
    trigger = schedule_spec.build_trigger(spec)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=pytz.UTC)
    fires = _fire_times(trigger, start, 3)
    assert [f.strftime("%Y-%m-%d %H:%M") for f in fires] == [
        "2026-01-15 03:30", "2026-02-15 03:30", "2026-03-15 03:30",
    ]


def test_monthly_day_31_skips_shorter_months_without_crashing():
    spec = {"mode": "monthly", "day_of_month": 31, "hour": 6, "minute": 0}
    trigger = schedule_spec.build_trigger(spec)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=pytz.UTC)
    fires = _fire_times(trigger, start, 3)
    months = [f.month for f in fires]
    assert months == [1, 3, 5]  # Feb has no 31st, so it's skipped, not an error


def test_daily_unchanged_from_before():
    spec = {"mode": "daily", "hour": 6, "minute": 30}
    trigger = schedule_spec.build_trigger(spec)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=pytz.UTC)
    fires = _fire_times(trigger, start, 2)
    assert [f.strftime("%H:%M") for f in fires] == ["06:30", "06:30"]


def test_unrecognized_mode_falls_back_to_daily_instead_of_crashing():
    """A stale spec saved before this redesign (mode="custom", with a "cron" key that no
    longer means anything) must never crash the scheduler -- just fall back safely."""
    spec = {"mode": "custom", "cron": "*/5 * * * *"}
    trigger = schedule_spec.build_trigger(spec)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=pytz.UTC)
    fires = _fire_times(trigger, start, 1)
    assert fires[0].strftime("%H:%M") == "06:00"


def test_describe_hourly():
    assert schedule_spec.describe({"mode": "hourly", "interval_hours": 6, "start_hour": 21}) == \
        "Every 6 hours, starting at 21:00"


def test_describe_weekly_multi_day():
    assert schedule_spec.describe({"mode": "weekly", "days_of_week": ["fri", "mon", "wed"], "hour": 14, "minute": 0}) == \
        "Weekly on Monday, Wednesday, Friday at 14:00"


def test_describe_monthly_ordinals():
    assert schedule_spec.describe({"mode": "monthly", "day_of_month": 1, "hour": 6, "minute": 0}) == \
        "Monthly on the 1st at 06:00"
    assert schedule_spec.describe({"mode": "monthly", "day_of_month": 2, "hour": 6, "minute": 0}) == \
        "Monthly on the 2nd at 06:00"
    assert schedule_spec.describe({"mode": "monthly", "day_of_month": 3, "hour": 6, "minute": 0}) == \
        "Monthly on the 3rd at 06:00"
    assert schedule_spec.describe({"mode": "monthly", "day_of_month": 11, "hour": 6, "minute": 0}) == \
        "Monthly on the 11th at 06:00"
    assert schedule_spec.describe({"mode": "monthly", "day_of_month": 21, "hour": 6, "minute": 0}) == \
        "Monthly on the 21st at 06:00"


def test_describe_daily_unchanged():
    assert schedule_spec.describe({"mode": "daily", "hour": 6, "minute": 0}) == "Daily at 06:00"
