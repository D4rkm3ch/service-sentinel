"""Follow-up polish: (1) the Unread/Read and Silenced badges now share the same color family as
their matching buttons (violet for Read/Unread, blue for Silence) instead of the old flat
green/grey, and (2) every plain button (Check now included) is unfilled by default and fills
solid on hover, matching .button-danger/.button-warn/.button-silence/.button-info's existing
shape -- Check now used to be the only pre-filled button in the app."""

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


def _rule(class_name: str) -> str:
    marker = f".{class_name} {{"
    start = STYLE.index(marker)
    end = STYLE.index("}", start)
    return STYLE[start:end]


def _rule_block(selector: str) -> str:
    start = STYLE.index(selector)
    end = STYLE.index("}", start)
    return STYLE[start:end]
