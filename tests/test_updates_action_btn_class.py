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
    assert "updates-action-btn" in text
    # Only the updates feature's Check now button should get it -- logs/compose have their
    # own independent running flag and weren't part of this ask.
    assert "'updates-action-btn' if feature == 'updates' else ''" in text


def test_stack_detail_retry_and_reset_carry_the_class():
    text = (TEMPLATES / "stack_detail.html").read_text()
    assert text.count("updates-action-btn") == 2  # Retry and Reset & re-check


def test_detail_page_check_now_and_reset_carry_the_class_but_disabled_retry_does_not():
    text = (TEMPLATES / "detail.html").read_text()
    assert text.count("updates-action-btn") == 2  # Check now and Reset & re-check
    # The permanently-disabled Retry button must never gain this class -- base.html's poller
    # unconditionally sets .disabled = running, which would wrongly re-enable a
    # not-yet-implemented button the instant any check elsewhere finishes.
    start = text.index("<button type=\"button\" disabled")
    end = text.index("</button>", start)
    assert "updates-action-btn" not in text[start:end]
