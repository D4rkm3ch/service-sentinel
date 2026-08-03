"""The Updates/Runtime/Configuration page header must stay exactly one row at every width.

A real-world report: with the sidebar and the chat panel both open there's little horizontal
room left, and the header broke three ways at once -- the collapse arrow dropped onto its own
line under the title, "Check Now" and "Reset & Re-Check" split mid-phrase, and the "last
checked" summary wrapped. The rule the layout now encodes: buttons and the title (with its
count and arrow) never wrap or shrink; the status summary is the only thing allowed to give up
space, truncating to nothing if it must.

CSS-level assertions rather than a rendering test -- the actual geometry was verified in a real
browser at 1500/1250/1050/900px; these pin the specific declarations that produce it, since
every one of them was arrived at by fixing a distinct failure and is easy to undo by accident.
"""

from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()
HEADER_TMPL = (Path(__file__).resolve().parent.parent / "app" / "templates" / "_feature_header.html").read_text()


def _rule(selector: str) -> str:
    start = CSS.index(selector + " {")
    return CSS[start:CSS.index("}", start)]


def test_header_never_wraps_to_a_second_row():
    rule = _rule(".feature-header")
    assert "flex-wrap: nowrap" in rule
    # Clipping is the deliberate last resort below the width where the title and buttons alone
    # can't both fit -- without it they rendered on top of each other.
    assert "overflow: hidden" in rule


def test_title_count_and_arrow_stay_together_on_one_line():
    rule = _rule(".feature-header h1")
    assert "white-space: nowrap" in rule
    # flex:none is what stops the heading being squeezed until the arrow is pushed off the line.
    assert "flex: none" in rule


def test_action_buttons_never_wrap_or_shrink():
    rule = _rule(".feature-header .feature-actions > form,\n.feature-header .feature-actions button")
    assert "white-space: nowrap" in rule
    assert "flex: none" in rule


def test_status_summary_is_the_only_thing_that_gives_up_space():
    """The actions column is content-sized while the status column is minmax(0, 1fr) -- the
    minmax lower bound of 0 is the specific part that lets the summary collapse instead of
    forcing the row wider (a plain 1fr or an auto column would not)."""
    rule = _rule(".feature-header .topbar-right")
    assert "display: grid" in rule
    assert "grid-template-columns: minmax(0, 1fr) auto" in rule
    status_rule = _rule(".feature-header #check-status")
    assert "min-width: 0" in status_rule
    assert "overflow: hidden" in status_rule


def test_status_text_truncates_rather_than_wrapping():
    idle = _rule('.feature-header .status-badge.status-idle')
    assert "text-overflow: ellipsis" in idle
    badge = _rule(".feature-header .status-badge")
    assert "white-space: nowrap" in badge


def test_template_wraps_the_actions_so_the_two_column_split_exists():
    """The grid above needs exactly two children: the status and one actions container."""
    assert 'class="feature-actions"' in HEADER_TMPL
    assert 'class="feature-title-text"' in HEADER_TMPL
