"""Temporary testing aid: when Settings > Simulate AI Calls is on, every real AI call site in
summarizer.py returns a description of the real prompt it would have sent -- real gathered data
included -- shaped exactly like a real response (a valid (summary, severity) tuple, a valid
finding-dict list, a valid blurb string) so it flows through the *same* parsing/persistence path
a real response would and shows up in the same place (the same finding, update summary, or stack
blurb) instead of a separate log. Off by default.
"""

from unittest.mock import patch

import pytest

from app import ai_provider, db, summarizer


@pytest.fixture(autouse=True)
def clean_simulate_setting():
    db.set_simulate_ai_calls_enabled(False)
    yield
    db.set_simulate_ai_calls_enabled(False)


def test_simulate_mode_is_off_by_default():
    assert db.get_simulate_ai_calls_enabled() is False


def test_toggle_route_flips_the_setting(client):
    resp = client.post("/settings/ai/simulate-toggle", data={"enabled": "on"})
    assert resp.status_code == 200
    assert db.get_simulate_ai_calls_enabled() is True

    resp = client.post("/settings/ai/simulate-toggle", data={})
    assert db.get_simulate_ai_calls_enabled() is False


def test_settings_page_shows_the_toggle_off_by_default(client):
    page = client.get("/settings")
    assert "Simulate AI Calls" in page.text
    assert 'id="simulate_ai_calls_enabled"' in page.text
    checkbox_tag = page.text[page.text.index('id="simulate_ai_calls_enabled"'):]
    checkbox_tag = checkbox_tag[:checkbox_tag.index(">")]
    assert "checked" not in checkbox_tag


# ---------------------------------------------------------------------------
# Each real call site: simulate mode must never call the real AI, and must return a value
# shaped exactly like a real response -- proving it flows through the normal parsing/
# persistence path (a real finding/update/blurb gets created with this content) rather than
# being diverted somewhere else.
# ---------------------------------------------------------------------------

def test_summarize_update_simulated_returns_a_valid_summary_and_severity_tuple():
    db.set_anthropic_api_key("sk-test")
    db.set_simulate_ai_calls_enabled(True)
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        summary, severity = summarizer.summarize_update(
            container_name="sonarr", image_repo="owner/sonarr", old_tag_or_digest="1.0",
            new_tag_or_digest="1.1", release_notes="Fixed a crash on startup", compose_config=None,
        )
        mock_client_cls.return_value.messages.create.assert_not_called()

    assert severity in ("bugfix", "feature", "action_needed", "breaking")
    assert "Simulated AI Call" in summary
    assert "Fixed a crash on startup" in summary


def test_generate_upgrade_guidance_simulated_returns_real_data_not_blank():
    db.set_anthropic_api_key("sk-test")
    db.set_simulate_ai_calls_enabled(True)
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        result = summarizer.generate_upgrade_guidance(
            container_name="sonarr", image_repo="owner/sonarr", release_notes="Breaking: env var renamed",
            compose_config=None, summary_markdown="summary",
        )
        mock_client_cls.return_value.messages.create.assert_not_called()
    assert "Simulated AI Call" in result
    assert "Breaking: env var renamed" in result
    assert "Deep Analysis (Updates) is enabled" in result


def test_analyze_logs_batch_simulated_returns_one_valid_finding_per_container():
    db.set_anthropic_api_key("sk-test")
    db.set_simulate_ai_calls_enabled(True)
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        result = summarizer.analyze_logs_batch(
            {"plex": "ERROR: transcoder crashed", "sonarr": "WARN: retry"}, include_fix=True,
        )
        mock_client_cls.return_value.messages.create.assert_not_called()

    assert len(result) == 2
    by_container = {f["container"]: f for f in result}
    assert set(by_container) == {"plex", "sonarr"}
    for container, finding in by_container.items():
        assert finding["title"] == summarizer.SIMULATED_TITLE
        assert finding["category"] in ("error", "reliability", "optimization")
        assert finding["severity"] in ("critical", "warning", "suggestion")
        assert "fix" in finding  # include_fix was True
    assert "ERROR: transcoder crashed" in by_container["plex"]["description"]
    assert "WARN: retry" in by_container["sonarr"]["description"]


def test_analyze_logs_batch_simulated_omits_fix_field_when_deep_analysis_off():
    db.set_anthropic_api_key("sk-test")
    db.set_simulate_ai_calls_enabled(True)
    with patch("app.ai_provider.anthropic.Anthropic"):
        result = summarizer.analyze_logs_batch({"plex": "ERROR: x"}, include_fix=False)
    assert "fix" not in result[0]
    assert "disabled" in result[0]["description"]


def test_review_compose_file_simulated_returns_one_valid_finding():
    db.set_anthropic_api_key("sk-test")
    db.set_simulate_ai_calls_enabled(True)
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        result = summarizer.review_compose_file(
            "docker-compose.yml", "services:\n  app:\n    image: nginx", include_fix=False,
        )
        mock_client_cls.return_value.messages.create.assert_not_called()

    assert len(result) == 1
    finding = result[0]
    assert finding["title"] == summarizer.SIMULATED_TITLE
    assert "fix" not in finding
    assert "services:" in finding["description"]
    assert "image: nginx" in finding["description"]


