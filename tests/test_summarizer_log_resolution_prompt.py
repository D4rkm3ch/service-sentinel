"""summarizer.analyze_logs_batch's active_findings_by_container argument -- makes sure the
"already tracked" section actually reaches the model's prompt when a container has open
findings, and stays absent when it doesn't, and that a resolved-issue marker in the model's
response passes through untouched (log_watcher.py is what acts on it)."""

from unittest.mock import patch

from app import summarizer


def _fake_complete_text(captured, response_text):
    def _inner(system, user_message, max_tokens):
        captured["system"] = system
        captured["user_message"] = user_message
        return response_text
    return _inner


def test_prompt_includes_already_tracked_section_when_findings_given():
    captured = {}
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake_complete_text(captured, "[]")):
        summarizer.analyze_logs_batch(
            {"web": "ERROR: connection refused"},
            active_findings_by_container={
                "web": [{"id": 1, "title": "Connection refused", "description": "keeps failing"}]
            },
        )

    assert "Already tracked -- check if still happening" in captured["user_message"]
    assert '"Connection refused": keeps failing' in captured["user_message"]


def test_prompt_omits_already_tracked_section_when_no_findings_given():
    captured = {}
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake_complete_text(captured, "[]")):
        summarizer.analyze_logs_batch({"web": "ERROR: connection refused"})

    assert "Already tracked" not in captured["user_message"]


def test_prompt_only_lists_tracked_findings_for_the_matching_container():
    captured = {}
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake_complete_text(captured, "[]")):
        summarizer.analyze_logs_batch(
            {"web": "ERROR: boom", "db": "all clear"},
            active_findings_by_container={
                "web": [{"id": 1, "title": "Connection refused", "description": "keeps failing"}]
            },
        )

    web_section, db_section = captured["user_message"].split("=== Container: db ===")
    assert "Already tracked" in web_section
    assert "Already tracked" not in db_section


def test_resolved_marker_passes_through_the_returned_list_untouched():
    response = '[{"container": "web", "resolved_title": "Connection refused"}]'
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value=response):
        result = summarizer.analyze_logs_batch(
            {"web": "all clear now"},
            active_findings_by_container={
                "web": [{"id": 1, "title": "Connection refused", "description": "keeps failing"}]
            },
        )

    assert result == [{"container": "web", "resolved_title": "Connection refused"}]
