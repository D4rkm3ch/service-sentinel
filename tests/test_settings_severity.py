"""Notification severity settings: a general/master severity with per-feature "use general"
overrides used to exist here -- removed per a real-world report that it was actively
misleading, since a feature quietly following the general value still showed its OWN
(different-scale-default) value underneath, unhighlighted, looking like nothing was selected.
Now every feature always uses its own severity directly, with a scale-appropriate default so a
button is genuinely highlighted on a fresh install rather than falling back to a value from the
wrong scale that matches none of that feature's buttons."""

import pytest

from app import db

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings")
    db.init_db()
    yield
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings")
    db.init_db()


def test_fresh_install_defaults_updates_to_bug_fixes():
    assert db.get_feature_severity("updates") == "bugfix"
    assert db.get_effective_severity("updates") == "bugfix"


def test_fresh_install_defaults_logs_and_compose_to_suggestion():
    assert db.get_feature_severity("logs") == "suggestion"
    assert db.get_feature_severity("compose") == "suggestion"


def test_settings_page_highlights_the_default_severity_for_every_feature(client):
    page = client.get("/settings")
    assert 'id="updates_sev_bugfix" name="updates_severity" value="bugfix"\n           checked' in page.text
    assert 'id="logs_sev_suggestion" name="logs_severity" value="suggestion"\n           checked' in page.text
    assert 'id="compose_sev_suggestion" name="compose_severity" value="suggestion"\n           checked' in page.text


def test_severity_radio_groups_have_distinct_names_so_they_dont_fight_over_one_checked_state(client):
    """Regression test for a real-world report: all three severity radio groups used to share
    the literal name="severity", so the browser's own native radio exclusivity treated every
    button across Updates/Logs/Compose as ONE group -- only the last group rendered on the page
    could ever actually show as checked, no matter what the individual "checked" attributes in
    the markup said. Each group's inputs must share a name with each other but not across
    features."""
    page = client.get("/settings")
    assert 'name="updates_severity"' in page.text
    assert 'name="logs_severity"' in page.text
    assert 'name="compose_severity"' in page.text
    assert page.text.count('name="severity"') == 0


def test_general_minimum_severity_section_is_gone(client):
    page = client.get("/settings")
    assert "General minimum severity" not in page.text
    assert "Use general severity" not in page.text


def test_updates_own_scale_explanatory_text_is_gone(client):
    page = client.get("/settings")
    assert "always sets its own threshold directly rather than following the general severity" not in page.text


def test_each_section_shows_the_minimum_level_blurb(client):
    page = client.get("/settings")
    assert page.text.count("Minimum level to receive a notification") == 3


def test_severity_save_route_rejects_master_scope(client):
    resp = client.post("/settings/notify/severity/master", data={"master_severity": "warning"})
    assert resp.status_code == 404


def test_use_master_severity_route_no_longer_exists(client):
    resp = client.post("/settings/notify/use-master-severity/logs", data={"enabled": "on"})
    assert resp.status_code == 404


def test_saving_logs_severity_does_not_affect_compose(client):
    client.post("/settings/notify/severity/logs", data={"logs_severity": "critical"})
    assert db.get_feature_severity("logs") == "critical"
    assert db.get_feature_severity("compose") == "suggestion"
