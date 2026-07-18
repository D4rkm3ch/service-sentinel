"""db.get_active_findings_by_subject and db.resolve_finding -- the pieces that let Logs' AI
triage compare a subject's already-tracked open issues against fresh log evidence and, when the
AI judges an issue resolved, delete it outright so the subject reads as healthy again."""

import pytest

from app import db

db.init_db()


@pytest.fixture(autouse=True)
def clean_findings():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM findings WHERE source = 'logs'")
    yield
    with db.get_conn() as conn:
        conn.execute("DELETE FROM findings WHERE source = 'logs'")


def test_get_active_findings_by_subject_returns_empty_for_no_subjects():
    assert db.get_active_findings_by_subject("logs", []) == {}


def test_get_active_findings_by_subject_returns_only_requested_and_active():
    db.upsert_finding("logs", "web", "Connection refused", "error", "critical", "desc a")
    db.upsert_finding("logs", "db", "Out of memory", "error", "critical", "desc b")
    finding_id, _ = db.upsert_finding("logs", "cache", "Segfault", "error", "critical", "desc c")
    db.set_finding_status(finding_id, "silenced")

    result = db.get_active_findings_by_subject("logs", ["web", "db", "cache", "other"])

    assert set(result.keys()) == {"web", "db"}
    assert result["web"][0]["title"] == "Connection refused"
    assert result["web"][0]["description"] == "desc a"
    assert result["db"][0]["title"] == "Out of memory"


def test_get_active_findings_by_subject_groups_multiple_findings_per_subject():
    db.upsert_finding("logs", "web", "Connection refused", "error", "critical", "desc a")
    db.upsert_finding("logs", "web", "Timeout", "error", "warning", "desc b")

    result = db.get_active_findings_by_subject("logs", ["web"])

    titles = {f["title"] for f in result["web"]}
    assert titles == {"Connection refused", "Timeout"}


def test_get_active_findings_by_subject_only_scoped_to_given_source():
    db.upsert_finding("compose", "web", "Bad config", "error", "critical", "desc")

    result = db.get_active_findings_by_subject("logs", ["web"])

    assert result == {}


def test_get_active_findings_by_subject_include_silenced_returns_both():
    active_id, _ = db.upsert_finding("logs", "web", "Connection refused", "error", "critical", "desc a")
    silenced_id, _ = db.upsert_finding("logs", "web", "Segfault", "error", "critical", "desc c")
    db.set_finding_status(silenced_id, "silenced")

    result = db.get_active_findings_by_subject("logs", ["web"], include_silenced=True)

    titles_by_status = {f["title"]: f["status"] for f in result["web"]}
    assert titles_by_status == {"Connection refused": "active", "Segfault": "silenced"}
    assert active_id and silenced_id  # sanity: both really got created


def test_get_active_findings_by_subject_without_include_silenced_still_excludes_it():
    finding_id, _ = db.upsert_finding("logs", "web", "Segfault", "error", "critical", "desc c")
    db.set_finding_status(finding_id, "silenced")

    result = db.get_active_findings_by_subject("logs", ["web"])

    assert result == {}


def test_get_active_findings_by_subject_default_entries_report_active_status():
    db.upsert_finding("logs", "web", "Connection refused", "error", "critical", "desc a")

    result = db.get_active_findings_by_subject("logs", ["web"])

    assert result["web"][0]["status"] == "active"


def test_resolve_finding_deletes_matching_active_finding():
    db.upsert_finding("logs", "web", "Connection refused", "error", "critical", "desc")

    deleted = db.resolve_finding("logs", "web", "Connection refused")

    assert deleted is True
    assert db.list_findings_for_subject("logs", "web") == []


def test_resolve_finding_is_case_and_whitespace_insensitive_like_the_fingerprint():
    db.upsert_finding("logs", "web", "Connection Refused", "error", "critical", "desc")

    deleted = db.resolve_finding("logs", "web", "  connection refused  ")

    assert deleted is True


def test_resolve_finding_returns_false_when_nothing_matches():
    assert db.resolve_finding("logs", "web", "Nonexistent issue") is False


def test_resolve_finding_does_not_cross_resolve_a_different_subjects_finding():
    db.upsert_finding("logs", "web", "Connection refused", "error", "critical", "desc")

    deleted = db.resolve_finding("logs", "other-subject", "Connection refused")

    assert deleted is False
    assert len(db.list_findings_for_subject("logs", "web")) == 1


def test_resolve_finding_does_not_delete_a_silenced_finding():
    finding_id, _ = db.upsert_finding("logs", "web", "Connection refused", "error", "critical", "desc")
    db.set_finding_status(finding_id, "silenced")

    deleted = db.resolve_finding("logs", "web", "Connection refused")

    assert deleted is False
    assert db.get_finding(finding_id) is not None


def test_upsert_finding_recurrence_bumps_a_silenced_finding_quietly_without_reviving_it():
    """A silenced finding that recurs under the exact same title (as the AI is now instructed to
    reuse, rather than inventing new wording) must bump its occurrence count and refresh its
    description, but stay silenced -- not come back as a brand-new unread duplicate. See
    upsert_finding's own docstring."""
    finding_id, is_new = db.upsert_finding(
        "logs", "web", "Backend unreachable", "error", "critical", "cannot reach 10.0.0.5:8080"
    )
    db.set_finding_status(finding_id, "silenced")
    assert is_new is True

    same_id, is_new_again = db.upsert_finding(
        "logs", "web", "Backend unreachable", "error", "critical",
        "cannot reach 10.0.0.5:8080 or 10.0.0.9:8080",
    )

    assert same_id == finding_id
    assert is_new_again is False
    finding = db.get_finding(finding_id)
    assert finding["status"] == "silenced"
    assert finding["occurrence_count"] == 2
    assert finding["description_markdown"] == "cannot reach 10.0.0.5:8080 or 10.0.0.9:8080"