def test_analyze_stack_impact_simulated_returns_real_data():
    db.set_anthropic_api_key("sk-test")
    db.set_simulate_ai_calls_enabled(True)
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        result = summarizer.analyze_stack_impact("media-stack", ["sonarr", "radarr"], "sonarr: fixed startup crash")
        mock_client_cls.return_value.messages.create.assert_not_called()
    assert "Simulated AI Call" in result
    assert "sonarr: fixed startup crash" in result
    assert "Cross-Service Analysis (Updates) is enabled" in result


def test_analyze_log_stack_impact_simulated_returns_real_data():
    db.set_anthropic_api_key("sk-test")
    db.set_simulate_ai_calls_enabled(True)
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        result = summarizer.analyze_log_stack_impact("media-stack", ["sonarr", "radarr"], "sonarr: crash loop")
        mock_client_cls.return_value.messages.create.assert_not_called()
    assert "Simulated AI Call" in result
    assert "sonarr: crash loop" in result
    assert "Cross-Service Analysis (Logs) is enabled" in result


def test_summarize_findings_overview_simulated_returns_real_data():
    db.set_anthropic_api_key("sk-test")
    db.set_simulate_ai_calls_enabled(True)
    findings = [
        {"severity": "warning", "title": "Disk almost full", "category": "reliability"},
        {"severity": "critical", "title": "Crash loop", "category": "error"},
    ]
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        result = summarizer.summarize_findings_overview("my-container", findings)
        mock_client_cls.return_value.messages.create.assert_not_called()
    assert "Simulated AI Call" in result
    assert "Disk almost full" in result


def test_simulate_mode_works_even_with_no_ai_provider_key_configured():
    """Simulate mode must not require a real key -- part of its value is letting someone see
    what would be sent before they've even set one up."""
    db.set_anthropic_api_key("")
    db.set_gemini_api_key("")
    db.set_simulate_ai_calls_enabled(True)
    assert ai_provider.is_configured() is False

    summary, severity = summarizer.summarize_update(
        container_name="sonarr", image_repo="owner/sonarr", old_tag_or_digest="1.0",
        new_tag_or_digest="1.1", release_notes="notes", compose_config=None,
    )
    assert "Simulated AI Call" in summary

    result = summarizer.analyze_logs_batch({"plex": "ERROR: x"}, include_fix=False)
    assert len(result) == 1
    result = summarizer.review_compose_file("compose.yml", "services: {}")
    assert len(result) == 1
    result = summarizer.analyze_stack_impact("stack", ["a", "b"], "notes")
    assert "Simulated AI Call" in result
    result = summarizer.analyze_log_stack_impact("stack", ["a", "b"], "findings")
    assert "Simulated AI Call" in result


def test_simulate_mode_off_makes_a_real_call_as_normal():
    """Sanity check that the guard clauses don't interfere with real behavior when the toggle
    is off -- the existing, already-tested real code path must still run."""
    db.set_anthropic_api_key("sk-test")
    db.set_simulate_ai_calls_enabled(False)
    with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls:
        mock_response = type("R", (), {"content": [type("B", (), {"type": "text", "text": "ok"})()]})()
        mock_client_cls.return_value.messages.create.return_value = mock_response
        result = summarizer.generate_upgrade_guidance(
            container_name="sonarr", image_repo="owner/sonarr", release_notes="notes",
            compose_config=None, summary_markdown="summary",
        )
        mock_client_cls.return_value.messages.create.assert_called_once()
    assert result == "ok"


# ---------------------------------------------------------------------------
# A simulated finding must be a real, valid finding once persisted -- proving it lands exactly
# where a real one would, not somewhere separate.
# ---------------------------------------------------------------------------

def test_a_simulated_logs_finding_persists_like_a_real_one(client):
    from app import db as db_module

    db.set_anthropic_api_key("sk-test")
    db.set_simulate_ai_calls_enabled(True)
    with patch("app.ai_provider.anthropic.Anthropic"):
        findings = summarizer.analyze_logs_batch({"plex-sim-test": "ERROR: transcoder crashed"}, include_fix=False)

    finding = findings[0]
    finding_id, is_new = db_module.upsert_finding(
        source="logs", subject="plex-sim-test", title=finding["title"],
        category=finding["category"], severity=finding["severity"],
        description_markdown=finding["description"], suggested_fix=finding.get("fix"),
    )
    try:
        stored = db_module.get_finding(finding_id)
        assert stored["title"] == summarizer.SIMULATED_TITLE
        assert "ERROR: transcoder crashed" in stored["description_markdown"]

        page = client.get(f"/findings/{finding_id}")
        assert page.status_code == 200
        assert "Simulated AI Call" in page.text
    finally:
        with db_module.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE id = ?", (finding_id,))
