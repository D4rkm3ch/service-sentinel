"""Translates the friendly schedule specs stored in the database into APScheduler triggers.

A spec is a small dict, one of:
  {"mode": "daily", "hour": 6, "minute": 0}
  {"mode": "hourly", "interval_hours": 4}
  {"mode": "weekly", "day_of_week": "mon", "hour": 6, "minute": 0}
  {"mode": "custom", "cron": "0 6 * * *"}

Keeping this as structured data rather than raw cron strings is what lets the UI offer simple
presets while still allowing exact cron for anyone who wants it via the "custom" mode.
"""

from apscheduler.triggers.cron import CronTrigger

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
              "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


def build_trigger(spec: dict) -> CronTrigger:
    mode = spec.get("mode", "daily")

    if mode == "hourly":
        interval = max(1, int(spec.get("interval_hours", 4)))
        return CronTrigger(hour=f"*/{interval}", minute=0)

    if mode == "weekly":
        day = spec.get("day_of_week", "mon")
        if day not in DAY_NAMES:
            day = "mon"
        return CronTrigger(day_of_week=day, hour=int(spec.get("hour", 6)), minute=int(spec.get("minute", 0)))

    if mode == "custom":
        return CronTrigger.from_crontab(spec.get("cron", "0 6 * * *"))

    # "daily" and any unrecognized mode fall back to a plain daily time.
    return CronTrigger(hour=int(spec.get("hour", 6)), minute=int(spec.get("minute", 0)))


def describe(spec: dict) -> str:
    """Human-readable one-liner for the settings page."""
    mode = spec.get("mode", "daily")

    if mode == "hourly":
        interval = max(1, int(spec.get("interval_hours", 4)))
        return f"Every {interval} hour{'s' if interval != 1 else ''}"

    if mode == "weekly":
        day = spec.get("day_of_week", "mon")
        label = DAY_LABELS.get(day, "Monday")
        return f"Weekly on {label} at {int(spec.get('hour', 6)):02d}:{int(spec.get('minute', 0)):02d}"

    if mode == "custom":
        return f"Custom: {spec.get('cron', '0 6 * * *')}"

    return f"Daily at {int(spec.get('hour', 6)):02d}:{int(spec.get('minute', 0)):02d}"
