"""Security hardening: there was no authentication of any kind anywhere in the app
(security_hardening_plan.md finding #2) -- every route, including ones that trigger checks that
spend real AI provider budget, silence findings, or read configured-key state, was reachable by
anyone who could reach the port. Fixed with an optional shared username+password gate
(AuthGateMiddleware in main.py): off by default (db.get_auth_secret() empty), on automatically
the moment an operator sets credentials in Settings (or via the first-launch onboarding modal --
see test_access_control_onboarding.py), no restart required either way. HTTP Basic Auth
specifically, since it's the one auth scheme browsers handle entirely natively (prompt, cache,
auto-attach to every subsequent request including htmx's own), with no session/cookie/CSRF
surface to build.

A later round added the username half (previously password-only, any username was accepted) plus
an opt-in LAN bypass, so this file also covers: username matching, the backward-compatibility
path for installs that configured a password before the username field existed, and the bypass.

IMPORTANT test-isolation note: the `client` fixture is session-scoped and shared by the entire
test suite. Every test here MUST leave every access-control setting cleared when it's done (see
setup_function/teardown_function below) -- an accidentally-leaked secret would make every other
test file's unauthenticated requests start failing with 401s."""

import base64

from app import db, main

db.init_db()

_SECRET = "correct-horse-battery-staple"
_USERNAME = "operator"


def _basic_header(password: str, username: str = "anyone") -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _clear_all():
    db.clear_auth_secret()
    db.set_auth_username("")
    db.set_auth_lan_bypass(False)
    db.set_auth_onboarding_done(False)


def setup_function(_):
    _clear_all()


def teardown_function(_):
    _clear_all()


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
    resp = client.get("/")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate", "").lower().startswith("basic")


def test_correct_password_is_accepted_when_no_username_is_configured_yet(client):
    """Backward compatibility: an install that configured a password before the username field
    existed has db.get_auth_username() == "" -- must keep accepting any username, exactly like
    before, rather than suddenly rejecting every request."""
    db.set_auth_secret(_SECRET)
    resp = client.get("/", headers=_basic_header(_SECRET, username="literally-anything"))
    assert resp.status_code == 200


def test_wrong_password_is_rejected(client):
    db.set_auth_secret(_SECRET)
    resp = client.get("/", headers=_basic_header("wrong-password"))
    assert resp.status_code == 401


def test_missing_authorization_header_is_rejected(client):
    db.set_auth_secret(_SECRET)
    resp = client.get("/")
    assert resp.status_code == 401


def test_malformed_authorization_header_is_rejected_not_a_500(client):
    db.set_auth_secret(_SECRET)
    resp = client.get("/", headers={"Authorization": "Basic not-valid-base64!!!"})
    assert resp.status_code == 401


def test_non_basic_auth_scheme_is_rejected(client):
    db.set_auth_secret(_SECRET)
    resp = client.get("/", headers={"Authorization": "Bearer " + _SECRET})
    assert resp.status_code == 401


def test_gate_applies_to_every_ordinary_route_not_just_the_homepage(client):
    db.set_auth_secret(_SECRET)
    for path in ("/settings", "/updates", "/logs", "/compose"):
        resp = client.get(path)
        assert resp.status_code == 401, f"{path} should have required auth"
        resp = client.get(path, headers=_basic_header(_SECRET))
        assert resp.status_code == 200, f"{path} should have accepted the correct password"


# ---------------------------------------------------------------------------
# Username matching, once a username is actually configured
# ---------------------------------------------------------------------------

def test_correct_username_and_password_is_accepted_once_a_username_is_configured(client):
    db.set_auth_secret(_SECRET)
    db.set_auth_username(_USERNAME)
    resp = client.get("/", headers=_basic_header(_SECRET, username=_USERNAME))
    assert resp.status_code == 200


