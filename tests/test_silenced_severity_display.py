"""A real-world report: once every finding for a subject was silenced, both the Logs/Compose
Issues table and the subject's own detail page stopped showing its real severity (critical,
warning, ...) and showed a generic "Silenced only" badge instead -- losing the classification
entirely. Fixed so the severity badge always reflects the worst finding regardless of active/
silenced state; a separate "Silenced" badge (not "Silenced only") is shown alongside it only
when nothing is currently active."""

from unittest.mock import patch

from app import db

_SUBJECT = "silenced-severity-test-container"
_COMPOSE_PATH = "/tmp/rr-test-compose/silenced-severity-test/compose.yml"


def test_issues_table_shows_real_severity_for_a_fully_silenced_subject(client):
    fid, _ = db.upsert_finding("logs", _SUBJECT, "OOM crash", "crash", "critical", "desc")
    db.set_finding_status(fid, "silenced")

    resp = client.get("/logs?show_silenced=1")
    assert resp.status_code == 200
    section = resp.text[resp.text.index(_SUBJECT):]
    row = section[:section.index("</tr>")]
    assert "badge-sev-critical" in row
    assert "Silenced only" not in row

    db.set_finding_status(fid, "active")  # leave state clean-ish; findings persist across runs


def test_subject_page_shows_real_severity_badge_and_plain_silenced_wording(client):
    fid, _ = db.upsert_finding("compose", _COMPOSE_PATH, "Missing restart policy", "reliability", "critical", "desc")
    db.set_finding_status(fid, "active")  # ensure a clean starting state regardless of leftovers
    db.set_finding_status(fid, "silenced")
    # A second finding so this subject has 2+ findings and actually renders subject_findings.html
    # -- a subject with exactly one finding redirects straight to that finding's own detail page.
    fid2, _ = db.upsert_finding("compose", _COMPOSE_PATH, "Another finding", "reliability", "warning", "desc2")
    db.set_finding_status(fid2, "silenced")

    resp = client.get(f"/compose/file?path={_COMPOSE_PATH}&show_silenced=1")
    assert resp.status_code == 200
    assert 'badge-lg badge-sev-critical' in resp.text
    # The title-row badge says plain "Silenced" (not "Silenced only"), same as Logs' own subject
    # page already does for a fully-silenced subject -- this WAS gated off for Compose (a
    # since-fixed parity gap, see subject_findings.html's action-row un-gating), so it's
    # expected here now, not absent.
    assert '<span class="badge badge-lg badge-silenced">Silenced</span>' in resp.text
    assert "Silenced only" not in resp.text
    assert "2 silenced" in resp.text

    db.set_finding_status(fid2, "active")
    db.set_finding_status(fid, "active")


def test_subject_page_with_zero_findings_shows_no_severity_badge(client):
    """Regression guard: a subject with genuinely no findings at all (e.g. reached via a
    mocked overview on an empty container) must not crash trying to label a None severity."""
    with patch("app.main._get_or_build_overview", return_value=None):
        resp = client.get("/logs/container/subject-with-truly-no-findings-at-all")
    assert resp.status_code == 200
    assert "badge-sev-None" not in resp.text
