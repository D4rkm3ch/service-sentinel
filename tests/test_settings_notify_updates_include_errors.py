"""Stage 10: the "also notify on registry check errors" toggle -- the DB getter/setter and the
Settings page route that saves it. Everything else in the Notifications panel already existed
before this stage; this is the one new control introduced alongside wiring up real Updates
notifications (see tests/test_notify_update.py and tests/test_persist.py for the notification
logic itself)."""

import pytest

from app import db


@pytest.fixture(autouse=True)
def clean_setting():
    db.set_notify_updates_include_errors(False)
    yield
    db.set_notify_updates_include_errors(False)


def test_defaults_to_off():
    assert db.get_notify_updates_include_errors() is False


def test_setter_round_trips():
    db.set_notify_updates_include_errors(True)
    assert db.get_notify_updates_include_errors() is True
    db.set_notify_updates_include_errors(False)
    assert db.get_notify_updates_include_errors() is False


def _toggle_snippet(html: str) -> str:
    idx = html.index('id="notify_updates_include_errors"')
    return html[idx - 40:idx + 120]


def test_settings_page_reflects_the_current_value(client):
    page = client.get("/settings")
    assert 'id="notify_updates_include_errors"' in page.text
    assert "checked" not in _toggle_snippet(page.text)

    db.set_notify_updates_include_errors(True)
    page = client.get("/settings")
    assert "checked" in _toggle_snippet(page.text)


def test_route_saves_enabled(client):
    resp = client.post("/settings/notify/updates-include-errors", data={"enabled": "on"})
    assert resp.status_code == 200
    assert db.get_notify_updates_include_errors() is True


def test_route_saves_disabled_when_field_omitted(client):
    db.set_notify_updates_include_errors(True)
    resp = client.post("/settings/notify/updates-include-errors", data={})
    assert resp.status_code == 200
    assert db.get_notify_updates_include_errors() is False
