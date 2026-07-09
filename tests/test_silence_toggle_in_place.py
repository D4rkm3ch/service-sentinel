"""An explicit ask: Silence/Unsilence should toggle in place exactly like Updates' Mark as
Read/Unread -- no full-page redirect back to the Logs/Compose list, just the button and the
title-row "Silenced" badge swapping via htmx (the badge via an out-of-band update, same
pattern as _read_toggle.html)."""

from app import db


def test_silencing_a_finding_does_not_redirect_and_updates_the_badge_in_place(client):
    fid, _ = db.upsert_finding("logs", "silence-toggle-test-container", "OOM crash", "crash", "critical", "desc")
    db.set_finding_status(fid, "active")

    resp = client.post(f"/findings/{fid}/silence")
    assert resp.status_code == 200  # not a 303 redirect
    assert 'hx-post="/findings/{}/unsilence"'.format(fid) in resp.text  # button flipped to Unsilence
    assert 'id="silence-status-badge" hx-swap-oob="true"' in resp.text
    assert 'badge badge-lg badge-silenced">Silenced</span>' in resp.text

    db.set_finding_status(fid, "active")


def test_unsilencing_a_finding_does_not_redirect_and_clears_the_badge_in_place(client):
    fid, _ = db.upsert_finding("compose", "/silence-toggle-test/compose.yml", "issue", "reliability", "warning", "desc")
    db.set_finding_status(fid, "silenced")

    resp = client.post(f"/findings/{fid}/unsilence")
    assert resp.status_code == 200
    assert 'hx-post="/findings/{}/silence"'.format(fid) in resp.text  # button flipped back to Silence
    assert "Silenced</span>" not in resp.text

    db.set_finding_status(fid, "silenced")


def test_finding_detail_page_renders_the_toggle_button_via_htmx_not_a_plain_form(client):
    fid, _ = db.upsert_finding("logs", "silence-toggle-test-container-2", "issue", "crash", "critical", "desc")
    db.set_finding_status(fid, "active")

    resp = client.get(f"/findings/{fid}")
    assert f'hx-post="/findings/{fid}/silence"' in resp.text
    assert f'action="/findings/{fid}/silence"' not in resp.text

    db.set_finding_status(fid, "silenced")
