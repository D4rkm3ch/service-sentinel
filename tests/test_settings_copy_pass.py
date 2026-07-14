"""Settings copy pass (post-UI-overhaul feedback round): unify the two AI providers' concurrency
and API-key hint wording (same text, different numbers/links), shorten several Deep Analysis /
Cross-Service Analysis / Release Notes / Apprise strings, and dim+disable each feature's "notify
on check errors" toggle whenever that feature's own notifications are off."""

from app import db

db.init_db()


def _settings_text(client):
    return client.get("/settings").text


def test_concurrency_hints_read_identically_apart_from_the_numbers(client):
    """"recommended:" makes clear these are suggestions, not hard limits -- a real-world report
    that the bare numbers alone read as confusing/arbitrary without it."""
    text = _settings_text(client)
    assert "Concurrent AI requests (recommended: 2 free, 4 paid)" in text
    assert "Concurrent AI requests (recommended: 1 free, 4 paid)" in text


def test_api_key_hints_share_the_same_short_pattern(client):
    text = _settings_text(client)
    assert "Get a key from\n<a" in text or "Get a key from" in text
    assert "console.anthropic.com" in text
    assert "aistudio.google.com" in text
    assert "no card required" not in text


def test_github_rate_limit_line_drops_the_unauthenticated_aside(client):
    text = _settings_text(client)
    assert "60/hr to 5,000/hr" in text
    assert "unauthenticated" not in text


def test_deep_analysis_and_cross_service_copy_is_shortened(client):
    text = _settings_text(client)
    assert "so it's opt-in per\nfeature" not in text
    assert "uses more tokens)" not in text
    assert "Not offered for Compose Health" not in text


def test_release_notes_section_is_renamed_and_intro_merged():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "templates" / "settings.html").read_text()
    assert "Release Notes Lookback Window" in text
    assert "<h4>Lookback Window</h4>" not in text
    assert "If you've missed several releases" not in text


def test_apprise_hint_explains_why_format_markdown_is_appended(client):
    text = _settings_text(client)
    assert "enable colored Discord embeds" in text
    assert "shown below is added automatically" not in text


def test_test_notification_hint_is_short(client):
    """No trailing period -- a single-sentence settings blurb, see the periods-removal pass."""
    text = _settings_text(client)
    assert "Only saved after a successful test</p>" in text
    assert "typing alone doesn't save it" not in text


def test_include_errors_labels_drop_the_leading_also(client):
    """Jinja HTML-escapes the label text's apostrophes to &#39; -- match against the escaped
    form, same as a real browser's DOM would after parsing this HTML."""
    text = _settings_text(client)
    assert "Notify when a container&#39;s registry can&#39;t be reached" in text
    assert "Also notify when a container&#39;s registry can&#39;t be reached" not in text
    assert "Notify when a container&#39;s logs can&#39;t be fetched" in text
    assert "Also notify when a container&#39;s logs can&#39;t be fetched" not in text
    assert "Notify when a compose file can&#39;t be checked" in text
    assert "Also notify when a compose file can&#39;t be checked" not in text


def _row(text, row_id):
    """The macro emits <span class="toggle-with-label {{...}}" id="{{ unique_id }}_row"> --
    class comes before id, so anchoring on the id string alone would miss the class attribute
    sitting right before it. Walk back to the tag's own '<span' to capture the whole thing."""
    id_pos = text.index(f'id="{row_id}"')
    span_start = text.rindex("<span", 0, id_pos)
    return text[span_start:text.index("</span>", id_pos)]


def test_include_errors_toggle_is_dimmed_and_disabled_when_feature_notifications_are_off(client):
    db.set_feature_notify_enabled("updates", False)
    try:
        text = _settings_text(client)
        row = _row(text, "notify_updates_include_errors_row")
        assert "dimmed" in row
        input_start = text.index('id="notify_updates_include_errors"')
        input_tag = text[input_start:text.index(">", input_start)]
        assert "disabled" in input_tag
    finally:
        db.set_feature_notify_enabled("updates", True)


def test_include_errors_toggle_is_enabled_when_feature_notifications_are_on(client):
    db.set_feature_notify_enabled("logs", True)
    text = _settings_text(client)
    row = _row(text, "notify_logs_include_errors_row")
    assert "dimmed" not in row
    input_start = text.index('id="notify_logs_include_errors"')
    input_tag = text[input_start:text.index(">", input_start)]
    assert "disabled" not in input_tag


def test_enable_notifications_toggle_wires_up_the_gating_js(client):
    """Jinja HTML-escapes the onchange attribute's single quotes to &#39; too."""
    text = _settings_text(client)
    assert "toggleNotifyErrorsField(&#39;updates&#39;" in text
    assert "toggleNotifyErrorsField(&#39;logs&#39;" in text
    assert "toggleNotifyErrorsField(&#39;compose&#39;" in text
    assert "function toggleNotifyErrorsField" in text
