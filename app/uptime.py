from datetime import datetime, timezone

_START_TIME = datetime.now(timezone.utc)


def get_uptime_str() -> str:
    delta = datetime.now(timezone.utc) - _START_TIME
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
