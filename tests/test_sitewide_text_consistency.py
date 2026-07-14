"""A final parity/consistency sweep across Updates, Logs, and Compose after bringing Compose to
full functional parity -- found several small, real, leftover text inconsistencies:

1. The global "Check now" button (feature-header) was the only one lowercase; every scoped/
   item-level Check Now button elsewhere already used title case.
2. The "All containers"/"All files" bottom table showed lowercase "issue"/"healthy" status
   badges while literally every other badge label in the app is title-cased.
3. "Log health"/"Compose health" were capitalized to "Log Health"/"Compose Health" on the
   Logs/Compose page headers earlier this session, but the Overview page's own cards, and the
   entire Settings page (Deep Analysis, Cross-Service Analysis, Scheduling, Notifications
   section headings, body text, and one stack-detail tooltip) were never updated to match."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_global_check_now_button_is_title_case():
    text = (ROOT / "app" / "templates" / "_feature_header.html").read_text()
    assert "Check Now" in text
    assert "Check now" not in text


def test_status_list_table_badges_are_title_cased():
    text = (ROOT / "app" / "templates" / "_status_list_table.html").read_text()
    assert '>Issue<' in text
    assert '>Healthy<' in text


def test_no_lowercase_log_or_compose_health_survives_anywhere_in_templates_or_routes():
    offenders = []
    for path in (ROOT / "app" / "templates").glob("*.html"):
        text = path.read_text()
        if "Log health" in text or "Compose health" in text:
            offenders.append(path.name)
    main_py = (ROOT / "app" / "main.py").read_text()
    if "Log health" in main_py or "Compose health" in main_py:
        offenders.append("main.py")
    assert offenders == []


def test_overview_cards_show_title_cased_feature_names(client):
    resp = client.get("/")
    assert "Runtime Health" in resp.text
    assert "Configuration Health" in resp.text
    assert "Runtime health" not in resp.text
    assert "Configuration health" not in resp.text


def test_settings_page_shows_title_cased_feature_names_everywhere(client):
    resp = client.get("/settings")
    assert "Runtime health" not in resp.text
    assert "Configuration health" not in resp.text
    # Spot-check a few of the sections that had it: Deep Analysis, Cross-Service Analysis,
    # Scheduling, Notifications all repeat the feature name as a subsection heading.
    assert resp.text.count("Runtime Health") >= 4
    assert resp.text.count("Configuration Health") >= 4
