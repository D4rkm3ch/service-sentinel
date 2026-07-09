"""A real-world report: the finding detail page showed each status twice -- once as the
title-row badge, once again as a second, unwanted badge sitting right next to the Mark as
Read/Unsilence buttons. Root cause: the action row directly {% include %}d _finding_read_
toggle.html/_silence_toggle.html, whose hx-swap-oob spans are only treated as out-of-band by
htmx on a POST response -- on a plain page load (no htmx involved at all) the "oob" span just
renders as ordinary, visible, duplicate content exactly where the include put it. Fixed by
inlining the button-only markup directly in finding_detail.html (matching how detail.html's
own Mark as Read/Unread has always worked) and reserving the two toggle partials for what they
were always meant for: the POST route's own response, which is a genuine htmx swap."""

from app import db

_SUBJECT = "no-duplicate-badges-test"


def test_finding_detail_page_shows_each_status_badge_exactly_once(client):
    fid, _ = db.upsert_finding("logs", _SUBJECT, "dup badge test", "crash", "critical", "desc")
    db.set_finding_read_status(fid, "read")
    db.set_finding_status(fid, "silenced")

    resp = client.get(f"/findings/{fid}")
    assert resp.status_code == 200
    assert resp.text.count("badge-lg badge-read") == 1
    assert resp.text.count("badge-lg badge-silenced") == 1
    assert resp.text.count("Unsilence") == 1
    assert resp.text.count("Mark as Unread") == 1

    db.set_finding_status(fid, "active")


def test_finding_detail_page_does_not_render_hx_swap_oob_on_initial_load(client):
    """The oob spans belong only to the POST-response partials -- the initial GET must not
    render an hx-swap-oob attribute at all."""
    fid, _ = db.upsert_finding("compose", "/no-duplicate-badges-test/compose.yml", "dup test 2", "reliability", "warning", "desc")
    resp = client.get(f"/findings/{fid}")
    assert "hx-swap-oob" not in resp.text
    db.set_finding_status(fid, "silenced")
