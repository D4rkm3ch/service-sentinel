"""An explicit ask: findings get the same Read/Unread feature Updates has for its updates --
per-finding granularity, a column on the main pages (both the per-subject findings table and an
aggregate indicator on the main Issues table), a Mark as Read/Unread toggle button on the
finding's own page, and auto-mark-as-read on viewing it (mirroring update_detail's own
behavior)."""

from app import db

_SUBJECT = "read-unread-test-container"


def test_new_findings_default_to_unread():
    fid, _ = db.upsert_finding("logs", _SUBJECT, "fresh finding", "crash", "critical", "desc")
    finding = db.get_finding(fid)
    assert finding["read_status"] == "unread"
    db.set_finding_status(fid, "silenced")


def test_viewing_a_finding_auto_marks_it_read(client):
    fid, _ = db.upsert_finding("logs", _SUBJECT, "auto-mark test", "crash", "critical", "desc")
    db.set_finding_read_status(fid, "unread")

    resp = client.get(f"/findings/{fid}")
    assert resp.status_code == 200
    assert 'badge badge-lg badge-read' in resp.text
    assert db.get_finding(fid)["read_status"] == "read"

    db.set_finding_status(fid, "silenced")


def test_viewing_a_subjects_findings_list_does_not_touch_read_status(client):
    """A real-world report: marking a finding Unread from its own page, then navigating back to
    the subject's findings list, silently flipped it back to Read -- an earlier attempt at
    auto-marking the whole visible list as read on every view (not just the single-item pages)
    made "Mark as Unread" effectively non-functional the moment you left the finding's own page.
    Reverted: only viewing an individual finding (or update) auto-marks it; viewing a list of
    findings must never mutate any of their read state."""
    subject = "read-unread-list-view-subject"
    fid_a, _ = db.upsert_finding("logs", subject, "list view a", "crash", "critical", "desc")
    fid_b, _ = db.upsert_finding("logs", subject, "list view b", "crash", "warning", "desc")
    try:
        db.set_finding_read_status(fid_a, "unread")
        db.set_finding_read_status(fid_b, "read")

        resp = client.get(f"/logs/container/{subject}")
        assert resp.status_code == 200
        assert db.get_finding(fid_a)["read_status"] == "unread"
        assert db.get_finding(fid_b)["read_status"] == "read"
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE subject = ?", (subject,))


def test_marking_a_finding_unread_survives_a_return_trip_to_the_subject_list(client):
    subject = "read-unread-survives-subject"
    fid_a, _ = db.upsert_finding("logs", subject, "survives a", "crash", "critical", "desc")
    fid_b, _ = db.upsert_finding("logs", subject, "survives b", "crash", "warning", "desc")
    try:
        client.post(f"/findings/{fid_a}/unread")
        assert db.get_finding(fid_a)["read_status"] == "unread"

        # Landing back on the subject's own list page (as a "back" navigation would) must not
        # silently undo that explicit choice.
        client.get(f"/logs/container/{subject}")
        assert db.get_finding(fid_a)["read_status"] == "unread"
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE subject = ?", (subject,))


def test_mark_as_read_and_unread_toggle_in_place(client):
    fid, _ = db.upsert_finding("logs", _SUBJECT, "toggle test", "crash", "critical", "desc")
    db.set_finding_read_status(fid, "read")

    resp = client.post(f"/findings/{fid}/unread")
    assert resp.status_code == 200
    assert 'hx-post="/findings/{}/read"'.format(fid) in resp.text
    assert 'id="read-status-badge" hx-swap-oob="true"' in resp.text
    assert db.get_finding(fid)["read_status"] == "unread"

    resp = client.post(f"/findings/{fid}/read")
    assert db.get_finding(fid)["read_status"] == "read"
    assert 'hx-post="/findings/{}/unread"'.format(fid) in resp.text

    db.set_finding_status(fid, "silenced")


def test_subject_findings_page_shows_a_read_column(client):
    compose_path = "/tmp/rr-test-compose/read-unread-test/compose.yml"
    fid, _ = db.upsert_finding("compose", compose_path, "col test 1", "reliability", "warning", "desc")
    fid2, _ = db.upsert_finding("compose", compose_path, "col test 2", "reliability", "critical", "desc")
    db.set_finding_read_status(fid, "unread")
    db.set_finding_read_status(fid2, "read")

    resp = client.get(f"/compose/file?path={compose_path}")
    # Read column header is a sortable link (see _sort_header.html), not a bare <th>.
    assert "sort=read" in resp.text
    assert "badge-unread\">Unread</span>" in resp.text
    assert "badge-read\">Read</span>" in resp.text

    db.set_finding_status(fid, "silenced")
    db.set_finding_status(fid2, "silenced")


def test_issues_table_shows_an_aggregate_unread_indicator(client):
    fid, _ = db.upsert_finding("logs", "read-unread-agg-test", "agg test", "crash", "critical", "desc")
    db.set_finding_status(fid, "active")
    db.set_finding_read_status(fid, "unread")

    resp = client.get("/logs")
    section = resp.text[resp.text.index("read-unread-agg-test"):]
    row = section[:section.index("</tr>")]
    assert "badge-unread\">Unread</span>" in row

    db.set_finding_read_status(fid, "read")
    resp = client.get("/logs")
    section = resp.text[resp.text.index("read-unread-agg-test"):]
    row = section[:section.index("</tr>")]
    assert "badge-read\">Read</span>" in row

    db.set_finding_status(fid, "silenced")
