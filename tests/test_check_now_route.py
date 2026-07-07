"""Regression tests driving the real HTTP routes with TestClient (unlike test_reconcile.py,
which calls reconcile.run_check() directly). Covers two things: (1) the fix shipped right
after Stage 1, where /updates/check-now must return immediately instead of blocking on the
whole check, still holds now that Stage 2 changed what happens inside run_check() and Stage 3
wrapped it in persist.run_and_persist_check(); and (2) the Stage 2 live-progress text and
faster poll cadence for the updates status badge.

Uses the shared `client` fixture from conftest.py rather than opening its own TestClient —
see that file's docstring for why."""

import time
from unittest.mock import patch

from app import check_state


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

    with patch("app.main.persist.run_and_persist_check", side_effect=_slow_run_check):
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

    with patch("app.main.persist.run_and_persist_check", side_effect=_slow_run_check_with_progress):
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
