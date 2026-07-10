import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import db

FEATURES = ("updates", "logs", "compose")

_lock = threading.Lock()
_state = {name: {"running": False, "last_result": None, "last_run_at": None} for name in FEATURES}
# Only "updates" wires this up so far (Stage 2, extended Stage 6) — logs/compose never call
# set_progress, so their progress stays {"stage": None, "done": 0, "total": 0} and the status
# template's "if progress.total" guard means their rendered "Checking…" text is unaffected.
#
# "stage" names which phase of a multi-step check is running (e.g. "checking" vs
# "release_notes" as of Stage 6) so the status text can say what's actually happening instead
# of a single generic "Checking (N/M)" that silently freezes once a later phase with its own
# item count starts. Any future stage that adds another phase (Stage 7's AI summarization,
# most likely) MUST call set_progress with its own stage name and report its own progress the
# same way — a phase that never calls set_progress looks exactly like a hang, which is the
# bug this fixes.
_progress = {name: {"stage": None, "done": 0, "total": 0} for name in FEATURES}

# Item-scoped state for a single update's own "Reset & re-check" button (Stage 6) — separate
# from the feature-level state above on purpose: a scoped one-container recheck must not
# stomp the Updates page's "Last checked: N checked, M found" summary with "1 checked", and
# each update's button needs its own independent running/progress/done signal rather than
# sharing the single global one. Entries are transient (created when a recheck starts, removed
# once its poller has read the final "done" state) rather than tied to the fixed FEATURES list.
_item_state: dict[str, dict] = {}


def set_running(feature: str) -> None:
    with _lock:
        _state[feature]["running"] = True
        _progress[feature] = {"stage": None, "done": 0, "total": 0}


def try_start(feature: str) -> bool:
    """Atomically claims the "a check is running" slot for this feature if it's free, same
    shape as persist.try_start_updates_check() (kept there too, unchanged, since callers
    already depend on its exact name) -- returns True if this caller now owns it, False if a
    check was already in progress. Used directly by features (e.g. Logs' scoped stack-retry
    actions) that don't have their own persist.py-level wrapper of this."""
    with _lock:
        if _state[feature]["running"]:
            return False
        _state[feature]["running"] = True
        _progress[feature] = {"stage": None, "done": 0, "total": 0}
    return True


def release_running(feature: str) -> None:
    """Clears the running flag without touching last_result/last_run_at — for a caller (a
    scoped item-level recheck) that shares the same "only one check at a time" mutex as a
    full check but must not overwrite the full check's last summary with its own partial
    result. See set_finished() below for the full-check equivalent that does update it."""
    with _lock:
        _state[feature]["running"] = False
        _progress[feature] = {"stage": None, "done": 0, "total": 0}


def set_progress(feature: str, stage: str, done: int, total: int) -> None:
    with _lock:
        _progress[feature] = {"stage": stage, "done": done, "total": total}


def get_progress(feature: str) -> dict:
    with _lock:
        return dict(_progress[feature])


def set_finished(feature: str, result: dict) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    with _lock:
        _state[feature]["running"] = False
        _state[feature]["last_result"] = result
        _state[feature]["last_run_at"] = now_iso
        _progress[feature] = {"stage": None, "done": 0, "total": 0}
    # Persisted separately from the in-memory dict above so "last checked" survives a
    # container restart — the in-memory value is just a faster path while the process
    # is still alive.
    db.set_last_check_result(feature, result, now_iso)


def start_item(item_key: str, container_name: str) -> None:
    with _lock:
        _item_state[item_key] = {
            "running": True, "stage": None, "done": 0, "total": 0, "container_name": container_name,
        }


def set_item_progress(item_key: str, stage: str, done: int, total: int) -> None:
    with _lock:
        if item_key in _item_state:
            _item_state[item_key].update(stage=stage, done=done, total=total)


def finish_item(item_key: str) -> None:
    with _lock:
        if item_key in _item_state:
            _item_state[item_key]["running"] = False


def get_item_state(item_key: str) -> dict | None:
    with _lock:
        state = _item_state.get(item_key)
        return dict(state) if state else None


def clear_item(item_key: str) -> None:
    with _lock:
        _item_state.pop(item_key, None)


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


def _local_timestamp(iso_utc: str) -> str:
    """Converts a stored UTC ISO timestamp (every timestamp in the database is UTC — see
    db.now_iso()) into the configured TZ (db.get_timezone() — Stage 5c: the Settings page,
    seeded from the TZ env var on first boot) for display, as "HH:MM, DD Mon YYYY". Falls
    back to UTC if the configured TZ name isn't a real IANA zone rather than crashing the
    status line over it."""
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        local = dt.astimezone(ZoneInfo(db.get_timezone()))
    except ZoneInfoNotFoundError:
        local = dt.astimezone(timezone.utc)
    return local.strftime("%H:%M, %d %b %Y")


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
    error_part = f" • {errors} error{'s' if errors != 1 else ''}" if errors else ""

    last_run_at = state.get("last_run_at")
    when = _local_timestamp(last_run_at) if last_run_at else "unknown time"
    prefix = f"Last checked: {when}"

    if feature == "updates":
        found = result.get("updates_found", 0)
        return f"{prefix} • {checked} checked • {found} update{'s' if found != 1 else ''} found{error_part}"
    if feature == "logs":
        found = result.get("findings_found", 0)
        return f"{prefix} • {checked} checked • {found} finding{'s' if found != 1 else ''} found{error_part}"
    if feature == "compose":
        reviewed = result.get("reviewed", 0)
        found = result.get("findings_found", 0)
        return f"{prefix} • {checked} checked • {reviewed} reviewed • {found} finding{'s' if found != 1 else ''} found{error_part}"
    return f"{prefix} • complete{error_part}"
