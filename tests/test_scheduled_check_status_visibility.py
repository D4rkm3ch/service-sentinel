"""A real-world report: the status badge on Updates/Logs/Compose only ever started polling
when a button on that page was clicked -- a scheduled check firing (or a manual check kicked
off from a different tab) left the badge frozen on "No check has run yet." until the page was
reloaded, and none of the feature's action buttons dimmed either. Fixed by making the status
badge's own poller perpetual (it re-embeds itself in every response, at a slow idle cadence
while nothing is running, switching to the fast cadence the moment it notices state.running),
and by generalizing the per-feature running-state poll (base.html) that dims Check now buttons
to all three features instead of just Updates."""

from pathlib import Path
from unittest.mock import patch

from app import check_state

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def test_status_poller_is_always_present_not_gated_on_running():
    """Previously the poller span only existed inside the running branch, so once a check
    finished (or if the page loaded while idle) nothing was left to notice a future check
    starting on its own. The poller markup must not be nested inside the running-only branch
    in either file -- i.e. it must appear after the {% endif %} that closes the running/idle
    switch, so it renders unconditionally."""
    for name in ("_status.html", "_status_poll.html"):
        text = (TEMPLATES / name).read_text()
        endif_pos = text.rindex("{% endif %}")
        poller_pos = text.index('class="status-poller"')
        assert poller_pos > endif_pos, f"{name}: poller must render outside the if/else"


def test_idle_poll_uses_a_slower_cadence_than_the_running_poll():
    """Also fast-polls while other_running_feature is set (another feature's check, noticed via
    the idle poll) -- not just this page's own state.running -- so the "A check is currently
    running…" badge clears promptly once that other check finishes, not up to 3s later."""
    text = (TEMPLATES / "_status.html").read_text()
    assert "idle_poll_delay_ms" in text
    assert "poll_delay_ms if (state.running or other_running_feature) else idle_poll_delay_ms" in text


def test_status_poll_route_passes_prev_running_through(client):
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    resp = client.get("/updates/status-poll?prev_running=true")
    assert resp.status_code == 200
    # A genuine running -> idle transition (prev_running=true, now idle) must fire checkComplete
    # so the tables refresh.
    assert resp.headers.get("HX-Trigger") == "checkComplete"


def test_status_poll_does_not_fire_check_complete_on_every_idle_tick(client):
    """Regression guard: firing checkComplete on every idle poll (not just on a genuine
    transition) would re-trigger every table's "every 20s, checkComplete from:body" listener
    every _IDLE_POLL_DELAY_MS for nothing."""
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    resp = client.get("/updates/status-poll")  # prev_running defaults to False
    assert resp.headers.get("HX-Trigger") is None


def test_logs_and_compose_have_their_own_running_state_endpoints(client):
    check_state._state["logs"] = {"running": False, "last_result": None, "last_run_at": None}
    check_state._state["compose"] = {"running": False, "last_result": None, "last_run_at": None}
    assert client.get("/logs/running-state").json() == {"running": False}
    assert client.get("/compose/running-state").json() == {"running": False}

    check_state.set_running("logs")
    assert client.get("/logs/running-state").json() == {"running": True}
    check_state.release_running("logs")

    check_state.set_running("compose")
    assert client.get("/compose/running-state").json() == {"running": True}
    check_state.release_running("compose")


def test_base_html_button_poller_covers_all_three_features():
    text = (TEMPLATES / "base.html").read_text()
    assert '"updates", "logs", "compose"' in text


def test_feature_header_check_now_button_carries_the_generic_class():
    text = (TEMPLATES / "_feature_header.html").read_text()
    assert 'class="{{ feature }}-action-btn"' in text


# ---------------------------------------------------------------------------
# Cross-feature "A check is currently running…" badge -- replaces the old sitewide banner
# (base.html's #check-running-notice) with an inline variant of this same status badge, shown
# on a feature's OWN page while a DIFFERENT feature's check is running elsewhere.
# ---------------------------------------------------------------------------

def test_updates_page_shows_running_badge_while_logs_check_is_in_progress(client):
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    check_state.set_running("logs")
    try:
        resp = client.get("/updates/status-poll")
        assert "A check is currently running" in resp.text
        assert 'class="spinner"' in resp.text
    finally:
        check_state.release_running("logs")


def test_own_feature_running_takes_priority_over_another_features_running_badge(client):
    """If both this page's own check AND another feature's check happen to be running at once,
    show this page's own live progress -- it's more specific/relevant than the generic "a check
    is running" text, not a coin flip between the two."""
    check_state._state["updates"] = {"running": True, "last_result": None, "last_run_at": None}
    check_state.set_running("logs")
    try:
        resp = client.get("/updates/status-poll")
        assert "A check is currently running" not in resp.text
    finally:
        check_state.release_running("logs")
        check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}


def test_other_feature_badge_does_a_full_swap_only_on_the_transition_into_running(client):
    """Same anti-flicker contract as the own-feature spinner (see
    test_status_poll_does_not_re_render_the_spinner_node): the first tick that notices another
    feature's check must build the badge wrapper fresh (prev_badge_running=false), but steady-
    state ticks while it's still running must only swap the text span, never recreate the
    spinner node."""
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    check_state.set_running("logs")
    try:
        first = client.get("/updates/status-poll?prev_badge_running=false")
        assert 'id="check-status-inner" hx-swap-oob="true"' in first.text
        assert 'class="spinner"' in first.text

        steady = client.get("/updates/status-poll?prev_badge_running=true")
        assert 'id="check-status-inner" hx-swap-oob="true"' not in steady.text
        assert 'class="spinner"' not in steady.text
        assert 'id="check-status-text" hx-swap-oob="true"' in steady.text
    finally:
        check_state.release_running("logs")


