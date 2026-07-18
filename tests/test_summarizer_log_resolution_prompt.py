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


def test_prompt_marks_a_silenced_tracked_finding_so_the_model_can_still_match_its_title():
    """A real-world report: a silenced finding recurring under different AI-generated wording
    kept getting re-filed as a brand-new unread duplicate, because silenced findings used to be
    excluded from this context entirely -- the model had nothing to match its new phrasing
    against. Silenced findings are now included, marked so the model knows not to treat one as a
    fresh, currently-open issue needing a resolution judgment."""
    captured = {}
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake_complete_text(captured, "[]")):
        summarizer.analyze_logs_batch(
            {"web": "ERROR: connection refused"},
            active_findings_by_container={
                "web": [
                    {"id": 1, "title": "Connection refused", "description": "keeps failing", "status": "silenced"},
                    {"id": 2, "title": "Slow response", "description": "sometimes slow", "status": "active"},
                ]
            },
        )

    assert '"Connection refused" (silenced): keeps failing' in captured["user_message"]
    assert '"Slow response": sometimes slow' in captured["user_message"]


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


def test_system_prompt_instructs_reusing_a_tracked_title_when_the_same_issue_recurs():
    """Locks in the actual fix for the real-world report (the same recurring problem, e.g. a
    proxy failing against different backend IPs on different days, kept getting re-titled and
    re-filed as a new finding instead of bumping one): the model must reuse a matching tracked
    title verbatim, must never bake a single volatile detail into a title, and must never treat
    a silenced tracked issue as newly resolved."""
    rendered = summarizer.LOG_TRIAGE_SYSTEM_PROMPT_BASE.format(fix_field="")
    assert "reuse that tracked issue's title exactly as given below" in rendered
    assert "never bake in a single volatile detail" in rendered
    assert 'Never report a "(silenced)" issue as resolved' in rendered