def test_correct_password_wrong_username_is_rejected_once_a_username_is_configured(client):
    db.set_auth_secret(_SECRET)
    db.set_auth_username(_USERNAME)
    resp = client.get("/", headers=_basic_header(_SECRET, username="someone-else"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /healthz stays exempt
# ---------------------------------------------------------------------------

def test_healthz_is_reachable_without_credentials_even_when_the_gate_is_on(client):
    """A container orchestrator's liveness probe has no way to supply a credential and
    shouldn't need one just to confirm the process is up. Asserts not-401 rather than a
    specific success code: /healthz is a real readiness check now (see
    test_healthz_readiness.py), and in this test environment the Docker socket legitimately
    doesn't exist, so it reports 503-degraded -- which is still 'reached the route without
    credentials', the only thing THIS test is about."""
    db.set_auth_secret(_SECRET)
    resp = client.get("/healthz")
    assert resp.status_code != 401
    assert resp.json()["status"] in ("ok", "degraded")


# ---------------------------------------------------------------------------
# LAN bypass
# ---------------------------------------------------------------------------

def test_lan_bypass_off_by_default_a_private_address_still_needs_credentials(client, monkeypatch):
    monkeypatch.setattr(main.AuthGateMiddleware, "_is_lan_client", staticmethod(lambda scope: True))
    db.set_auth_secret(_SECRET)
    resp = client.get("/")
    assert resp.status_code == 401


def test_lan_bypass_enabled_skips_credentials_for_a_private_address(client, monkeypatch):
    monkeypatch.setattr(main.AuthGateMiddleware, "_is_lan_client", staticmethod(lambda scope: True))
    db.set_auth_secret(_SECRET)
    db.set_auth_lan_bypass(True)
    resp = client.get("/")
    assert resp.status_code == 200


def test_lan_bypass_enabled_still_requires_credentials_for_a_non_private_address(client, monkeypatch):
    monkeypatch.setattr(main.AuthGateMiddleware, "_is_lan_client", staticmethod(lambda scope: False))
    db.set_auth_secret(_SECRET)
    db.set_auth_lan_bypass(True)
    resp = client.get("/")
    assert resp.status_code == 401
    resp = client.get("/", headers=_basic_header(_SECRET))
    assert resp.status_code == 200


def test_is_lan_client_recognizes_private_ranges_and_rejects_public_ones():
    """Unit-level: the TestClient always reports its own peer as the literal string
    "testclient", not a real IP (see conftest.py), so the end-to-end tests above stub this
    method out rather than exercising the real IP parsing -- this test covers that parsing
    directly instead."""
    is_lan = main.AuthGateMiddleware._is_lan_client
    assert is_lan({"client": ("192.168.1.5", 12345)}) is True
    assert is_lan({"client": ("10.0.0.1", 12345)}) is True
    assert is_lan({"client": ("172.16.0.1", 12345)}) is True
    assert is_lan({"client": ("127.0.0.1", 12345)}) is True
    assert is_lan({"client": ("::1", 12345)}) is True
    assert is_lan({"client": ("8.8.8.8", 12345)}) is False
    assert is_lan({"client": ("1.1.1.1", 12345)}) is False
    assert is_lan({"client": ("testclient", 50000)}) is False
    assert is_lan({"client": None}) is False
    assert is_lan({}) is False


# ---------------------------------------------------------------------------
# Removing the secret turns the gate back off
# ---------------------------------------------------------------------------

def test_clearing_the_secret_turns_the_gate_back_off(client):
    db.set_auth_secret(_SECRET)
    db.clear_auth_secret()
    resp = client.get("/")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Settings routes that manage the credentials
# ---------------------------------------------------------------------------

def test_save_credentials_route_rejects_a_too_short_secret(client):
    resp = client.post("/settings/access-control/credentials", data={"username": _USERNAME, "secret": "short"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert db.get_auth_secret() == ""


def test_save_credentials_route_rejects_an_empty_username(client):
    resp = client.post("/settings/access-control/credentials", data={"username": "  ", "secret": _SECRET})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert db.get_auth_secret() == ""


def test_save_credentials_route_accepts_and_persists_a_valid_pair(client):
    resp = client.post("/settings/access-control/credentials", data={"username": _USERNAME, "secret": _SECRET})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert db.get_auth_secret() == _SECRET
    assert db.get_auth_username() == _USERNAME


def test_save_credentials_route_marks_onboarding_done(client):
    assert db.get_auth_onboarding_done() is False
    client.post("/settings/access-control/credentials", data={"username": _USERNAME, "secret": _SECRET})
    assert db.get_auth_onboarding_done() is True


def test_save_credentials_route_immediately_turns_the_gate_on_for_the_very_next_request(client):
    client.post("/settings/access-control/credentials", data={"username": _USERNAME, "secret": _SECRET})
    resp = client.get("/")
    assert resp.status_code == 401


def test_disable_route_clears_credentials_lan_bypass_and_marks_onboarding_done(client):
    db.set_auth_secret(_SECRET)
    db.set_auth_username(_USERNAME)
    db.set_auth_lan_bypass(True)
    resp = client.post(
        "/settings/access-control/disable", headers=_basic_header(_SECRET, username=_USERNAME),
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert db.get_auth_secret() == ""
    assert db.get_auth_username() == ""
    assert db.get_auth_lan_bypass() is False
    assert db.get_auth_onboarding_done() is True


def test_lan_bypass_route_toggles_the_setting(client):
    resp = client.post("/settings/access-control/lan-bypass", data={"enabled": "on"})
    assert resp.status_code == 200
    assert db.get_auth_lan_bypass() is True

    resp = client.post("/settings/access-control/lan-bypass", data={})
    assert resp.status_code == 200
    assert db.get_auth_lan_bypass() is False


def test_settings_page_reflects_configured_state(client):
    page = client.get("/settings")
    assert "Access Control" in page.text
    assert 'id="auth_secret_remove_btn"' not in page.text  # not configured yet -- no Disable button

    db.set_auth_secret(_SECRET)
    db.set_auth_username(_USERNAME)
    page = client.get("/settings", headers=_basic_header(_SECRET, username=_USERNAME))
    assert 'id="auth_secret_remove_btn"' in page.text
    assert "✓ Enabled" in page.text
    assert _USERNAME in page.text
