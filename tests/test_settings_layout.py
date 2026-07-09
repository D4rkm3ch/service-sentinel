"""Settings page layout fixes from a real-world feedback round: AI Provider and Deep Analysis
moved to the top of the page (this is an AI program first and foremost), the "notify on
registry error" toggle moved above the severity buttons, the saved-indicator's reserved 40px
slot moved from between a toggle switch and its label (where it just read as an oversized gap)
to trail after the label instead, and the severity buttons colored to match their badges
elsewhere in the app instead of one flat accent color regardless of severity. A later round
removed the now-redundant "Active provider" label and the Timezone explanation paragraph, and
replaced the free-form Apprise textarea with a single input showing a fixed ?format=markdown
suffix outside the editable box."""

from pathlib import Path

SETTINGS_TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "templates" / "settings.html"


def test_ai_provider_and_deep_analysis_come_before_scheduling_and_notifications(client):
    page = client.get("/settings")
    ai_provider_pos = page.text.index(">AI Provider<")
    deep_analysis_pos = page.text.index(">Deep Analysis<")
    scheduling_pos = page.text.index(">Scheduling<")
    notifications_pos = page.text.index(">Notifications<")
    assert ai_provider_pos < deep_analysis_pos < scheduling_pos < notifications_pos


def test_registry_error_toggle_sits_between_enable_notifications_and_severity_buttons(client):
    page = client.get("/settings")
    updates_section = page.text[page.text.index("Updates</h4>"):]
    enable_pos = updates_section.index("Enable notifications")
    errors_pos = updates_section.index("can&#39;t be reached")
    severity_pos = updates_section.index("Minimum level to receive a notification.")
    assert enable_pos < errors_pos < severity_pos


def test_saved_indicator_trails_the_toggle_label_not_between_switch_and_label(client):
    """The macro now renders switch -> label -> saved-indicator-slot in that DOM order, so
    nothing sits between the switch and its label text anymore."""
    page = client.get("/settings")
    start = page.text.index('id="notify_master_enabled"')
    chunk = page.text[start:start + 500]
    label_pos = chunk.index("Enable notifications")
    slot_pos = chunk.index("saved-indicator-slot")
    assert label_pos < slot_pos


def test_severity_buttons_use_per_severity_colors_not_one_flat_class(client):
    """Regression test for a real-world report: every selected severity button used to render
    identically (one flat accent color) regardless of which tier it was -- now each severity
    tier's own badge color class exists in the CSS for the selected state."""
    css = open("app/static/style.css").read()
    assert ".severity-group input:checked + .severity-btn-bugfix" in css
    assert ".severity-group input:checked + .severity-btn-breaking" in css
    assert ".severity-group input:checked + .severity-btn-warning" in css


def test_ai_provider_description_paragraph_is_gone_or_much_shorter(client):
    page = client.get("/settings")
    assert "Powers release note summaries, severity classification, log/compose analysis" not in page.text


def test_active_provider_label_is_gone_and_dropdown_sits_right_under_the_heading():
    """The "Active provider" <h4> was redundant once the AI Provider blurb above it was
    removed -- the select now sits directly under the "AI Provider" <h2>."""
    text = SETTINGS_TEMPLATE.read_text()
    assert "Active provider" not in text
    ai_provider_heading = text.index(">AI Provider<")
    select_pos = text.index('name="ai_provider"')
    deep_analysis_heading = text.index(">Deep Analysis<")
    assert ai_provider_heading < select_pos < deep_analysis_heading


def test_timezone_explanation_paragraph_is_gone():
    text = SETTINGS_TEMPLATE.read_text()
    assert "actually means" not in text
    timezone_section = text.split(">Timezone<")[1].split("</form>")[0]
    assert "Last checked" not in timezone_section


def test_apprise_field_shows_a_fixed_format_markdown_suffix(client):
    page = client.get("/settings")
    assert '<span class="input-suffix">?format=markdown</span>' in page.text
    # The editable input itself must never show the suffix baked into its value -- it's shown
    # once, fixed, outside the box, not duplicated inside it.
    input_start = page.text.index('id="apprise_urls_field"')
    input_end = page.text.index(">", input_start)
    assert "?format=markdown" not in page.text[input_start:input_end]


def test_apprise_help_text_shows_a_discord_format_example():
    text = SETTINGS_TEMPLATE.read_text()
    assert "discord://{WebhookID}/{WebhookToken}" in text
    assert "Discord webhook URLs are formatted automatically." not in text


def test_apprise_help_text_breaks_after_the_first_sentence():
    text = SETTINGS_TEMPLATE.read_text()
    start = text.index("Apprise URL(s), comma-separated")
    end = text.index("</p>", start)
    paragraph = text[start:end]
    assert "for other services.<br>Discord webhooks" in paragraph
