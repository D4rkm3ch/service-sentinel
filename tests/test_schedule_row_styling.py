"""A real-world report: the "Time" label in the weekly schedule row sat visibly higher than the
day checkboxes next to it, and the native checkboxes rendered as stark white/blue OS-default
boxes that clashed with the dark theme. Fixed with color-scheme: dark + accent-color (themes
every native form control sitewide, not just these checkboxes) and making each schedule mode's
label a flex container so its text and control align on the same line as the weekday group."""


def test_root_themes_native_form_controls_to_the_dark_palette():
    """color-scheme now lives in the dark theme's own [data-theme="dark"] block (each theme sets
    its own -- light's is color-scheme: light, see style.css's theme system), rather than a
    single un-themed :root, since which one applies depends on the active theme. accent-color
    itself isn't theme-specific (var(--accent) already resolves to whichever theme is active on
    its own) so it stays on the bare :root."""
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()
    dark_start = text.index(':root[data-theme="dark"] {')
    dark_block = text[dark_start:dark_start + text[dark_start:].index("}")]
    assert "color-scheme: dark" in dark_block
    root_start = text.index(":root {")
    root_block = text[root_start:root_start + text[root_start:].index("}")]
    assert "accent-color: var(--accent)" in root_block


def test_schedule_mode_field_labels_are_flex_aligned():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()
    assert ".schedule-mode-field label" in text
    block = text[text.index(".schedule-mode-field label"):]
    block = block[:block.index("}")]
    assert "display: inline-flex" in block
    assert "align-items: center" in block
