"""Stage 4: hardens the background job Stage 1's real-world fixes already introduced, rather
than building it fresh. Two things needed proving:

1. A check that fails partway through must never leave check_state stuck "running" forever --
   this is the exact failure class ("ran all night and was still checking") that triggered the
   whole ground-up rebuild, so it gets a direct regression test rather than just a code review.
2. Now that Stage 3 added real SQLite writes to the background thread, concurrent reads from
   other request-handling threads (page loads, polling) must not crash or deadlock against it.

Also covers the webhook's new check_state integration: it shares the same "running" flag as
the UI (so the two can't run concurrently) and gets the same exception safety net.
"""

import threading
import time
from unittest.mock import patch

import pytest

from app import check_state, db


def _wait_until_not_running(feature: str = "updates", timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not check_state.get_state(feature)["running"]:
            return
        time.sleep(0.05)
    raise AssertionError(f"{feature} still running after {timeout}s")


@pytest.fixture(autouse=True)
def clean_state():
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    db.reset_updates_data()
    yield
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    db.reset_updates_data()


def test_check_now_recovers_from_a_failing_check_instead_of_getting_stuck(client):
    """The critical regression test: if persist.run_and_persist_check() blows up, running
    must still end up False -- not stuck forever with no way to trigger a new check."""
    with patch("app.main.persist.run_and_persist_check", side_effect=RuntimeError("boom")):
        resp = client.post("/updates/check-now")
        assert resp.status_code == 200
        _wait_until_not_running()

    assert check_state.get_state("updates")["running"] is False
    # And a fresh click must actually be able to start a new check -- not silently refused
    # because the guard still thinks the old one is running.
    with patch("app.main.persist.run_and_persist_check", return_value={"containers": [], "errors": 0, "checked_at": "x"}) as mock_run:
        resp = client.post("/updates/check-now")
        assert resp.status_code == 200
        _wait_until_not_running()
        mock_run.assert_called_once()


def test_webhook_shares_the_running_flag_and_skips_when_a_check_is_already_active(client):
    release_event = threading.Event()

    def slow_check(on_progress=None):
        release_event.wait(timeout=2)
        return {"containers": [], "errors": 0, "checked_at": "x"}

    with patch("app.main.persist.run_and_persist_check", side_effect=slow_check):
        client.post("/updates/check-now")
        assert check_state.get_state("updates")["running"] is True

        # A webhook fired while the UI-triggered check is still running must be skipped, not
        # run a second, overlapping check.
        with patch("app.webhook.persist.run_and_persist_check") as webhook_run:
            resp = client.post("/webhook/dockhand?token=test-token")
            assert resp.status_code == 200
            assert resp.json()["status"].startswith("skipped")
            webhook_run.assert_not_called()

        release_event.set()
        _wait_until_not_running()


def test_webhook_clears_running_even_when_the_check_raises(client):
    # TestClient re-raises unhandled exceptions into the caller by default (rather than
    # turning them into a 500 response, which is what a real deployment would return to
    # Dockhand) -- the thing under test here is state cleanup, not the HTTP status code.
    with patch("app.webhook.persist.run_and_persist_check", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            client.post("/webhook/dockhand?token=test-token")

    # The failure must not leave the flag stuck -- a UI check right after must be able to run.
    assert check_state.get_state("updates")["running"] is False
    with patch("app.main.persist.run_and_persist_check", return_value={"containers": [], "errors": 0, "checked_at": "x"}) as mock_run:
        client.post("/updates/check-now")
        _wait_until_not_running()
        mock_run.assert_called_once()


def test_concurrent_reads_survive_while_persist_writes_in_the_background():
    """Stage 3 put real SQLite writes on the background thread; this proves concurrent reads
    from other threads (simulating page loads / polling while a check runs) don't crash or
    deadlock against it -- the whole point of WAL mode, verified under real thread contention
    rather than assumed."""
    stop = threading.Event()
    read_errors = []

    def reader_loop():
        while not stop.is_set():
            try:
                db.list_tracked_containers_with_status()
            except Exception as exc:  # pragma: no cover - failure path under test
                read_errors.append(exc)
                return

    readers = [threading.Thread(target=reader_loop, daemon=True) for _ in range(4)]
    for t in readers:
        t.start()

    from app import persist

    outcome = {
        "containers": [
            {
                "container_name": f"c{i}", "image_repo": f"owner/repo{i}", "tag": "latest",
                "status": "update_available", "current_digest": "sha256:old", "latest_digest": "sha256:new",
            }
            for i in range(40)
        ],
        "errors": 0, "checked_at": "x",
    }
    persist.persist_check_outcome(outcome)

    stop.set()
    for t in readers:
        t.join(timeout=2)

    assert read_errors == []
    assert len(db.list_tracked_containers_with_status()) == 40
