"""A real-world report: once every finding for a subject was silenced, both the Logs/Compose
Issues table and the subject's own detail page stopped showing its real severity (critical,
warning, ...) and showed a generic "Silenced only" badge instead -- losing the classification
entirely. Fixed so the severity badge always reflects the worst finding regardless of active/
silenced state; a separate "Silenced" badge (not "Silenced only") is shown alongside it only
when nothing is currently active."""

from unittest.mock import patch

from app import db

_SUBJECT = "silenced-severity-test-container"
_COMPOSE_PATH = "/silenced-severity-test/compose.yml"


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

    # show_silenced=1 -- otherwise a subject with exactly one (silenced) finding redirects
    # straight to that finding's own detail page instead of rendering subject_findings.html.
    resp = client.get(f"/compose/file?path={_COMPOSE_PATH}&show_silenced=1")
    assert resp.status_code == 200
    assert 'badge-lg badge-sev-critical' in resp.text
    assert '<span class="badge badge-lg badge-silenced">Silenced</span>' in resp.text
    assert "Silenced only" not in resp.text

    db.set_finding_status(fid, "active")


def test_subject_page_with_zero_findings_shows_no_severity_badge(client):
    """Regression guard: a subject with genuinely no findings at all (e.g. reached via a
    mocked overview on an empty container) must not crash trying to label a None severity."""
    with patch("app.main._get_or_build_overview", return_value=None):
        resp = client.get("/logs/container/subject-with-truly-no-findings-at-all")
    assert resp.status_code == 200
    assert "badge-sev-None" not in resp.text
