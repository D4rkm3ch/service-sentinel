"""Sidebar hover-peek: while collapsed, hovering the rail after a short delay widens it to the
expanded width without clicking the toggle. This has gone through three versions: (1) a JS
mouseenter/mouseleave timer toggling a "sidebar-peeking" class -- removed entirely for feeling
obtrusive; (2) a pure CSS :hover + :has() rule with no JS at all -- reintroduced after realizing
the real complaint was that (1) couldn't survive a full page navigation (clicking a nav link while
peeked open snapped shut then slowly reopened), reasoning that :hover reflects the browser's live
cursor position and so should carry straight through a same-position navigation; (3) the current
version -- real-world testing of (2) showed that reasoning didn't hold in practice (a fresh
document doesn't reliably re-apply :hover to an already-positioned cursor immediately on paint,
and users also wanted the original open-delay back), so it's back to a JS timer + class, but with
a sessionStorage handoff read by the same blocking pre-paint <head> script that already restores
theme/accent/sidebar-collapsed state -- a deterministic restore rather than a bet on browser hover
timing.

These are presence/absence assertions on the shipped markup/CSS. The actual cross-navigation
behavior was verified manually with Playwright against a live server (not part of this suite).
"""

from pathlib import Path

STYLE = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"
BASE_HTML = Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html"


def test_hover_peek_css_targets_the_peeking_class_scoped_to_collapsed():
    style = STYLE.read_text()
    assert 'html[data-sidebar="collapsed"].sidebar-peeking {' in style
    assert 'html[data-sidebar="collapsed"].sidebar-peeking .sidebar-label {' in style
    # The pure-:hover version tried and reverted for this exact class of bug.
    assert ":has(.sidebar:hover)" not in style


def test_pinned_expanded_state_is_unaffected_by_hover_peek():
    style = STYLE.read_text()
    assert 'html[data-sidebar="expanded"].sidebar-peeking' not in style


def test_hover_peek_js_has_an_opening_delay_and_instant_close():
    base = BASE_HTML.read_text()
    assert "PEEK_DELAY_MS = 400" in base
    assert "mouseenter" in base
    assert "mouseleave" in base
    assert 'sidebar.addEventListener("mouseleave", cancelPeek)' in base


def test_pin_toggle_click_cancels_any_active_peek():
    base = BASE_HTML.read_text()
    assert "pinToggleBtn.addEventListener(\"click\", cancelPeek)" in base


def test_peek_state_is_handed_off_via_sessionstorage_on_unload():
    base = BASE_HTML.read_text()
    peek_iife_start = base.index("Sidebar hover-peek")
    peek_iife_end = base.index("Light/dark theme toggle")
    peek_iife = base[peek_iife_start:peek_iife_end]
    assert "pagehide" in peek_iife
    assert 'PEEK_STORAGE_KEY = "service-sentinel-sidebar-peek"' in peek_iife
    assert "sessionStorage.setItem(PEEK_STORAGE_KEY" in peek_iife


def test_head_script_restores_the_peeking_class_before_first_paint():
    """Same before-first-paint treatment as theme/accent/sidebar-collapsed above it -- this is
    what lets a continued hover survive a full page navigation without a flash, instead of
    depending on the browser's own (unreliable, per real-world testing) :hover recompute timing
    after a fresh document loads."""
    base = BASE_HTML.read_text()
    head = base[:base.index("</head>")]
    assert 'sessionStorage.getItem("service-sentinel-sidebar-peek")' in head
    assert 'document.documentElement.classList.add("sidebar-peeking")' in head
    assert 'sessionStorage.removeItem("service-sentinel-sidebar-peek")' in head


def test_click_to_pin_toggle_and_its_tooltip_are_still_javascript():
    """The click-to-pin toggle (and its native title="Expand"/"Collapse" tooltip) is a
    deliberately separate mechanism from hover-peek and must still exist in JS."""
    base = BASE_HTML.read_text()
    assert 'getElementById("sidebar-toggle")' in base
    assert "titleFor" in base
