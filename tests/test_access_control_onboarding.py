"""First-launch access-control onboarding: a real-world report that removing the Overview page's
old per-feature enable toggle (see the Overview redesign work) highlighted a related gap -- the
optional access-control gate (test_auth_gate.py) has no login by default, and nothing ever
actually asked a fresh install to make a deliberate choice about it. This modal is that ask: shown
once, on every page (see the auto-load slot in base.html), until the operator either sets a
username/password or explicitly opts out.

IMPORTANT test-isolation note: same as test_auth_gate.py -- the `client` fixture is session-scoped
and shared by the whole suite, so every test here must leave every access-control setting cleared
when it's done, or a leaked secret/onboarding flag will corrupt other test files' requests."""

import base64

from app import db

db.init_db()

_SECRET = "correct-horse-battery-staple"
_USERNAME = "operator"


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
# The modal fragment itself
# ---------------------------------------------------------------------------

def test_modal_renders_on_a_fresh_install_with_no_decision_made_yet(client):
    resp = client.get("/settings/access-control/onboarding-modal")
    assert resp.status_code == 200
    assert "onboarding-modal-overlay" in resp.text
    assert "Set up access control" in resp.text


def test_modal_renders_nothing_once_onboarding_is_explicitly_marked_done(client):
    db.set_auth_onboarding_done(True)
    resp = client.get("/settings/access-control/onboarding-modal")
    assert resp.status_code == 200
    assert resp.text == ""


def test_modal_renders_nothing_once_a_secret_is_already_configured(client):
    """Migration case: an install upgrading from before this modal existed already has a secret
    configured but never set the onboarding_done flag -- must not suddenly interrupt someone who
    made this decision long ago."""
    db.set_auth_secret(_SECRET)
    token = base64.b64encode(f"anyone:{_SECRET}".encode()).decode()
    resp = client.get("/settings/access-control/onboarding-modal", headers={"Authorization": f"Basic {token}"})
    assert resp.status_code == 200
    assert resp.text == ""


def test_modal_is_auto_loaded_from_every_page_via_base_html(client):
    text = client.get("/").text
    assert 'hx-get="/settings/access-control/onboarding-modal"' in text
    assert 'hx-trigger="load"' in text


# ---------------------------------------------------------------------------
# Both onboarding actions mark the decision as made
# ---------------------------------------------------------------------------

def test_enabling_credentials_marks_onboarding_done_and_hides_the_modal_afterward(client):
    assert db.get_auth_onboarding_done() is False
    resp = client.post("/settings/access-control/credentials", data={"username": _USERNAME, "secret": _SECRET})
    assert resp.json()["ok"] is True
    assert db.get_auth_onboarding_done() is True

    token = base64.b64encode(f"{_USERNAME}:{_SECRET}".encode()).decode()
    resp = client.get("/settings/access-control/onboarding-modal", headers={"Authorization": f"Basic {token}"})
    assert resp.text == ""


def test_disabling_marks_onboarding_done_and_hides_the_modal_afterward(client):
    assert db.get_auth_onboarding_done() is False
    resp = client.post("/settings/access-control/disable")
    assert resp.json()["ok"] is True
    assert db.get_auth_onboarding_done() is True

    resp = client.get("/settings/access-control/onboarding-modal")
    assert resp.text == ""


# ---------------------------------------------------------------------------
# The modal's own LAN-bypass control reuses the real settings toggle, not a separate one
# ---------------------------------------------------------------------------

def test_modal_includes_a_lan_bypass_toggle_posting_to_the_real_route(client):
    resp = client.get("/settings/access-control/onboarding-modal")
    assert '/settings/access-control/lan-bypass' in resp.text
