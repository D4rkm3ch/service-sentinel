"""The per-feature "use general schedule" toggle used to be backwards from what a real user
expected: checked meant "using the general schedule" (the default, unconfigured state), so a
fresh install showed every feature's toggle already ON for a state that isn't really an
explicit choice. Flipped so unchecked (the default) means "using general schedule" and checked
means "using my own schedule" -- checking a toggle should mean turning something on, not
turning the default off. The underlying stored flag (use_master, True = defers to general) is
unchanged; only the checkbox's mapping to it, and the route that writes it, are inverted."""

import pytest

from app import db


@pytest.fixture(autouse=True)
def reset_schedule_overrides():
    db.set_feature_uses_master_schedule("updates", True)
    db.set_feature_uses_master_schedule("logs", True)
    yield
    db.set_feature_uses_master_schedule("updates", True)
    db.set_feature_uses_master_schedule("logs", True)


def test_a_fresh_feature_defaults_to_using_the_general_schedule_and_the_checkbox_is_unchecked(client):
    resp = client.get("/settings")
    assert db.get_feature_uses_master_schedule("updates") is True
    start = resp.text.index('id="updates_use_own_sched"')
    end = resp.text.index(">", start)
    tag = resp.text[start:end]
    # 'value="on" checked' is the literal output of the macro's own checked branch -- must not
    # be confused with the unrelated substring "checked" inside the onchange="...this.checked)"
    # handler, which is always present regardless of state.
    assert 'value="on" checked' not in tag
    assert "Using general schedule" in resp.text


def test_posting_the_toggle_on_switches_to_the_features_own_schedule(client):
    resp = client.post("/settings/schedule/use-master/updates", data={"enabled": "on"})
    assert resp.status_code == 200
    assert db.get_feature_uses_master_schedule("updates") is False


def test_posting_the_toggle_off_switches_back_to_the_general_schedule(client):
    client.post("/settings/schedule/use-master/updates", data={"enabled": "on"})
    resp = client.post("/settings/schedule/use-master/updates", data={})
    assert resp.status_code == 200
    assert db.get_feature_uses_master_schedule("updates") is True


def test_settings_page_checks_the_box_and_shows_own_schedule_label_once_using_own_schedule(client):
    client.post("/settings/schedule/use-master/logs", data={"enabled": "on"})
    resp = client.get("/settings")
    start = resp.text.index('id="logs_use_own_sched"')
    end = resp.text.index(">", start)
    assert 'value="on" checked' in resp.text[start:end]
    assert "Using own schedule" in resp.text
