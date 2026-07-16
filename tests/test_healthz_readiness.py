"""Reliability hardening (test_improvement_plan.md section 6): /healthz was a bare liveness
check ({"status": "ok"}, unconditionally) with no readiness signal -- a container that was up
but couldn't reach its own database, or whose Docker socket wasn't mounted, still reported
healthy to the Dockerfile's HEALTHCHECK. Now checks both and returns 503 on a degraded state
(what actually flips Docker's health status to unhealthy), with per-dependency detail in the
body. The Docker socket check is a cheap existence test, not a live API ping -- see the route's
own docstring."""

from unittest.mock import patch

from app import db

db.init_db()


def test_healthz_reports_ok_when_everything_is_reachable(client):
    """In the test environment the SQLite database is real (conftest sets DATA_DIR) and
    DOCKER_SOCKET defaults to the unix path -- which doesn't exist here, so patch the existence
    check to isolate 'everything healthy'."""
    with patch("app.main.os.path.exists", return_value=True):
        resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["database"] == "ok"
    assert data["docker_socket"] == "ok"


def test_healthz_reports_degraded_with_503_when_the_database_is_unreachable(client):
    """Patches main._database_reachable rather than db.get_conn wholesale -- the auth gate
    middleware reads db.get_auth_secret() on this same request, so killing every db connection
    would crash that before the route even ran (see _database_reachable's own docstring)."""
    with patch("app.main._database_reachable", return_value=False), \
         patch("app.main.os.path.exists", return_value=True):
        resp = client.get("/healthz")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["database"] == "unreachable"


def test_healthz_reports_degraded_with_503_when_the_docker_socket_is_missing(client):
    with patch("app.main.os.path.exists", return_value=False):
        resp = client.get("/healthz")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["docker_socket"] == "missing"


def test_healthz_treats_a_tcp_docker_transport_as_ok_without_probing(client):
    """A DOCKER_SOCKET override pointing at tcp:// has no local socket file to stat -- reported
    ok rather than failing the probe on a check this route can't cheaply make."""
    with patch("app.main.settings.docker_socket", "tcp://192.168.1.50:2375"):
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["docker_socket"] == "ok"


def test_healthz_stays_reachable_without_credentials_when_auth_gate_is_on(client):
    """Already covered in test_auth_gate.py, re-asserted here since this file owns /healthz's
    behavior contract: the readiness rework must not have broken the auth exemption."""
    db.set_auth_secret("some-shared-password")
    try:
        with patch("app.main.os.path.exists", return_value=True):
            resp = client.get("/healthz")
        assert resp.status_code == 200
    finally:
        db.clear_auth_secret()
