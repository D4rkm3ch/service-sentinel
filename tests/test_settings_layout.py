"""Settings page layout fixes from a real-world feedback round: AI Provider and Deep Analysis
moved to the top of the page (this is an AI program first and foremost), the "notify on
registry error" toggle moved above the severity buttons, the saved-indicator's reserved 40px
slot moved from between a toggle switch and its label (where it just read as an oversized gap)
to trail after the label instead, and the severity buttons colored to match their badges
elsewhere in the app instead of one flat accent color regardless of severity."""

from app import db


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
