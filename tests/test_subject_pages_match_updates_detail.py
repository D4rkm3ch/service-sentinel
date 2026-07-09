"""Logs and Compose's subject page (a container's/file's list of findings) and finding detail
page were the last pieces still styled differently from Updates' own detail.html -- plain
<section>, no title-row badges, no back-link-row wrapper. Brought into the same visual language:
panel-wrapped content, a subject-title-row with severity badges, and a back-link-row.

Subject names here are unique to this test file (not "sonarr") since the `client` fixture's
database is session-scoped and persists across repeated runs -- a generic name risks colliding
with leftover rows (and their status) from an earlier run."""

from unittest.mock import patch

from app import db

_SUBJECT = "subject-pages-test-container"
_COMPOSE_PATH = "/subject-pages-test/compose.yml"


def test_subject_findings_page_uses_panel_and_subject_title_row(client):
    with patch("app.summarizer.summarize_findings_overview", return_value=None):
        fid1, _ = db.upsert_finding("logs", _SUBJECT, "OOM crash", "crash", "critical", "desc one")
        fid2, _ = db.upsert_finding("logs", _SUBJECT, "Slow start", "startup", "warning", "desc two")
    # Force both active regardless of whatever status a previous run of this test left them in.
    db.set_finding_status(fid1, "active")
    db.set_finding_status(fid2, "active")

    resp = client.get(f"/logs/container/{_SUBJECT}")
    assert resp.status_code == 200
    assert '<section class="panel">' in resp.text
    assert '<div class="subject-title-row">' in resp.text
    assert '<div class="back-link-row">' in resp.text
    assert 'badge-lg badge-sev-critical' in resp.text  # top severity across the two findings

    db.set_finding_status(fid1, "silenced")
    db.set_finding_status(fid2, "silenced")


def test_finding_detail_page_uses_panel_and_action_row(client):
    fid, _ = db.upsert_finding("compose", _COMPOSE_PATH, "Missing restart policy", "reliability", "warning", "desc")
    db.set_finding_status(fid, "active")
    finding = db.get_finding(fid)

    resp = client.get(f"/findings/{finding['id']}")
    assert resp.status_code == 200
    assert '<section class="panel">' in resp.text
    assert '<div class="subject-title-row">' in resp.text
    assert '<div class="back-link-row">' in resp.text
    assert 'hx-post="/findings/{}/silence"'.format(finding["id"]) in resp.text

    db.set_finding_status(fid, "silenced")
