"""The footer used to just follow <main> in normal document flow with no sticky-footer layout,
so a short page (Overview with few cards, an empty Updates page) left it floating right under
the sparse content instead of at the bottom of the viewport, and a large gap (main's 64px
bottom padding + the footer's own 40px top margin, ~104px total) separated the two on every
page. Fixed with the standard flexbox sticky-footer pattern (body as a min-height:100vh flex
column, main taking the remaining space) and matching main's top/bottom padding so the gap
before the footer reads the same as the gap after the topbar."""

from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"


def _rule(selector: str) -> str:
    text = CSS.read_text()
    start = text.index(selector + " {")
    end = text.index("}", start)
    return text[start:end]


def test_body_is_a_full_height_flex_column():
    rule = _rule("body")
    assert "min-height: 100vh" in rule
    assert "display: flex" in rule
    assert "flex-direction: column" in rule


def test_main_grows_to_push_the_footer_down():
    rule = _rule("main")
    assert "flex: 1 0 auto" in rule


def test_footer_does_not_grow_and_has_no_extra_top_margin():
    rule = _rule(".app-footer")
    assert "flex-shrink: 0" in rule
    assert "margin: 0 auto" in rule


def test_main_top_and_bottom_padding_match():
    """The gap before the footer should read the same as the gap after the topbar -- both
    sides of main's vertical padding must be equal."""
    rule = _rule("main")
    padding_line = next(line for line in rule.splitlines() if "padding:" in line)
    values = padding_line.strip().rstrip(";").split(":", 1)[1].split()
    assert len(values) == 2, f"expected a two-value padding (vertical horizontal), got {values}"
