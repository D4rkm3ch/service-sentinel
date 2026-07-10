"""A batch of small, real fixes:

1. "Reset & re-check" was capitalized to "Reset & Re-Check" everywhere it appears as a button
   label (it was already title case except for the second word).
2. A further sitewide lowercase sweep found several more two-word labels/badges/headings that
   didn't match the rest of the app's title-case convention: "Suggested fix", "Upgrade guidance",
   "Lookback window", "Notes not found", "Up to date", "Save name", "Save key".
3. The top-table collapse/expand state used to persist across page loads via localStorage; that
   was reverted -- every fresh page load now shows the table expanded, though the toggle itself
   still works for the current page view.
4. The weekly schedule row's "Time" label rendered in a larger font than the adjacent weekday
   checkboxes because .schedule-mode-field label had no font-size override. Fixed to match
   .schedule-weekday-option's 13px, the label text was renamed "Time" -> "at" in all three modes
   that show it (daily/weekly/monthly), and a "|" separator was added between the weekday group
   and the "at" label in the weekly row.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_reset_and_recheck_button_label_is_fully_title_cased():
    offenders = []
    for path in (ROOT / "app" / "templates").glob("*.html"):
        text = path.read_text()
        if "Reset &amp; re-check<" in text or "Reset & re-check<" in text:
            offenders.append(path.name)
    assert offenders == []


def test_reset_and_recheck_button_appears_with_capitalized_recheck(client):
    for url in ("/updates", "/logs", "/compose"):
        resp = client.get(url)
        assert "Reset &amp; Re-Check" in resp.text


def test_suggested_fix_and_upgrade_guidance_headings_are_title_cased():
    finding_detail = (ROOT / "app" / "templates" / "finding_detail.html").read_text()
    assert "<h3>Suggested Fix</h3>" in finding_detail
    assert "<h3>Suggested fix</h3>" not in finding_detail

    detail = (ROOT / "app" / "templates" / "detail.html").read_text()
    assert "<h3>Upgrade Guidance</h3>" in detail
    assert "<h3>Upgrade guidance</h3>" not in detail


def test_settings_lookback_window_heading_is_title_cased():
    text = (ROOT / "app" / "templates" / "settings.html").read_text()
    assert "<h4>Lookback Window</h4>" in text


def test_notes_not_found_and_up_to_date_badges_are_title_cased():
    for name in ("_updates_table.html", "detail.html", "stack_detail.html"):
        text = (ROOT / "app" / "templates" / name).read_text()
        assert "Notes not found" not in text
    stack_detail = (ROOT / "app" / "templates" / "stack_detail.html").read_text()
    assert "Up To Date" in stack_detail
    assert "Up to date" not in stack_detail


def test_save_name_and_save_key_buttons_are_title_cased():
    for name in ("logs_stack_detail.html", "stack_detail.html"):
        text = (ROOT / "app" / "templates" / name).read_text()
        assert "Save Name" in text
        assert "Save name" not in text

    settings = (ROOT / "app" / "templates" / "settings.html").read_text()
    assert settings.count("Save Key") == 2
    assert "Save key" not in settings


def test_dead_dashboard_template_was_removed():
    assert not (ROOT / "app" / "templates" / "dashboard.html").exists()


def test_collapse_state_no_longer_written_to_or_read_from_localstorage():
    text = (ROOT / "app" / "templates" / "base.html").read_text()
    assert "localStorage" not in text
    # The click-to-toggle behavior itself must still be present.
    assert "collapsible-header" in text
    assert "scrollHeight" in text


def test_schedule_time_label_renamed_to_at_in_every_mode():
    text = (ROOT / "app" / "templates" / "_schedule_fields.html").read_text()
    assert ">Time <input" not in text
    assert text.count(">at <input") == 3


def test_weekly_schedule_row_has_a_separator_between_weekdays_and_time():
    text = (ROOT / "app" / "templates" / "_schedule_fields.html").read_text()
    weekly_block = text[text.index('data-mode="weekly"'):text.index('data-mode="monthly"')]
    assert "schedule-field-separator" in weekly_block
    # The separator must sit after the weekday group and before the "at" time label.
    assert weekly_block.index("schedule-weekday-group") < weekly_block.index("schedule-field-separator") < weekly_block.index(">at <input")

    css = (ROOT / "app" / "static" / "style.css").read_text()
    block = css[css.index(".schedule-field-separator"):]
    block = block[:block.index("}")]
    # Drawn as a CSS bar (not a "|" glyph) so its height can be set to match the time input.
    assert "height: 30px" in block


def test_schedule_mode_field_label_font_size_matches_weekday_option():
    text = (ROOT / "app" / "static" / "style.css").read_text()
    block = text[text.index(".schedule-mode-field label"):]
    block = block[:block.index("}")]
    assert "font-size: 13px" in block
