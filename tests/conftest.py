"""Shared pytest setup. Sets DATA_DIR/COMPOSE_ROOT before any test module imports app.config
(env vars are read once at import time — see app/config.py — so whichever import happens
first for the whole test session wins; centralizing it here instead of duplicating it per
file avoids that turning into an accidental footgun).

Also provides a single session-scoped TestClient/app fixture. Entering the TestClient context
manager fires FastAPI's startup event, which starts APScheduler; starting it twice in the same
process raises SchedulerAlreadyRunningError, so every test file that needs a live app must
share this one fixture rather than opening its own.
"""

import os

os.environ.setdefault("DATA_DIR", "/tmp/rr-test-data")
os.environ.setdefault("COMPOSE_ROOT", "/tmp/rr-test-compose")
os.makedirs(os.environ["DATA_DIR"], exist_ok=True)
os.makedirs(os.environ["COMPOSE_ROOT"], exist_ok=True)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as _main  # noqa: E402
from app.main import app  # noqa: E402

# RateLimitMiddleware's real, wall-clock-timed per-IP window is fundamentally incompatible with
# a test suite that legitimately calls the same check-triggering routes many times in quick
# succession from what TestClient always reports as one single client identity ("testclient") --
# see main.py's own comment on RATE_LIMITING_ENABLED for why this is a suite-wide bypass rather
# than a real rate limit tuned to tolerate tests. The limiter logic itself is still exercised
# directly (not through the full HTTP stack) by test_rate_limiting.py, which flips this back on
# for its own narrow scope and restores it afterward.
_main.RATE_LIMITING_ENABLED = False


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
