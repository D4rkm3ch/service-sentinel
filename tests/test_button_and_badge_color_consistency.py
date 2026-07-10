"""Follow-up polish: (1) the Unread/Read and Silenced badges now share the same color family as
their matching buttons (violet for Read/Unread, blue for Silence) instead of the old flat
green/grey, and (2) every plain button (Check now included) is unfilled by default and fills
solid on hover, matching .button-danger/.button-warn/.button-silence/.button-info's existing
shape -- Check now used to be the only pre-filled button in the app.

Also covers a real-world regression report: every colored button variant's :hover briefly grew
a mismatched GREEN border, because the base `button:hover` rule sets border-color: var(--accent)
and none of .button-danger/.button-warn/.button-silence/.button-info's own :hover rules
overrode it -- CSS cascades per property, not per rule, so the more specific class selector's
declared properties (background, color) won, but the undeclared border-color fell through to
the lower-specificity element rule's green. Fixed by having every variant's :hover set its own
border-color too. And a Settings-page follow-up: the severity segmented buttons get the same
hollow-by-default look (transparent background, not a solid var(--bg) fill) as every other
button in the app now has."""

from pathlib import Path

STYLE = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()


def test_unread_read_badges_use_the_violet_family():
    assert "--violet" in _rule("badge-unread")
    assert "--violet" in _rule("badge-read") or "text-dim" in _rule("badge-read")
    assert "--accent" not in _rule("badge-unread")  # no longer the old green


def test_silenced_badge_uses_the_blue_info_family():
    assert "--info" in _rule("badge-silenced")
    assert "--text-dim" not in _rule("badge-silenced")  # no longer flat grey


def test_plain_buttons_are_unfilled_by_default_and_fill_on_hover():
    base_rule = _rule_block("button {")
    assert "background: transparent" in base_rule
    hover_rule = _rule_block("button:hover")
    assert "background: var(--accent)" in hover_rule


def test_every_colored_button_variant_sets_its_own_border_color_on_hover():
    """Regression guard: without this, every variant's hover state fell back to the base
    button's green border-color, producing a mismatched green outline on red/amber/blue/violet
    buttons."""
    for variant, color_var in [
        ("button-danger", "--error"),
        ("button-warn", "--warn"),
        ("button-silence", "--info"),
        ("button-info", "--violet"),
    ]:
        hover_rule = _rule_block(f".{variant}:hover")
        assert f"border-color: var({color_var})" in hover_rule, f"{variant}:hover is missing its own border-color"


def test_severity_buttons_are_hollow_by_default():
    rule = _rule("severity-btn")
    assert "background: transparent" in rule
    assert "background: var(--bg)" not in rule


def test_the_selected_severity_button_is_also_hollow_not_solid_filled():
    """A real-world correction: the first pass only fixed the unselected buttons -- the
    SELECTED one still had a solid var(--warn)/var(--error)/etc. fill. It should look like
    every other button's default (unhovered) state: transparent background, colored border and
    text, same shape as e.g. Send test notification -- just colored per severity instead of
    the unselected buttons' dim grey."""
    for variant, color_var in [
        ("severity-btn-bugfix", "--text-dim"),
        ("severity-btn-warning", "--warn"),
        ("severity-btn-critical", "--error"),
        ("severity-btn-feature", "--accent"),
    ]:
        rule = _rule_block(f"input:checked + .{variant}")
        assert f"border-color: var({color_var})" in rule
        assert "background:" not in rule  # no solid fill override for the checked state


def _rule(class_name: str) -> str:
    marker = f".{class_name} {{"
    start = STYLE.index(marker)
    end = STYLE.index("}", start)
    return STYLE[start:end]


def _rule_block(selector: str) -> str:
    start = STYLE.index(selector)
    end = STYLE.index("}", start)
    return STYLE[start:end]
