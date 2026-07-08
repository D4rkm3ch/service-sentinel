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
