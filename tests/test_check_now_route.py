"""Regression test: Stage 2 only changed what happens inside reconcile.run_check() (making
the registry checks concurrent). It must not reopen the bug fixed after Stage 1 shipped,
where /updates/check-now blocked the HTTP response until the whole check finished. This
drives the real route through TestClient with a slow, mocked run_check to prove the request
still returns immediately and the real work happens in the background thread."""

import os
import time
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", "/tmp/rr-test-data")
os.environ.setdefault("COMPOSE_ROOT", "/tmp/rr-test-compose")
os.makedirs(os.environ["DATA_DIR"], exist_ok=True)
os.makedirs(os.environ["COMPOSE_ROOT"], exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import check_state  # noqa: E402


def _slow_run_check():
    time.sleep(0.5)
    return {"containers": [], "errors": 0, "checked_at": "2026-01-01T00:00:00+00:00"}


def test_check_now_returns_immediately_while_check_runs_in_background():
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}

    with TestClient(app) as client, \
         patch("app.main.reconcile.run_check", side_effect=_slow_run_check):
        start = time.monotonic()
        resp = client.post("/updates/check-now")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert elapsed < 0.4, f"check-now should return before the 0.5s check finishes, took {elapsed:.2f}s"

        # The background check should still be running right after the response comes back.
        assert check_state.get_state("updates")["running"] is True

        # Wait for the background thread to finish so it doesn't leak into other tests.
        for _ in range(20):
            if not check_state.get_state("updates")["running"]:
                break
            time.sleep(0.1)
        assert check_state.get_state("updates")["running"] is False
