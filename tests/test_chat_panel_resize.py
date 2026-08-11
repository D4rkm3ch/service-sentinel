"""The chat panel's width is draggable, and the width survives navigation.

Everything downstream already keys off --chat-panel-width -- the panel's own width, the
topbar's right inset, and body's padding-right that pushes the page content over -- so the drag
handler only has to set that one custom property and the whole shell follows.

Two bounds matter. The panel can't be dragged narrower than a width the conversation still
reads at, and it can't be dragged wider than leaves the page content usable. The upper one is
deliberately expressed as a floor under the CONTENT, recomputed against the live window and
sidebar width, rather than as a fixed ceiling on the panel: what actually matters is that the
page stays workable, and how much room the panel may take to get there depends on how wide the
window is and whether the sidebar is expanded.

Browser-verified alongside these: dragging clamps at both bounds, the width persists across a
page load, shrinking the window re-clamps without destroying the stored width (it comes back
when there's room again), expanding the sidebar narrows the panel rather than the content, and
the 20px content gaps hold at every panel width."""

from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"
BASE = Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html"


def _rule(selector: str) -> str:
    text = CSS.read_text()
    start = text.index(selector + " {")
    return text[start:text.index("}", start)]


def _base() -> str:
    return BASE.read_text()


def test_the_handle_sits_on_the_panels_own_left_edge():
    """Inside the panel, so it slides away with it -- a handle left behind on the page edge
    would be a live drag target for a panel that isn't even open."""
    text = _base()
    panel = text.index('id="chat-panel"')
    handle = text.index('id="chat-resize-handle"')
    assert panel < handle < text.index('class="chat-panel-head"')


def test_the_handle_is_reachable_without_a_pointer():
    text = _base()
    handle = text[text.index('id="chat-resize-handle"') - 200:text.index('id="chat-resize-handle"') + 200]
    assert 'role="separator"' in handle
    assert 'tabindex="0"' in handle
    assert 'aria-label="Resize chat panel"' in handle
    assert "ArrowLeft" in text and "ArrowRight" in text


def test_the_handle_is_wide_enough_to_grab():
    """A 1px border is not a pointer target -- it straddles the edge instead."""
    rule = _rule(".chat-resize-handle")
    assert "cursor: col-resize" in rule
    assert "width: 7px" in rule
    assert "left: -3px" in rule


def test_dragging_turns_off_the_width_transitions():
    """The panel, the topbar's right edge and body's padding-right all ease over ~0.2s so
    opening and closing glides. Left on during a drag, the layout lags the pointer by a frame
    or two and the whole thing feels rubbery."""
    text = CSS.read_text()
    start = text.index("html[data-chat-resizing] body,")
    block = text[start:text.index("}", start)]
    for selector in (".chat-panel", ".topbar"):
        assert selector in block
    assert "transition: none" in block
    assert "user-select: none" in _rule("html[data-chat-resizing]")


def test_the_drag_uses_pointer_capture():
    """Without it the pointer outruns the 7px handle on the first real drag and the events go
    to whatever is underneath instead."""
    text = _base()
    assert "setPointerCapture" in text
    assert "releasePointerCapture" in text


def test_the_upper_bound_is_a_floor_under_the_content_not_a_ceiling_on_the_panel():
    text = _base()
    assert "CONTENT_FLOOR" in text
    # Recomputed from the live window and sidebar, so it holds on any window size.
    assert "window.innerWidth - sidebar - CONTENT_FLOOR" in text


def test_the_lower_bound_never_lets_the_content_floor_collapse_the_panel():
    """On a window too small to honour the floor at all, the subtraction goes negative -- the
    panel must clamp to its own minimum rather than to that."""
    text = _base()
    assert "Math.max(MIN_WIDTH, window.innerWidth - sidebar - CONTENT_FLOOR)" in text


def test_the_width_is_restored_before_first_paint():
    """Same treatment as theme/accent/sidebar/chat-open: restored from the deferred script
    instead, the panel paints at its default width first and then visibly jumps."""
    text = _base()
    head_script = text[:text.index("</head>")]
    assert "service-sentinel-chat-width" in head_script
    assert "--chat-panel-width" in head_script


def test_a_window_resize_reclamps_without_overwriting_the_stored_width():
    """A width that fitted on a wide monitor won't on a narrow one, but shrinking the window
    shouldn't cost the operator the width they picked -- it should come back."""
    text = _base()
    assert 'window.addEventListener("resize", reclamp)' in text
    resize_fn = text[text.index("function reclamp()"):text.index("function reclamp()") + 200]
    assert "false" in resize_fn, "re-clamping must not persist the clamped value"


def test_expanding_the_sidebar_reclamps_too():
    """It eats into the same width budget the content floor is measured against."""
    text = _base()
    assert "sidebarToggle" in text
    assert "setTimeout(reclamp" in text


def test_the_handle_is_removed_from_hit_testing_while_the_panel_is_closed():
    """A real-world report: with the panel closed, reaching for the window's scrollbar instead
    grabbed the resize handle -- accent highlight, col-resize cursor, nothing draggable. The
    panel hides by sliding off with transform: translateX(100%), which carries the handle with
    it, but the handle's own -3px grab overhang (see .chat-resize-handle above) rides straight
    back onto the screen as a sliver pinned to the window's right edge, exactly where the
    scrollbar lives.

    display:none is the only fix that actually works here -- visibility:hidden still hit-tests,
    so the sliver would keep stealing the pointer even though it can no longer be seen."""
    rule = _rule('html:not([data-chat="open"]) .chat-resize-handle')
    assert "display: none" in rule