def test_updates_page_status_poller_carries_prev_badge_running_forward():
    text = (TEMPLATES / "_status.html").read_text()
    assert "prev_badge_running" in text
    text = (TEMPLATES / "_status_poll.html").read_text()
    assert "prev_badge_running" in text


# ---------------------------------------------------------------------------
# Stack/service/finding detail pages -- no server-rendered status badge of their own, just a
# permanently-present #item-recheck-status span next to their buttons, still only ever filled in
# by htmx with that item's own scoped-check result. The sitewide top banner (see below) is what
# tells the operator a check is running on every page now, including these -- there's no more
# separate "a check is running elsewhere" text injected next to this span.
# ---------------------------------------------------------------------------

def test_every_sub_page_has_the_persistent_item_recheck_status_anchor():
    """Regression guard: the client-side indicator only works because this span always exists,
    even when idle -- if a future edit ever made it conditional, the indicator would have
    nowhere to render on a fresh page load."""
    for name in (
        "detail.html", "finding_detail.html", "logs_stack_detail.html",
        "stack_detail.html", "subject_findings.html",
    ):
        text = (TEMPLATES / name).read_text()
        assert '<span id="item-recheck-status"></span>' in text, f"{name} is missing the anchor"


# ---------------------------------------------------------------------------
# Main page badge (#check-status) vs. the sitewide 1s poll -- the badge runs its own perpetual
# self-poll (see _status.html/_status_poll.html) that only switches to the fast cadence once IT
# notices something running, on its own schedule (up to idle_poll_delay_ms behind). Two real-
# world reports here: (1) the main page's own badge could sit on stale text for up to 3s after
# another feature's check had already started -- even though buttons were disabled and every
# sub-page's elsewhere-indicator had already updated within 1s, via this same sitewide poll;
# (2) on the OTHER edge -- a check finishing -- buttons/spinner reset the instant this poll
# notices running -> false, but the table below (Issues/Updates) only refreshes once the
# badge's OWN poll fires the checkComplete HX-Trigger (see _render_status_poll's docstring),
# so without nudging that too, the table could sit stale for a few seconds after the check
# visibly looked done.
# ---------------------------------------------------------------------------

def test_base_html_nudges_the_main_badge_on_either_running_state_transition():
    text = (TEMPLATES / "base.html").read_text()
    assert "function nudgeMainBadge" in text
    assert 'querySelector("#check-status .status-poller")' in text
    assert "htmx.ajax(" in text
    # Edge-triggered on an actual transition (either direction), not fired on every tick while
    # the state stays the same -- the badge's own self-poll is already fast by then, so a
    # second forced ajax call every second on top of it would just stack a redundant,
    # overlapping poll chain.
    assert "data.running !== wasAnyRunning" in text
    assert "nudgeMainBadge()" in text


# ---------------------------------------------------------------------------
# Sitewide "a check is running" top banner (base.html) -- replaces both the Check Now -> Cancel
# button flip and the sub-page "a check is running elsewhere" text: one banner, visible on every
# page, shows live progress and a single Cancel button while any check is running.
# ---------------------------------------------------------------------------

def test_base_html_has_the_banner_markup_and_it_lives_outside_the_content_block():
    text = (TEMPLATES / "base.html").read_text()
    assert 'id="check-running-banner"' in text
    assert 'id="check-running-banner-text"' in text
    assert 'id="check-running-banner-cancel"' in text
    # First thing inside <main> (so it inherits the same content width as every .panel below
    # it), but before {% block content %} -- so it's present (even if hidden) on every page,
    # not just ones that happen to render a {% block content %} with its own status markup.
    assert text.index('id="check-running-banner"') > text.index('class="topbar"')
    assert text.index('id="check-running-banner"') > text.index("<main>")
    assert text.index('id="check-running-banner"') < text.index("{% block content %}")


def test_base_html_banner_polls_the_consolidated_status_endpoint():
    text = (TEMPLATES / "base.html").read_text()
    assert 'fetch("/checks/status")' in text
    assert "function updateBanner" in text
    assert 'fetch("/checks/cancel"' in text


def test_checks_status_reports_the_running_feature_and_its_progress(client):
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    check_state._state["logs"] = {"running": False, "last_result": None, "last_run_at": None}
    check_state._state["compose"] = {"running": False, "last_result": None, "last_run_at": None}
    resp = client.get("/checks/status")
    assert resp.json() == {"running": False, "feature": None, "progress_text": "", "cancelling": False}

    check_state.set_running("logs")
    check_state.set_progress("logs", "checking_logs", 23, 59)
    try:
        resp = client.get("/checks/status")
        data = resp.json()
        assert data["running"] is True
        assert data["feature"] == "logs"
        assert "23/59" in data["progress_text"]
        assert data["cancelling"] is False

        check_state.request_cancel("logs")
        resp = client.get("/checks/status")
        assert resp.json()["cancelling"] is True
    finally:
        check_state.release_running("logs")


def test_checks_status_is_idle_when_nothing_is_running(client):
    for feature in check_state.FEATURES:
        check_state._state[feature] = {"running": False, "last_result": None, "last_run_at": None}
    resp = client.get("/checks/status")
    assert resp.json()["running"] is False
