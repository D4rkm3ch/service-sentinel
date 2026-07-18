"""summarizer.review_compose_file's active_findings argument -- the compose-side counterpart to
test_summarizer_log_resolution_prompt.py. Makes sure a file's currently tracked findings (open
and silenced) reach the model's prompt so it can recognize a still-ongoing issue and reuse its
exact title instead of re-wording it fresh every review -- a real-world report showed the same
misconfigured setting getting re-titled ("Insecure authentication setting" vs "Mousehole allows
no authentication") on separate reviews of the same file, spawning duplicate findings instead of
bumping one."""

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
        summarizer.review_compose_file(
            "/compose/mousehole/compose.yaml", "services:\n  mousehole:\n    image: x",
            active_findings=[{"id": 1, "title": "Insecure authentication setting",
                               "description": "no-auth is enabled", "status": "active"}],
        )

    assert "Already tracked in this file" in captured["user_message"]
    assert '"Insecure authentication setting": no-auth is enabled' in captured["user_message"]


def test_prompt_omits_already_tracked_section_when_none_given():
    captured = {}
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake_complete_text(captured, "[]")):
        summarizer.review_compose_file("/compose/x/compose.yaml", "services:\n  x:\n    image: y")

    assert "Already tracked" not in captured["user_message"]


def test_prompt_omits_already_tracked_section_for_an_empty_list():
    captured = {}
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake_complete_text(captured, "[]")):
        summarizer.review_compose_file(
            "/compose/x/compose.yaml", "services:\n  x:\n    image: y", active_findings=[],
        )

    assert "Already tracked" not in captured["user_message"]


def test_prompt_marks_a_silenced_tracked_finding_so_the_model_can_still_match_its_title():
    captured = {}
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake_complete_text(captured, "[]")):
        summarizer.review_compose_file(
            "/compose/mousehole/compose.yaml", "services:\n  mousehole:\n    image: x",
            active_findings=[
                {"id": 1, "title": "Mousehole allows no authentication",
                 "description": "no-auth is enabled", "status": "silenced"},
                {"id": 2, "title": "Missing restart policy",
                 "description": "no restart: set", "status": "active"},
            ],
        )

    assert '"Mousehole allows no authentication" (silenced): no-auth is enabled' in captured["user_message"]
    assert '"Missing restart policy": no restart: set' in captured["user_message"]


def test_system_prompt_instructs_reusing_a_tracked_title_when_the_same_issue_recurs():
    """Locks in the actual fix -- the prompt tells the model to prefer the tracked title over
    inventing new wording, since that's what makes upsert_finding's fingerprint-based dedup
    actually catch a recurrence instead of silently filing a lookalike duplicate."""
    rendered = summarizer.COMPOSE_REVIEW_SYSTEM_PROMPT_BASE.format(fix_field="")
    assert "Already tracked in this file" in rendered
    assert "reuse that tracked issue's title exactly as given" in rendered
