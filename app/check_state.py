import threading
from datetime import datetime, timezone

FEATURES = ("updates", "logs", "compose")

_lock = threading.Lock()
_state = {name: {"running": False, "last_result": None, "last_run_at": None} for name in FEATURES}


def set_running(feature: str) -> None:
    with _lock:
        _state[feature]["running"] = True


def set_finished(feature: str, result: dict) -> None:
    with _lock:
        _state[feature]["running"] = False
        _state[feature]["last_result"] = result
        _state[feature]["last_run_at"] = datetime.now(timezone.utc).isoformat()


def get_state(feature: str) -> dict:
    with _lock:
        return dict(_state[feature])


def get_all_states() -> dict:
    with _lock:
        return {name: dict(s) for name, s in _state.items()}


def format_summary(feature: str, state: dict) -> str:
    """Turns a feature's last_result dict into a short human-readable line for the status
    badge — each feature's result dict has slightly different keys, so this is the one
    place that knows how to read all three."""
    result = state.get("last_result")
    if result is None:
        return "No check has run yet."
    if result.get("skipped"):
        return "Disabled."

    checked = result.get("checked", 0)
    errors = result.get("errors", 0)
    error_part = f", {errors} error{'s' if errors != 1 else ''}" if errors else ""

    if feature == "updates":
        found = result.get("updates_found", 0)
        return (
            f"Last check: {checked} container{'s' if checked != 1 else ''} checked, "
            f"{found} new update{'s' if found != 1 else ''}{error_part}"
        )
    if feature == "logs":
        found = result.get("findings_found", 0)
        return (
            f"Last check: {checked} container{'s' if checked != 1 else ''} checked, "
            f"{found} finding{'s' if found != 1 else ''}{error_part}"
        )
    if feature == "compose":
        reviewed = result.get("reviewed", 0)
        found = result.get("findings_found", 0)
        return (
            f"Last check: {checked} file{'s' if checked != 1 else ''} checked, "
            f"{reviewed} reviewed, {found} finding{'s' if found != 1 else ''}{error_part}"
        )
    return "Last check complete."
