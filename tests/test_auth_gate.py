"""Security hardening: there was no authentication of any kind anywhere in the app
(security_hardening_plan.md finding #2) -- every route, including ones that trigger checks that
spend real AI provider budget, silence findings, or read configured-key state, was reachable by
anyone who could reach the port. Fixed with an optional single shared-secret gate
(AuthGateMiddleware in main.py): off by default (db.get_auth_secret() empty), on automatically
the moment an operator sets one in Settings, no restart required either way. HTTP Basic Auth
specifically, since it's the one auth scheme browsers handle entirely natively (prompt, cache,
auto-attach to every subsequent request including htmx's own), with no session/cookie/CSRF
surface to build.

IMPORTANT test-isolation note: the `client` fixture is session-scoped and shared by the entire
test suite. Every test here MUST leave db.get_auth_secret() cleared when it's done (see
setup_function/teardown_function below) -- an accidentally-leaked secret would make every other
test file's unauthenticated requests start failing with 401s."""

import base64

from app import db

db.init_db()

_SECRET = "correct-horse-battery-staple"


def _basic_header(password: str, username: str = "anyone") -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def setup_function(_):
    db.clear_auth_secret()


def teardown_function(_):
    db.clear_auth_secret()


# ---------------------------------------------------------------------------
# Gate off by default
# ---------------------------------------------------------------------------

def test_gate_is_off_by_default_no_secret_configured(client):
    assert db.get_auth_secret() == ""
    resp = client.get("/")
    assert resp.status_code == 200


def test_no_authorization_header_needed_when_gate_is_off(client):
    resp = client.get("/settings")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Gate on once a secret is set
# ---------------------------------------------------------------------------

def test_setting_a_secret_turns_the_gate_on(client):
    db.set_auth_secret(_SECRET)
    try:
        resp = client.get("/")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate", "").lower().startswith("basic")
    finally:
        db.clear_auth_secret()


def test_correct_password_is_accepted_username_is_not_checked(client):
    db.set_auth_secret(_SECRET)
    try:
        resp = client.get("/", headers=_basic_header(_SECRET, username="literally-anything"))
        assert resp.status_code == 200
    finally:
        db.clear_auth_secret()


def test_wrong_password_is_rejected(client):
    db.set_auth_secret(_SECRET)
    try:
        resp = client.get("/", headers=_basic_header("wrong-password"))
        assert resp.status_code == 401
    finally:
        db.clear_auth_secret()


def test_missing_authorization_header_is_rejected(client):
    db.set_auth_secret(_SECRET)
    try:
        resp = client.get("/")
        assert resp.status_code == 401
    finally:
        db.clear_auth_secret()


def test_malformed_authorization_header_is_rejected_not_a_500(client):
    db.set_auth_secret(_SECRET)
    try:
        resp = client.get("/", headers={"Authorization": "Basic not-valid-base64!!!"})
        assert resp.status_code == 401
    finally:
        db.clear_auth_secret()


def test_non_basic_auth_scheme_is_rejected(client):
    db.set_auth_secret(_SECRET)
    try:
        resp = client.get("/", headers={"Authorization": "Bearer " + _SECRET})
        assert resp.status_code == 401
    finally:
        db.clear_auth_secret()


def test_gate_applies_to_every_ordinary_route_not_just_the_homepage(client):
    db.set_auth_secret(_SECRET)
    try:
        for path in ("/settings", "/updates", "/logs", "/compose"):
            resp = client.get(path)
            assert resp.status_code == 401, f"{path} should have required auth"
            resp = client.get(path, headers=_basic_header(_SECRET))
            assert resp.status_code == 200, f"{path} should have accepted the correct password"
    finally:
        db.clear_auth_secret()


# ---------------------------------------------------------------------------
# /healthz stays exempt
# ---------------------------------------------------------------------------

def test_healthz_is_reachable_without_credentials_even_when_the_gate_is_on(client):
    """A container orchestrator's liveness probe has no way to supply a credential and
    shouldn't need one just to confirm the process is up."""
    db.set_auth_secret(_SECRET)
    try:
        resp = client.get("/healthz")
        assert resp.status_code == 200
    finally:
        db.clear_auth_secret()


# ---------------------------------------------------------------------------
# Removing the secret turns the gate back off
# ---------------------------------------------------------------------------

def test_clearing_the_secret_turns_the_gate_back_off(client):
    db.set_auth_secret(_SECRET)
    db.clear_auth_secret()
    resp = client.get("/")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Settings routes that manage the secret
# ---------------------------------------------------------------------------

def test_save_auth_secret_route_rejects_a_too_short_secret(client):
    resp = client.post("/settings/auth-secret", data={"secret": "short"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert db.get_auth_secret() == ""


def test_save_auth_secret_route_accepts_and_persists_a_valid_secret(client):
    try:
        resp = client.post("/settings/auth-secret", data={"secret": _SECRET})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert db.get_auth_secret() == _SECRET
    finally:
        db.clear_auth_secret()


def test_save_auth_secret_route_immediately_turns_the_gate_on_for_the_very_next_request(client):
    try:
        client.post("/settings/auth-secret", data={"secret": _SECRET})
        resp = client.get("/")
        assert resp.status_code == 401
    finally:
        db.clear_auth_secret()


def test_remove_auth_secret_route_clears_it(client):
    db.set_auth_secret(_SECRET)
    resp = client.post(
        "/settings/auth-secret/remove", headers=_basic_header(_SECRET),
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert db.get_auth_secret() == ""


def test_settings_page_reflects_configured_state(client):
    try:
        page = client.get("/settings")
        assert "Access Control" in page.text
        assert 'id="auth_secret_remove_btn"' not in page.text  # not configured yet -- no Remove button

        db.set_auth_secret(_SECRET)
        page = client.get("/settings", headers=_basic_header(_SECRET))
        assert 'id="auth_secret_remove_btn"' in page.text
        assert "✓ Enabled" in page.text
    finally:
        db.clear_auth_secret()
