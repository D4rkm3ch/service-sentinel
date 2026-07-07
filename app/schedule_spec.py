"""Translates the friendly schedule specs stored in the database into APScheduler triggers.

A spec is a small dict, one of:
  {"mode": "hourly", "interval_hours": 6, "start_hour": 21}
  {"mode": "daily", "hour": 6, "minute": 0}
  {"mode": "weekly", "days_of_week": ["mon", "wed", "fri"], "hour": 6, "minute": 0}
  {"mode": "monthly", "day_of_month": 1, "hour": 6, "minute": 0}

Keeping this as structured data rather than raw cron strings is what lets the UI offer a
plain frequency picker (Hourly/Daily/Weekly/Monthly) with no cron entry anywhere — the
"appropriate step" the Stage 5 UI redesign replaced the old cron text box with.
"""

from apscheduler.triggers.cron import CronTrigger

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
              "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def _valid_days(days) -> list[str]:
    if not isinstance(days, list):
        return ["mon"]
    valid = [d for d in DAY_NAMES if d in days]  # DAY_NAMES order, not whatever order the form posted
    return valid or ["mon"]


def _hourly_params(spec: dict) -> tuple[int, int]:
    interval = max(1, min(23, int(spec.get("interval_hours", 4))))
    start_hour = max(0, min(23, int(spec.get("start_hour", 0))))
    return interval, start_hour


def _hours_for(start_hour: int, interval: int) -> list[int]:
    """The set of hours-of-day an "every N hours, anchored at start_hour" schedule fires at.
    APScheduler's cron hour field can't express "start/step" when start+step would exceed 23
    (e.g. hour="21/6" is rejected outright — the step can't run past the field's own max), so
    this computes the explicit wrapped-around hour list instead, which works for any interval."""
    count = max(1, 24 // interval)
    return sorted({(start_hour + i * interval) % 24 for i in range(count)})


def _ordinal(day: int) -> str:
    suffix = "th" if 11 <= day <= 13 else _ORDINAL_SUFFIX.get(day % 10, "th")
    return f"{day}{suffix}"


def build_trigger(spec: dict, tz: str | None = None) -> CronTrigger:
    """tz is an IANA zone name (e.g. "Australia/Sydney") the times in `spec` are meant in —
    left as None here (this module stays a pure, database-free translation function, like
    reconcile.py) rather than reading the configured timezone itself; the caller
    (scheduler.py's apply_schedules(), which does own a database connection) passes
    db.get_timezone() through explicitly. None means "let APScheduler use its own default,"
    which is only ever the case before any real timezone has been configured."""
    mode = spec.get("mode", "daily")
    kwargs = {"timezone": tz} if tz else {}

    if mode == "hourly":
        interval, start_hour = _hourly_params(spec)
        hours = _hours_for(start_hour, interval)
        return CronTrigger(hour=",".join(str(h) for h in hours), minute=0, **kwargs)

    if mode == "weekly":
        days = _valid_days(spec.get("days_of_week"))
        return CronTrigger(day_of_week=",".join(days), hour=int(spec.get("hour", 6)), minute=int(spec.get("minute", 0)), **kwargs)

    if mode == "monthly":
        day = max(1, min(31, int(spec.get("day_of_month", 1))))
        return CronTrigger(day=day, hour=int(spec.get("hour", 6)), minute=int(spec.get("minute", 0)), **kwargs)

    # "daily" and any unrecognized/legacy mode (e.g. a stale "custom" cron spec saved before
    # this redesign removed that option) fall back to a plain daily time — never crash on an
    # old stored spec.
    return CronTrigger(hour=int(spec.get("hour", 6)), minute=int(spec.get("minute", 0)), **kwargs)


def describe(spec: dict) -> str:
    """Human-readable one-liner for the settings page."""
    mode = spec.get("mode", "daily")

    if mode == "hourly":
        interval, start_hour = _hourly_params(spec)
        return f"Every {interval} hour{'s' if interval != 1 else ''}, starting at {start_hour:02d}:00"

    if mode == "weekly":
        days = _valid_days(spec.get("days_of_week"))
        labels = ", ".join(DAY_LABELS[d] for d in days)
        return f"Weekly on {labels} at {int(spec.get('hour', 6)):02d}:{int(spec.get('minute', 0)):02d}"

    if mode == "monthly":
        day = max(1, min(31, int(spec.get("day_of_month", 1))))
        return f"Monthly on the {_ordinal(day)} at {int(spec.get('hour', 6)):02d}:{int(spec.get('minute', 0)):02d}"

    return f"Daily at {int(spec.get('hour', 6)):02d}:{int(spec.get('minute', 0)):02d}"
