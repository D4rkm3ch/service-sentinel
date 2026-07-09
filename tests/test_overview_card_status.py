"""A real-world feedback ask: the Overview page should show the same live "a check is running"
indicator next to each feature's title that Updates/Logs/Compose show in their own topbar, so a
scheduled check is visible from the dashboard without having to open the feature's own tab."""

from pathlib import Path

from app import check_state

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def test_feature_card_embeds_a_status_slot_next_to_its_title():
    text = (TEMPLATES / "_feature_card.html").read_text()
    title_pos = text.index("feature-card-title")
    status_include_pos = text.index('include "_card_status.html"')
    toggle_pos = text.index("toggle-switch")
    assert title_pos < status_include_pos < toggle_pos


def test_card_status_route_reflects_running_state(client):
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    resp = client.get("/status/card/updates")
    assert resp.status_code == 200
    assert "Checking" not in resp.text

    check_state.set_running("updates")
    resp = client.get("/status/card/updates")
    assert "Checking" in resp.text
    check_state.release_running("updates")


def test_card_status_route_rejects_unknown_feature(client):
    resp = client.get("/status/card/nonsense")
    assert resp.status_code == 404


def test_card_status_refreshes_the_whole_card_only_on_a_genuine_transition(client):
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    # Still running (per prev_running) -> no whole-card oob refresh needed yet.
    resp = client.get("/status/card/updates?prev_running=true")
    assert resp.status_code == 200
    # This call reports running=False now, with prev_running=true, i.e. a genuine transition:
    # the whole card should be re-rendered (oob) so its headline/detail isn't left stale.
    assert 'id="card-updates"' in resp.text
    assert 'hx-swap-oob="true"' in resp.text


def test_card_status_does_not_refresh_the_card_while_idle_with_no_transition(client):
    check_state._state["updates"] = {"running": False, "last_result": None, "last_run_at": None}
    resp = client.get("/status/card/updates")  # prev_running defaults to False -- no transition
    assert 'id="card-updates"' not in resp.text
