"""The page content should sit in an evenly-inset box: the same clear gap between it and the
topbar above, the sidebar rail on the left, the chat panel (or the window edge) on the right,
and the footer rule below -- whichever of the two pull-out panels happen to be open.

It didn't. main's horizontal padding was 24px against 20px vertical, and worse, .app-scroll
reserved its stable scrollbar gutter on the right edge only. That second one is what actually
made the two sides visibly unequal: the reserved lane shrank the box main centers itself in, so
a scrollbar's width of extra space appeared on the chat-panel side on every page, both when
main filled the region and when its max-width column centered inside it.

The fix is a pair: reserve the gutter on both edges so the box is symmetrical again, then
subtract one lane back out of main's own horizontal padding so lane + padding still add up to
exactly --content-gap. Measured in a real browser the rendered inset is now 20px on all four
sides in every panel combination. These are the CSS-level checks on the pieces that produce
that -- if one of them is edited away, the gap goes lopsided again."""

from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"


def _rule(selector: str) -> str:
    text = CSS.read_text()
    start = text.index(selector + " {")
    return text[start:text.index("}", start)]


def test_a_single_token_defines_the_gap_on_every_side():
    """One value, so the four sides can't drift apart the way 24px-vs-20px did."""
    assert "--content-gap: 20px;" in CSS.read_text()


def test_the_scrollbar_width_token_matches_the_themed_scrollbar():
    """main subtracts --scrollbar-width from its padding on the assumption that it's exactly
    the width of the lane .app-scroll reserves -- which is the width we style the scrollbar to
    be. If the two ever disagree the subtraction over- or under-shoots."""
    webkit = _rule("::-webkit-scrollbar")
    assert "width: 10px" in webkit
    assert "--scrollbar-width: 10px;" in CSS.read_text()


def test_the_scroll_container_reserves_its_gutter_on_both_edges():
    """both-edges is the part that makes the box symmetrical; plain `stable` reserves on the
    scrollbar's side only, which is the original bug."""
    assert "scrollbar-gutter: stable both-edges" in _rule(".app-scroll")


def test_main_subtracts_one_reserved_lane_from_its_horizontal_padding():
    rule = _rule("main")
    assert "padding: var(--content-gap) calc(var(--content-gap) - var(--scrollbar-width))" in rule


def test_the_footer_is_inset_to_the_same_line_as_the_content_above_it():
    """Its top border is a full-width rule, so if its padding didn't track main's the line
    would visibly start and stop somewhere other than the panels' edges."""
    rule = _rule(".app-footer")
    assert "calc(var(--content-gap) - var(--scrollbar-width))" in rule


def test_browsers_without_scrollbar_gutter_keep_the_full_padding():
    """Nothing is reserved there, so there's nothing to subtract -- without this fallback the
    inset would render at half the intended gap."""
    text = CSS.read_text()
    start = text.index("@supports not (scrollbar-gutter: stable)")
    block = text[start:text.index("\n}", start)]
    assert "main," in block
    assert ".app-footer" in block
    assert "padding-left: var(--content-gap)" in block
    assert "padding-right: var(--content-gap)" in block


def test_the_fallback_is_declared_after_the_rules_it_overrides():
    """It sets longhands against shorthands at equal specificity, so source order is the only
    thing deciding which wins."""
    text = CSS.read_text()
    assert text.index("@supports not (scrollbar-gutter: stable)") > text.index(".app-footer {")
    assert text.index("@supports not (scrollbar-gutter: stable)") > text.index("\nmain {")
