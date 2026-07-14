"""Direct unit tests for summarizer.py's summarize_update() -- the per-service AI summary +
severity classification Stage 7 wires into persist.py. This existed dormant before Stage 7
(pre-written, never called) so its own prompt-response parsing logic (the SEVERITY: line
regex, the retry-on-empty-content behavior) had no test coverage at all until now. Mocks
app.summarizer.ai_provider.complete_text -- the provider-agnostic dispatcher (see
app/ai_provider.py) every AI call site now goes through -- rather than any specific provider's
SDK, since summarize_update() itself no longer knows or cares which provider is active."""

from unittest.mock import patch

import pytest

from app import summarizer


def test_severity_line_is_parsed_and_stripped_from_the_summary():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text",
               return_value="## New Features\nAdds dark mode.\n\nSEVERITY: feature") as mock_complete:
        summary, severity = summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert severity == "feature"
    assert "SEVERITY" not in summary
    assert "Adds dark mode." in summary
    mock_complete.assert_called_once()


def test_missing_severity_line_defaults_to_feature():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value="## New Features\nSomething changed."):
        summary, severity = summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert severity == "feature"
    assert "Something changed." in summary


def test_severity_line_is_case_insensitive_and_can_appear_mid_response():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text",
               return_value="## Breaking Changes\nRemoved an env var.\nseverity: BREAKING\n"):
        summary, severity = summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert severity == "breaking"


def test_retries_once_if_the_model_returns_only_a_severity_line():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text") as mock_complete:
        mock_complete.side_effect = [
            "SEVERITY: bugfix",  # nothing but the severity line -- triggers a retry
            "## Bug Fixes\nFixed a crash.\nSEVERITY: bugfix",
        ]
        summary, severity = summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert severity == "bugfix"
    assert "Fixed a crash." in summary
    assert mock_complete.call_count == 2


def test_raises_after_two_empty_attempts_rather_than_storing_a_blank_summary():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value="SEVERITY: bugfix") as mock_complete:
        with pytest.raises(RuntimeError, match="no summary content"):
            summarizer.summarize_update(
                "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
            )

    assert mock_complete.call_count == 2


def test_raises_immediately_if_no_provider_is_configured():
    with patch("app.summarizer.ai_provider.is_configured", return_value=False):
        with pytest.raises(RuntimeError, match="No AI provider is configured"):
            summarizer.summarize_update(
                "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
            )


def test_compose_config_none_uses_the_no_config_fallback_text_in_the_prompt():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value="Body.\nSEVERITY: bugfix") as mock_complete:
        summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    sent = mock_complete.call_args.kwargs["user_message"]
    assert "no matching compose service found" in sent


def test_compose_config_is_included_as_json_in_the_prompt_when_present():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value="Body.\nSEVERITY: bugfix") as mock_complete:
        summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text",
            {"environment": ["PUID=1000"]},
        )

    sent = mock_complete.call_args.kwargs["user_message"]
    assert "PUID=1000" in sent


def test_lone_bullet_in_a_section_is_converted_to_a_plain_sentence():
    """A real-world report: the system prompt asks the model to write a plain sentence instead
    of a one-item bullet list, but the model doesn't always comply -- enforced in code now (see
    summarizer._debulletize_single_item_sections)."""
    raw = (
        "## New Features\n"
        "- Read-only filesystem support now requires exposing `/run` as a tmpfs.\n\n"
        "## Breaking Changes\n"
        "None found.\n\n"
        "## Relevant to your Setup\n"
        "Nothing in this release affects your configuration.\n\n"
        "SEVERITY: feature"
    )
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value=raw):
        summary, severity = summarizer.summarize_update(
            "jdownloader-2", "jlesage/jdownloader-2", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert "- Read-only filesystem support" not in summary
    assert "<li>" not in summary
    assert "Read-only filesystem support now requires exposing `/run` as a tmpfs." in summary


def test_genuine_multi_item_list_is_left_as_a_list():
    raw = (
        "## New Features\n"
        "- First point.\n"
        "- Second point.\n\n"
        "SEVERITY: feature"
    )
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value=raw):
        summary, severity = summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert "- First point." in summary
    assert "- Second point." in summary


def test_plain_sentence_section_is_left_untouched():
    raw = "## New Features\nNothing notable.\n\nSEVERITY: bugfix"
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value=raw):
        summary, severity = summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert summary.strip() == "## New Features\nNothing notable."


def test_debulletize_helper_directly_on_a_multi_section_document():
    text = (
        "## New Features\n"
        "- Only point.\n\n"
        "## Breaking Changes\n"
        "- One.\n"
        "- Two.\n\n"
        "## Relevant to your Setup\n"
        "Already a sentence."
    )
    result = summarizer._debulletize_single_item_sections(text)
    assert "## New Features\n\nOnly point." in result
    assert "- One.\n- Two." in result
    assert "Already a sentence." in result
