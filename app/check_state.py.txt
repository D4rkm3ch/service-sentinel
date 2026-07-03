import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_state = {"running": False, "last_result": None, "last_run_at": None}


def set_running() -> None:
    with _lock:
        _state["running"] = True


def set_finished(result: dict) -> None:
    with _lock:
        _state["running"] = False
        _state["last_result"] = result
        _state["last_run_at"] = datetime.now(timezone.utc).isoformat()


def get_state() -> dict:
    with _lock:
        return dict(_state)