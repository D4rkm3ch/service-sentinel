"""base.html polls /updates|logs|compose/running-state once a second, indefinitely, from every
open tab -- and each poll only ever needs one in-memory boolean. These previously went through
check_state.get_state(), whose last-result fallback reads the DATABASE whenever a feature
hasn't completed a check since the process started (which, with daily schedules, can be all
day): three SQLite reads per second of pure idle overhead. Pins the fix: the polls use the
lock-only is_running(), and get_state() caches the persisted fallback after its first read so
the status-badge polls stop re-reading the DB too."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app import check_state, db
from app.main import app

db.init_db()

client = TestClient(app)


def _clear_in_memory_last_result(feature: str):
    with check_state._lock:
        check_state._state[feature]["last_result"] = None
        check_state._state[feature]["last_run_at"] = None


def test_running_state_endpoints_never_touch_the_db():
    for feature in check_state.FEATURES:
        _clear_in_memory_last_result(feature)

    with patch("app.check_state.db.get_last_check_result",
               side_effect=AssertionError("running-state poll read the DB")) as mock_read:
        for feature in check_state.FEATURES:
            resp = client.get(f"/{feature}/running-state")
            assert resp.status_code == 200
            assert resp.json() == {"running": False}
    assert mock_read.call_count == 0


def test_get_state_reads_the_persisted_fallback_once_then_caches_it():
    db.set_last_check_result("updates", {"checked": 3, "updates_found": 1, "errors": 0}, db.now_iso())
    _clear_in_memory_last_result("updates")

    with patch("app.check_state.db.get_last_check_result",
               wraps=db.get_last_check_result) as mock_read:
        first = check_state.get_state("updates")
        second = check_state.get_state("updates")

    assert first["last_result"] == {"checked": 3, "updates_found": 1, "errors": 0}
    assert second["last_result"] == first["last_result"]
    assert mock_read.call_count == 1, "the persisted fallback should be cached after one read"


def test_a_finish_during_the_fallback_read_is_not_clobbered_by_the_older_value():
    """The cache write is guarded: if a check finishes (set_finished) between the fallback DB
    read and the cache write, the fresh result must win over the older persisted one."""
    db.set_last_check_result("updates", {"checked": 1, "updates_found": 0, "errors": 0}, db.now_iso())
    _clear_in_memory_last_result("updates")

    fresh = {"checked": 9, "updates_found": 2, "errors": 0}

    real_read = db.get_last_check_result

    def read_then_finish(feature):
        result = real_read(feature)
        check_state.set_finished("updates", fresh)  # lands between the read and the cache write
        return result

    with patch("app.check_state.db.get_last_check_result", side_effect=read_then_finish):
        check_state.get_state("updates")

    assert check_state.get_state("updates")["last_result"] == fresh
