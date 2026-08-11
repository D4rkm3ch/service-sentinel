"""Settings copy pass (post-UI-overhaul feedback round): unify the two AI providers' concurrency
and API-key hint wording (same text, different numbers/links), shorten several Deep Analysis /
Cross-Service Analysis / Release Notes / Apprise strings, and dim+disable each feature's "notify
on check errors" toggle whenever that feature's own notifications are off."""

from app import db

db.init_db()


def _settings_text(client):
    return client.get("/settings").text


def test_concurrency_hints_read_identically_apart_from_the_numbers(client):
    """"Recommended:" makes clear these are suggestions, not hard limits -- a real-world report
    that the bare numbers alone read as confusing/arbitrary without it. Laid out the same as
    Retries -- title above explanation above the stepper -- rather than one inline sentence with
    the stepper tacked on the end, which a real-world report called easy to miss."""
    text = _settings_text(client)
    assert text.count("<h4>Concurrent AI Requests</h4>") == 4
    assert "Recommended: 2 free, 4 paid" in text
    assert "Recommended: 1 free, 4 paid" in text
    assert "Recommended: 4</p>" in text
    assert "Recommended: 1 for local models" in text


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
    """The heading is plain "Lookback Window" now that it sits inside the Updates panel -- see
    test_logs_lookback_window.py. Only the dropped intro sentence is still this test's business."""
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "templates" / "settings.html").read_text()
    assert "<h4>Lookback Window</h4>" in text
    assert "If you've missed several releases" not in text


def test_apprise_hint_no_longer_singles_out_discord(client):
    """The Discord-specific ?format=markdown explanation (and the auto-append behavior it
    described) was removed per a real-world report that it read as hamstringing every other
    Apprise-supported service -- see test_apprise_url_normalization.py."""
    text = _settings_text(client)
    assert "enable colored Discord embeds" not in text
    assert "Discord webhooks are entered as" not in text


def test_apprise_url_uses_the_test_and_save_cancel_button_pair_not_a_hint_paragraph(client):
    """Replaced the old lone "Send test notification" button + explanatory hint text with the
    same Test & Save / Cancel pair the API key fields already use -- the pair itself is
    self-explanatory, so the separate hint paragraph is gone entirely."""
    text = _settings_text(client)
    assert "Only saved after a successful test" not in text
    assert "typing alone doesn't save it" not in text
    assert "Send test notification" not in text
    apprise_section = text[text.index('id="apprise_urls_field"'):]
    apprise_section = apprise_section[:apprise_section.index("</div>", apprise_section.index("api-key-row"))]
    assert "Test &amp; Save" in apprise_section
    assert 'onclick="cancelAppriseEdit()"' in apprise_section


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


def test_the_modules_are_ordered_versions_runtime_configuration(client):
    """A real-world reorder request, originally about the Deep Analysis panel's three rows.
    Settings is grouped by module now (see test_settings_structure.py), so the ordering it asked
    for is the ordering of the panels themselves -- and it holds for every setting at once
    rather than needing re-asserting per panel."""
    text = _settings_text(client)
    heading = 'settings-heading-lg">{}</h2>'
    assert (text.index(heading.format("Versions"))
            < text.index(heading.format("Runtime"))
            < text.index(heading.format("Configuration")))


def test_the_per_module_rows_no_longer_repeat_their_own_module_name(client):
    """"Update Notifications" inside a Notifications panel made sense; inside the Updates panel
    it's saying Updates twice. Each module's panel heading carries the name, so the subsections
    within it are plain."""
    text = _settings_text(client)
    for stale in (">Update Notifications<", ">Runtime Notifications<", ">Configuration Notifications<",
                  ">Update Analysis<", ">Runtime Analysis<", ">Updates Pending<"):
        assert stale not in text
    assert text.count("<h4>Notifications</h4>") == 3
    assert text.count("<h4>Schedule</h4>") == 3


def test_deep_and_cross_service_analysis_are_one_subsection_now(client):
    """Two top-level panels carrying the same "costs noticeably more tokens" warning as each
    other -- they're the same kind of decision, so they're one subsection with the warning
    stated once."""
    text = _settings_text(client)
    assert ">Deep Analysis<" not in text
    assert ">Cross-Service Analysis<" not in text
    assert text.count("<h4>AI Analysis</h4>") == 3
    # The toggles themselves are untouched -- only their grouping changed.
    for feature in ("updates", "logs", "compose"):
        assert f'id="deep_analysis_{feature}"' in text
    for feature in ("updates", "logs"):
        assert f'id="cross_service_analysis_{feature}"' in text
    assert 'id="cross_service_analysis_compose"' not in text
