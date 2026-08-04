"""The chat composer grows with what's typed into it instead of scrolling sideways.

It was a single-line <input>, which is the wrong control for this box: a question worth asking
the assistant is often several lines of pasted log or compose YAML, and an input scrolls one
character at a time with no way to read back what you wrote. It's a textarea now, one row tall
to start, growing a row at a time and scrolling internally once it reaches its cap.

The cap is three fifths of the PANEL's height, not of the window's. A window-relative cap would
leave a short panel with a composer that swallowed the conversation above it.

Browser-verified alongside these: it grows 1 -> 40 lines without ever showing a scrollbar
before the cap, stops exactly at the cap, shrinks back as text is deleted, drops to one row on
send, and the cap follows the panel when the window is resized."""

from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"
BASE = Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html"


def _rule(selector: str) -> str:
    text = CSS.read_text()
    start = text.index(selector + " {")
    return text[start:text.index("}", start)]


def test_the_composer_is_a_textarea_starting_at_one_row():
    text = BASE.read_text()
    assert '<textarea class="chat-input" id="chat-input" rows="1"' in text
    assert '<input type="text" class="chat-input"' not in text


def test_the_cap_is_three_fifths_of_the_panel_not_the_window():
    text = BASE.read_text()
    assert "panel.getBoundingClientRect().height" in text
    assert "panelHeight * 0.6" in text


def test_the_height_is_reset_before_it_is_measured():
    """scrollHeight on an element already sized to its content just reports that size back, so
    without the reset the box can only ever grow -- deleting text would leave it tall."""
    body = BASE.read_text()
    fn = body[body.index("function autoGrow()"):body.index("if (sendBtn) sendBtn.addEventListener")]
    assert 'input.style.height = "auto"' in fn
    assert fn.index('input.style.height = "auto"') < fn.index("input.scrollHeight")


def test_the_border_box_difference_is_added_back_in():
    """Everything is border-box, so a height set to scrollHeight alone lands exactly the
    borders short and the box sits permanently scrolled by a couple of pixels."""
    body = BASE.read_text()
    fn = body[body.index("function autoGrow()"):body.index("if (sendBtn) sendBtn.addEventListener")]
    assert "input.offsetHeight - input.clientHeight" in fn
    assert "Math.min(input.scrollHeight + chrome, cap)" in fn


def test_it_scrolls_internally_once_it_hits_the_cap():
    assert "overflow-y: auto" in _rule(".chat-input")


def test_the_drag_handle_is_off_since_the_script_owns_the_height():
    assert "resize: none" in _rule(".chat-input")


def test_enter_sends_and_shift_enter_makes_a_newline():
    body = BASE.read_text()
    assert 'evt.key === "Enter" && !evt.shiftKey && !evt.isComposing' in body
    assert "evt.preventDefault()" in body


def test_sending_collapses_it_back_to_one_row():
    """A composer left standing at the height of the question just sent would cover the top of
    the answer it's waiting for."""
    body = BASE.read_text()
    submit_fn = body[body.index("function submit()"):body.index("pending = true;")]
    assert 'input.value = "";' in submit_fn
    assert "autoGrow();" in submit_fn


def test_the_send_button_stays_level_with_the_last_line():
    """The composer grows upward, so a centred button would float in the middle of a tall box
    instead of sitting beside the line being typed."""
    assert "align-items: flex-end" in _rule(".chat-input-row")


def test_the_cap_is_recomputed_when_the_window_changes_size():
    assert 'window.addEventListener("resize", autoGrow)' in BASE.read_text()
