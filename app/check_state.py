import threading
from datetime import datetime, timezone

from app import db

FEATURES = ("updates", "logs", "compose")

_lock = threading.Lock()
_state = {name: {"running": False, "last_result": None, "last_run_at": None} for name in FEATURES}
# Only "updates" wires this up so far (Stage 2) — logs/compose never call set_progress, so
# their progress stays {"done": 0, "total": 0} and the status template's "if progress.total"
# guard means their rendered "Checking…" text is unaffected.
_progress = {name: {"done": 0, "total": 0} for name in FEATURES}


def set_running(feature: str) -> None:
    with _lock:
        _state[feature]["running"] = True
        _progress[feature] = {"done": 0, "total": 0}


def set_progress(feature: str, done: int, total: int) -> None:
    with _lock:
        _progress[feature] = {"done": done, "total": total}


def get_progress(feature: str) -> dict:
    with _lock:
        return dict(_progress[feature])


def set_finished(feature: str, result: dict) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    with _lock:
        _state[feature]["running"] = False
        _state[feature]["last_result"] = result
        _state[feature]["last_run_at"] = now_iso
        _progress[feature] = {"done": 0, "total": 0}
    # Persisted separately from the in-memory dict above so "last checked" survives a
    # container restart — the in-memory value is just a faster path while the process
    # is still alive.
    db.set_last_check_result(feature, result, now_iso)


def get_state(feature: str) -> dict:
    with _lock:
        state = dict(_state[feature])
    if state["last_result"] is None:
        persisted = db.get_last_check_result(feature)
        if persisted:
            state["last_result"] = persisted.get("result")
            state["last_run_at"] = persisted.get("at")
    return state


def get_all_states() -> dict:
    return {name: get_state(name) for name in FEATURES}


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
