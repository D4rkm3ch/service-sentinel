"""A real-world report: the "Time" label in the weekly schedule row sat visibly higher than the
day checkboxes next to it, and the native checkboxes rendered as stark white/blue OS-default
boxes that clashed with the dark theme. Fixed with color-scheme: dark + accent-color (themes
every native form control sitewide, not just these checkboxes) and making each schedule mode's
label a flex container so its text and control align on the same line as the weekday group."""


def test_root_themes_native_form_controls_to_the_dark_palette():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()
    root_block = text[text.index(":root {"):text.index(":root {") + text[text.index(":root {"):].index("}")]
    assert "color-scheme: dark" in root_block
    assert "accent-color: var(--accent)" in root_block


def test_schedule_mode_field_labels_are_flex_aligned():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()
    assert ".schedule-mode-field label" in text
    block = text[text.index(".schedule-mode-field label"):]
    block = block[:block.index("}")]
    assert "display: inline-flex" in block
    assert "align-items: center" in block
