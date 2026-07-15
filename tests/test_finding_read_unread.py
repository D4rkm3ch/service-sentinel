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
    db.set_finding_status(fid, "active")
    db.set_finding_status(fid2, "active")
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
