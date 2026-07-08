"""base.html's button-disabling poller (Stage 6 polish) finds every Check now / Reset &
re-check button to dim while an Updates check is running purely by CSS class -- there's no
server-side registry of "which buttons exist on which page." These are template-content checks
(not full HTTP renders, since the Stack page needs a real compose tree to render at all) that
lock in the contract: every button the poller is meant to control must carry the class, and the
buttons that must NOT be touched by it (the permanently-disabled per-item Retry) must not."""

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def test_feature_header_check_now_and_global_reset_carry_the_class():
    text = (TEMPLATES / "_feature_header.html").read_text()
    # Two Check now button branches (updates vs logs/compose) plus the global Reset & re-check
    # form's button -- three occurrences of the class total.
    assert text.count("updates-action-btn") == 3
    # The updates-feature Check now button must NOT carry hx-disabled-elt -- it only covers
    # the fast POST round-trip (the real check runs in a background thread), which was causing
    # a visible re-enable/re-disable flicker; base.html's instant beforeRequest handler plus
    # its poll now own the whole disabled lifecycle for every .updates-action-btn instead.
    updates_branch_start = text.index("{% if feature == 'updates' %}")
    updates_branch_end = text.index("{% else %}")
    assert "hx-disabled-elt" not in text[updates_branch_start:updates_branch_end]
    # logs/compose's Check now button is untouched by this change -- still self-disables for
    # its own POST round-trip exactly as before.
    assert "hx-disabled-elt" in text[updates_branch_end:]


def test_base_html_disables_instantly_on_before_request_not_just_on_poll():
    """Regression test for the "takes 0.5-1s to dim" report: base.html's poller must disable
    every .updates-action-btn the instant htmx actually sends a request for one of them
    (htmx:beforeRequest -- fires only after any hx-confirm was accepted), not rely solely on
    the once-a-second poll to notice."""
    text = (TEMPLATES / "base.html").read_text()
    assert "htmx:beforeRequest" in text
    assert "applyRunningState(true)" in text


def test_stack_detail_retry_and_reset_carry_the_class():
    text = (TEMPLATES / "stack_detail.html").read_text()
    assert text.count("updates-action-btn") == 2  # Retry and Reset & re-check


def test_detail_page_check_now_and_reset_always_carry_the_class():
    text = (TEMPLATES / "detail.html").read_text()
    # Check Now and Reset & Re-check are unconditional -- exactly 2 occurrences outside the
    # Regenerate AI Response if/else block (checked separately below), which itself contributes
    # a 3rd only in its enabled branch.
    regen_start = text.index("{% if update.release_notes_raw %}")
    regen_end = text.index("{% endif %}", regen_start)
    assert text[:regen_start].count("updates-action-btn") + text[regen_end:].count("updates-action-btn") == 2


def test_detail_page_regenerate_button_class_depends_on_whether_notes_exist():
    """Regenerate AI Response only carries the class (and is only clickable) in the branch
    that renders when release_notes_raw exists -- the no-notes branch stays permanently
    disabled without it, same reasoning as the old permanently-disabled Retry button: base.html's
    poller unconditionally sets .disabled = running, which would wrongly re-enable a button
    with nothing to regenerate from the instant any check elsewhere finishes."""
    text = (TEMPLATES / "detail.html").read_text()
    regen_start = text.index("{% if update.release_notes_raw %}")
    regen_else = text.index("{% else %}", regen_start)
    regen_end = text.index("{% endif %}", regen_else)

    enabled_branch = text[regen_start:regen_else]
    disabled_branch = text[regen_else:regen_end]
    assert "updates-action-btn" in enabled_branch
    assert "hx-post" in enabled_branch
    assert "updates-action-btn" not in disabled_branch
    assert "disabled" in disabled_branch
