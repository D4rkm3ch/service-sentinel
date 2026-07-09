"""A real-world report: the Overview card's live "Checking…" indicator duplicated itself on
every poll tick ("Checking... Checking... Checking...") instead of updating in place. Root
cause: the poller span's hx-target="this"/hx-swap="outerHTML" replaced only the poller itself,
but each poll response also carried a brand-new (non-oob) status-slot span as sibling content --
so every poll left the old status-slot behind and added another one alongside it, forever.
Fixed by splitting into an initial-embed template (_card_status.html, plain elements, used once
per real page/card render) and a poll-response template (_card_status_poll.html, whose status
slot is an out-of-band update against the *same* id, plus a fresh self-perpetuating poller)."""

from pathlib import Path

from app import check_state

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def test_poll_response_updates_the_status_slot_out_of_band_not_as_a_duplicate():
    text = (TEMPLATES / "_card_status_poll.html").read_text()
    assert 'id="card-status-{{ feature }}" class="card-status-slot" hx-swap-oob="true"' in text


def test_initial_embed_does_not_carry_hx_swap_oob():
    """The initial embed (used once when the card/page itself is first rendered) must NOT be
    oob -- there's nothing yet in the DOM for it to out-of-band-target."""
    text = (TEMPLATES / "_card_status.html").read_text()
    assert "hx-swap-oob" not in text


def test_card_status_poll_response_contains_exactly_one_status_slot(client):
    check_state._state["updates"] = {"running": True, "last_result": None, "last_run_at": None}
    resp = client.get("/status/card/updates?prev_running=true")
    assert resp.text.count('id="card-status-updates"') == 1
    check_state.release_running("updates")


def test_card_status_shows_live_progress_text_matching_the_feature_page(client):
    """The card's running indicator reuses the exact same progress_text the feature's own
    status badge computes (live "(N/total)" numbers for Updates, plain "Checking…" for
    Logs/Compose), not a simplified stand-in -- per an explicit ask to match Updates' page
    exactly, or at minimum show the same spinner + "Checking" text."""
    check_state._state["updates"] = {"running": True, "last_result": None, "last_run_at": None}
    check_state._progress["updates"] = {"stage": "checking", "done": 3, "total": 59}
    resp = client.get("/status/card/updates")
    assert "Checking for updates (3/59)" in resp.text
    assert 'class="spinner"' in resp.text
    check_state.release_running("updates")
    check_state._progress["updates"] = {"stage": None, "done": 0, "total": 0}
