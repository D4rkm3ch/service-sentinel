"""Sidebar hover-peek: while collapsed, hovering the rail widens it to the expanded width without
clicking the toggle. A JS mouseenter/mouseleave version of this was tried first, then removed
entirely (it felt obtrusive), then reintroduced here as a pure CSS :hover + :has() rule instead of
JS: a JS-applied class can't survive a full page navigation, so clicking a nav link while peeked
open used to load the next page already-collapsed and only re-peek once the JS timer re-fired -- a
jarring snap-shut-then-reopen. :hover instead reflects the browser's real, live cursor position, so
a same-viewport-position navigation (the normal case for clicking a link you're hovering) keeps
:hover matching straight through page load with no JS involved at all.

These are just presence/absence assertions on the shipped CSS -- see the Playwright script run
manually against a live server (not part of this suite) for the actual behavioral verification
that a continued hover survives navigation and only collapses once the mouse leaves the rail.
"""

from pathlib import Path

STYLE = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"
BASE_HTML = Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html"


def test_hover_peek_is_a_pure_css_rule_not_javascript():
    style = STYLE.read_text()
    assert 'html[data-sidebar="collapsed"]:has(.sidebar:hover)' in style

    # No JS mouseenter/mouseleave/timer-driven peeking -- the earlier version of this feature.
    base = BASE_HTML.read_text()
    assert "sidebar-peeking" not in base
    assert "sidebar-peeking" not in style
    assert "PEEK_DELAY" not in base
    assert "mouseenter" not in base
    assert "mouseleave" not in base


def test_hover_peek_has_no_opening_transition_delay():
    """A transition-delay before opening was tried and reverted: a full page navigation always
    repaints the rail collapsed for a beat before the browser's next hit-test re-applies :hover,
    so any added delay stacked on top of that and read as a real close rather than a brief
    pause -- exactly the snap this feature exists to avoid."""
    style = STYLE.read_text()
    peek_start = style.index('html[data-sidebar="collapsed"]:has(.sidebar:hover) {')
    peek_end = style.index("}", peek_start)
    peek_rule = style[peek_start:peek_end]
    assert "transition-delay" not in peek_rule


def test_pinned_expanded_state_is_unaffected_by_hover_peek():
    """The peek rule is scoped to [data-sidebar="collapsed"] only -- a pinned-open sidebar
    (via the hamburger click-to-toggle, still handled separately in JS) must not have its own
    width transition altered by hovering it."""
    style = STYLE.read_text()
    assert 'html[data-sidebar="expanded"]:has(.sidebar:hover)' not in style


def test_click_to_pin_toggle_and_its_tooltip_are_still_javascript():
    """The click-to-pin toggle (and its native title="Expand"/"Collapse" tooltip) is a
    deliberately separate mechanism from hover-peek and must still exist in JS."""
    base = BASE_HTML.read_text()
    assert 'getElementById("sidebar-toggle")' in base
    assert "titleFor" in base
