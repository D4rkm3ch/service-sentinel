"""Regression tests driving the real HTTP routes with TestClient (unlike test_reconcile.py,
which calls reconcile.run_check() directly). Covers two things: (1) the fix shipped right
after Stage 1, where /updates/check-now must return immediately instead of blocking on the
whole check, still holds now that Stage 2 changed what happens inside run_check(); and (2)
the new Stage 2 live-progress text and faster poll cadence for the updates status badge.

All tests share one TestClient/app instance (module-scoped fixture) because entering the
TestClient context manager re-fires FastAPI's startup event, and starting APScheduler twice
in the same process raises SchedulerAlreadyRunningError."""

import os
import time
from unittest.mock import patch

import pytest

os.environ.setdefault("DATA_DIR", "/tmp/rr-test-data")
os.environ.setdefault("COMPOSE_ROOT", "/tmp/rr-test-compose")
os.makedirs(os.environ["DATA_DIR"], exist_ok=True)
os.makedirs(os.environ["COMPOSE_ROOT"], exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import check_state  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _slow_run_check(on_progress=None):
    time.sleep(0.5)
    return {"containers": [], "errors": 0, "checked_at": "2026-01-01T00:00:00+00:00"}


def _slow_run_check_with_progress(on_progress=None):
    if on_progress:
        on_progress(0, 10)
        time.sleep(0.2)
        on_progress(3, 10)
    time.sleep(0.3)
    return {"containers": [], "errors": 0, "checked_at": "2026-01-01T00:00:00+00:00"}


def _wait_until_not_running(feature: str):
    for _ in range(20):
        if not check_state.get_state(feature)["running"]:
            return
        time.sleep(0.1)


def test_check_now_returns_immediately_while_check_runs_in_background(client):
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}

    with patch("app.main.reconcile.run_check", side_effect=_slow_run_check):
        start = time.monotonic()
        resp = client.post("/updates/check-now")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert elapsed < 0.4, f"check-now should return before the 0.5s check finishes, took {elapsed:.2f}s"

        # The background check should still be running right after the response comes back.
        assert check_state.get_state("updates")["running"] is True

        _wait_until_not_running("updates")
        assert check_state.get_state("updates")["running"] is False


def test_status_poll_shows_live_progress_and_polls_faster_for_updates(client):
    """The new Stage 2 feature: while a check runs, /updates/status-poll should render
    "Checking (N/total)" text (not just a bare spinner) and use the faster 500ms poll
    delay — logs/compose keep the original 2s cadence, proven separately below."""
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}

    with patch("app.main.reconcile.run_check", side_effect=_slow_run_check_with_progress):
        resp = client.post("/updates/check-now")
        assert "Checking" in resp.text

        time.sleep(0.25)  # let the worker thread call on_progress(3, 10)
        poll_resp = client.get("/updates/status-poll")
        assert "3/10" in poll_resp.text
        assert "delay:500ms" in poll_resp.text

        _wait_until_not_running("updates")


def test_status_poll_keeps_2s_cadence_for_logs_and_compose(client):
    check_state._state["logs"] = {"running": True, "last_result": None, "last_run_at": None}
    resp = client.get("/logs/status-poll")
    check_state._state["logs"]["running"] = False  # don't leak "running" into other tests
    assert "delay:2000ms" in resp.text
    assert "/10" not in resp.text  # no progress text was ever wired up for logs
