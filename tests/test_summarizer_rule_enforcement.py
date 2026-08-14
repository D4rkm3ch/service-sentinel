"""summarizer._enforce_custom_rules -- the guaranteed second pass that checks the primary
review's own findings against the operator's active custom rules (db.ai_custom_rules), added
after a real-world report that a detailed default-behavior instruction elsewhere in the system
prompt was outweighing a short operator rule appended after it in that same call. This runs the
check as an entirely separate call so the operator's rules are the only instruction being judged,
and covers both directions: dropping a finding an Exclude rule covers, and adding one a Watch
rule covers that the primary pass missed."""

from unittest.mock import patch

from app import db, summarizer

db.init_db()


def _clear():
    for rule in db.list_ai_custom_rules():
        db.remove_ai_custom_rule(rule["id"])


def setup_function(_):
    _clear()


def teardown_function(_):
    _clear()


# ---------------------------------------------------------------------------
# _enforce_custom_rules directly
# ---------------------------------------------------------------------------

def test_no_rules_returns_findings_unchanged_without_calling_the_provider():
    findings = [{"title": "Something", "category": "error", "severity": "warning", "description": "d"}]
    with patch("app.summarizer.ai_provider.complete_text") as fake:
        result = summarizer._enforce_custom_rules("logs", "context", findings, '"error"')
    fake.assert_not_called()
    assert result == findings


def test_no_findings_and_no_watch_rules_skips_the_call():
    db.add_ai_custom_rule("logs", "exclude", "rule", "Never flag X.")
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text") as fake:
        result = summarizer._enforce_custom_rules("logs", "context", [], '"error"')
    fake.assert_not_called()
    assert result == []


def test_no_findings_but_a_watch_rule_still_calls_the_provider():
    db.add_ai_custom_rule("logs", "watch", "rule", "Always flag Y.")
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value='{"drop": [], "add": []}') as fake:
        summarizer._enforce_custom_rules("logs", "context", [], '"error"')
    fake.assert_called_once()


def test_drops_a_finding_the_model_matches_to_an_exclude_rule():
    db.add_ai_custom_rule("logs", "exclude", "rule", "Never flag parse failures for *arr apps.")
    findings = [
        {"title": "Release title parsing errors", "category": "error", "severity": "critical", "description": "d"},
        {"title": "Disk pressure", "category": "reliability", "severity": "warning", "description": "d2"},
    ]
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value='{"drop": [0], "add": []}'):
        result = summarizer._enforce_custom_rules("logs", "context", findings, '"error", "reliability"')

    titles = [f["title"] for f in result]
    assert "Release title parsing errors" not in titles
    assert "Disk pressure" in titles


def test_adds_a_finding_the_model_matches_to_a_watch_rule():
    db.add_ai_custom_rule("compose", "watch", "rule", "Always flag exposed database ports.")
    add_payload = (
        '{"drop": [], "add": [{"title": "Postgres port exposed", "category": "security", '
        '"severity": "warning", "description": "5432 published to the host."}]}'
    )
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value=add_payload):
        result = summarizer._enforce_custom_rules("compose", "context", [], '"security"')

    assert len(result) == 1
    assert result[0]["title"] == "Postgres port exposed"


def test_a_provider_failure_keeps_the_primary_findings_unchanged():
    db.add_ai_custom_rule("logs", "exclude", "rule", "Never flag X.")
    findings = [{"title": "Something", "category": "error", "severity": "warning", "description": "d"}]
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=RuntimeError("boom")):
        result = summarizer._enforce_custom_rules("logs", "context", findings, '"error"')
    assert result == findings


def test_an_unparseable_response_keeps_the_primary_findings_unchanged():
    db.add_ai_custom_rule("logs", "exclude", "rule", "Never flag X.")
    findings = [{"title": "Something", "category": "error", "severity": "warning", "description": "d"}]
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value="not json"):
        result = summarizer._enforce_custom_rules("logs", "context", findings, '"error"')
    assert result == findings


def test_out_of_range_drop_indices_are_ignored_rather_than_crashing():
    db.add_ai_custom_rule("logs", "exclude", "rule", "Never flag X.")
    findings = [{"title": "Something", "category": "error", "severity": "warning", "description": "d"}]
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value='{"drop": [5, "x"], "add": []}'):
        result = summarizer._enforce_custom_rules("logs", "context", findings, '"error"')
    assert result == findings


# ---------------------------------------------------------------------------
# Wired into the real review calls end to end
# ---------------------------------------------------------------------------

def test_analyze_logs_batch_drops_an_excluded_finding_via_enforcement():
    db.add_ai_custom_rule("logs", "exclude", "rule", "Never flag parse failures for *arr apps.")
    primary = (
        '[{"container": "lidarr", "title": "Release title parsing errors", "category": "error", '
        '"severity": "critical", "description": "d"}]'
    )
    calls = {"n": 0}

    def _fake(system, user_message, max_tokens):
        calls["n"] += 1
        if calls["n"] == 1:
            return primary
        return '{"drop": [0], "add": []}'

    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=_fake):
        result = summarizer.analyze_logs_batch({"lidarr": "some log text"})

    assert result == []
    assert calls["n"] == 2


def test_analyze_logs_batch_leaves_resolved_markers_untouched_by_enforcement():
    # No findings with a "title" reach _enforce_custom_rules here, and there's no watch rule, so
    # it takes the no-op fast path and complete_text is only ever called once (the primary pass).
    db.add_ai_custom_rule("logs", "exclude", "rule", "Never flag parse failures for *arr apps.")
    primary = '[{"container": "lidarr", "resolved_title": "Old issue"}]'

    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value=primary) as fake:
        result = summarizer.analyze_logs_batch({"lidarr": "some log text"})

    fake.assert_called_once()
    assert result == [{"container": "lidarr", "resolved_title": "Old issue"}]


def test_review_compose_file_adds_a_watch_finding_via_enforcement():
    db.add_ai_custom_rule("compose", "watch", "rule", "Always flag exposed database ports.")
    add_payload = (
        '{"drop": [], "add": [{"title": "Postgres port exposed", "category": "security", '
        '"severity": "warning", "description": "5432 published to the host."}]}'
    )
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", side_effect=["[]", add_payload]):
        result = summarizer.review_compose_file("/compose/x.yaml", "services:\n  db:\n    image: postgres")

    assert any(f["title"] == "Postgres port exposed" for f in result)
