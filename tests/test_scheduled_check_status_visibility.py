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
    text = (TEMPLATES / "_status.html").read_text()
    assert "idle_poll_delay_ms" in text
    assert "poll_delay_ms if state.running else idle_poll_delay_ms" in text


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
