"""Every real message in the chat log carries a copy button.

An answer is very often a command or a config snippet meant to be pasted somewhere, and hand-
selecting rendered markdown out of a narrow side panel is miserable. Questions get one too --
re-asking a long question somewhere else is the same problem.

What lands on the clipboard is the plain markdown, never the rendered HTML: paste it into an
editor and you get the text, not a wall of tags. For an assistant turn that's the markdown the
server sent alongside the HTML; for a user turn the two are the same thing.

Browser-verified alongside these: both bubbles get a button, it's invisible until the message
is hovered on a pointer device, always visible on a touch device, and clicking it puts the
markdown (not the HTML) on the clipboard and flips the label to "Copied" and back."""

from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"
BASE = Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html"


def _rule(selector: str) -> str:
    text = CSS.read_text()
    start = text.index(selector + " {")
    return text[start:text.index("}", start)]


def test_both_sides_of_the_conversation_get_a_button():
    """Fourth argument to appendMessage is the text to copy, and passing it is what gives a
    message its button."""
    text = BASE.read_text()
    assert 'appendMessage("user", text, false, text);' in text
    assert 'appendMessage("assistant", data.html, true, data.markdown);' in text


def test_a_restored_conversation_gets_its_buttons_back():
    """The log is replayed from sessionStorage on every page load, so the restore path has to
    pass the copy text too or the buttons only exist until the next navigation."""
    text = BASE.read_text()
    assert 'appendMessage("assistant", turn.html || turn.content, !!turn.html, turn.content);' in text
    assert 'appendMessage("user", turn.content, false, turn.content);' in text


def test_the_spinner_and_the_error_notices_do_not_get_one():
    """There's nothing on them worth putting on the clipboard."""
    text = BASE.read_text()
    assert 'appendMessage("pending", "", true);' in text
    assert 'appendMessage("error", (data && data.error) || "Something went wrong.", false);' in text
    assert 'appendMessage("error", "Couldn\'t reach the server.", false);' in text


def test_the_assistant_copies_markdown_rather_than_the_rendered_html():
    """data.markdown, not data.html -- the whole point is pasting usable text."""
    text = BASE.read_text()
    assert "data.markdown);" in text


def test_there_is_a_fallback_for_pages_not_served_over_https():
    """navigator.clipboard only exists in a secure context, and this app is very often reached
    over plain http on a LAN address. Without the fallback the button would silently do nothing
    for most of the people running it."""
    text = BASE.read_text()
    fn = text[text.index("function copyToClipboard("):text.index("function buildCopyButton(")]
    assert "navigator.clipboard && window.isSecureContext" in fn
    assert 'document.execCommand("copy")' in fn
    # display:none / visibility:hidden would make the selection, and so the copy, fail.
    assert 'scratch.style.position = "fixed"' in fn
    assert "display" not in fn.split("scratch.style.position")[1].split("removeChild")[0]


def test_the_scratch_textarea_is_always_cleaned_up():
    text = BASE.read_text()
    fn = text[text.index("function copyToClipboard("):text.index("function buildCopyButton(")]
    assert "document.body.removeChild(scratch)" in fn
    # Removed before the promise settles either way, so a failed copy can't leak an element.
    assert fn.index("removeChild(scratch)") < fn.index("resolve()")


def test_clicking_confirms_and_then_returns_to_its_resting_label():
    text = BASE.read_text()
    assert '"#icon-check", "Copied", "is-copied"' in text
    assert '"#icon-copy", "Failed", "is-failed"' in text
    assert "clearTimeout(revert)" in text, "hammering the button must not strand the label"


def test_the_button_sits_under_its_own_bubble_on_its_own_side():
    assert "margin-top: 3px" in _rule(".chat-msg-actions")
    assert "justify-content: flex-end" in _rule(".chat-msg-user .chat-msg-actions")


def test_hover_reveal_only_applies_where_hovering_exists():
    """A touch screen reports (hover: none) and skips the block entirely, so the button is
    simply always visible there -- a control you can't reveal is worse than one always on show.
    Keyboard focus reveals it for the same reason, and the confirmation state keeps it up so
    the "Copied" flash isn't missed by a pointer that has already moved on."""
    text = CSS.read_text()
    start = text.index("@media (hover: hover) {")
    block = text[start:text.index("\n}", text.index(".chat-copy-btn.is-failed {", start))]
    assert "opacity: 0" in block
    for selector in (".chat-msg:hover .chat-copy-btn", ".chat-copy-btn:focus-visible",
                     ".chat-copy-btn.is-copied", ".chat-copy-btn.is-failed"):
        assert selector in block


def test_the_icons_it_swaps_between_both_exist_in_the_sprite():
    text = BASE.read_text()
    assert '<symbol id="icon-copy"' in text
    assert '<symbol id="icon-check"' in text
