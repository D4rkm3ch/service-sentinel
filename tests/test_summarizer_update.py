"""Direct unit tests for summarizer.py's summarize_update() -- the per-service AI summary +
severity classification Stage 7 wires into persist.py. This existed dormant before Stage 7
(pre-written, never called) so its own prompt-response parsing logic (the SEVERITY: line
regex, the retry-on-empty-content behavior) had no test coverage at all until now. Mocks
app.summarizer.anthropic.Anthropic directly rather than hitting the real API."""

from unittest.mock import MagicMock, patch

import pytest

from app import summarizer


def _response(text: str):
    resp = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp.content = [block]
    return resp


def test_severity_line_is_parsed_and_stripped_from_the_summary():
    with patch("app.summarizer.settings.anthropic_api_key", "sk-test"), \
         patch("app.summarizer.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = _response(
            "## New Features\nAdds dark mode.\n\nSEVERITY: feature"
        )
        summary, severity = summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert severity == "feature"
    assert "SEVERITY" not in summary
    assert "Adds dark mode." in summary


def test_missing_severity_line_defaults_to_feature():
    with patch("app.summarizer.settings.anthropic_api_key", "sk-test"), \
         patch("app.summarizer.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = _response("## New Features\nSomething changed.")
        summary, severity = summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert severity == "feature"
    assert "Something changed." in summary


def test_severity_line_is_case_insensitive_and_can_appear_mid_response():
    with patch("app.summarizer.settings.anthropic_api_key", "sk-test"), \
         patch("app.summarizer.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = _response(
            "## Breaking Changes\nRemoved an env var.\nseverity: BREAKING\n"
        )
        summary, severity = summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert severity == "breaking"


def test_retries_once_if_the_model_returns_only_a_severity_line():
    with patch("app.summarizer.settings.anthropic_api_key", "sk-test"), \
         patch("app.summarizer.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.side_effect = [
            _response("SEVERITY: bugfix"),  # nothing but the severity line -- triggers a retry
            _response("## Bug Fixes\nFixed a crash.\nSEVERITY: bugfix"),
        ]
        summary, severity = summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    assert severity == "bugfix"
    assert "Fixed a crash." in summary
    assert mock_client_cls.return_value.messages.create.call_count == 2


def test_raises_after_two_empty_attempts_rather_than_storing_a_blank_summary():
    with patch("app.summarizer.settings.anthropic_api_key", "sk-test"), \
         patch("app.summarizer.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = _response("SEVERITY: bugfix")

        with pytest.raises(RuntimeError, match="no summary content"):
            summarizer.summarize_update(
                "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
            )

    assert mock_client_cls.return_value.messages.create.call_count == 2


def test_raises_immediately_if_api_key_is_not_configured():
    with patch("app.summarizer.settings.anthropic_api_key", ""):
        with pytest.raises(RuntimeError, match="not configured"):
            summarizer.summarize_update(
                "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
            )


def test_compose_config_none_uses_the_no_config_fallback_text_in_the_prompt():
    with patch("app.summarizer.settings.anthropic_api_key", "sk-test"), \
         patch("app.summarizer.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = _response("Body.\nSEVERITY: bugfix")
        summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text", None,
        )

    sent = mock_client_cls.return_value.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "no matching compose service found" in sent


def test_compose_config_is_included_as_json_in_the_prompt_when_present():
    with patch("app.summarizer.settings.anthropic_api_key", "sk-test"), \
         patch("app.summarizer.anthropic.Anthropic") as mock_client_cls:
        mock_client_cls.return_value.messages.create.return_value = _response("Body.\nSEVERITY: bugfix")
        summarizer.summarize_update(
            "sonarr", "linuxserver/sonarr", "sha256:old", "sha256:new", "release notes text",
            {"environment": ["PUID=1000"]},
        )

    sent = mock_client_cls.return_value.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "PUID=1000" in sent
