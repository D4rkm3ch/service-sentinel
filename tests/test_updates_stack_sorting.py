"""The Updates stack page's members table was the one findings-style table in the app with no
sortable headers at all -- its Logs twin (logs_stack_detail.html) already had them. Locks in
the new _sort_updates_stack_members: same column set as the table, Importance keeping the main
Updates table's tiering (unclassified pinned first, notes-not-found below bugfix, up-to-date
members always last), and the template actually rendering sort links."""

from pathlib import Path

from app.main import _sort_updates_stack_members

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _member(name, severity=None, error=None, notes=None, status="unread", created_at="", pending=True):
    latest = None
    if pending:
        latest = {"id": 1, "severity": severity, "error": error, "release_notes_raw": notes,
                  "status": status, "created_at": created_at}
    return {"container_name": name, "image_repo": f"owner/{name}", "tag": "latest", "latest_update": latest}


def test_importance_tiers_unclassified_first_then_ranked_then_up_to_date():
    members = [
        _member("uptodate", pending=False),
        _member("bugfix", severity="bugfix", notes="notes"),
        _member("breaking", severity="breaking", notes="notes"),
        _member("nonotes", severity=None, notes=None),          # notes-not-found: below bugfix
        _member("errored", severity=None, error="boom", notes="notes"),  # unclassified: pinned first
    ]
    ordered = [m["container_name"] for m in _sort_updates_stack_members(members, "importance", "asc")]
    assert ordered == ["errored", "breaking", "bugfix", "nonotes", "uptodate"]

    # Direction flips only the ranked tier -- pinned/up-to-date stay put.
    ordered = [m["container_name"] for m in _sort_updates_stack_members(members, "importance", "desc")]
    assert ordered == ["errored", "nonotes", "bugfix", "breaking", "uptodate"]


def test_read_column_tiers_error_unread_read_then_up_to_date():
    members = [
        _member("read-one", severity="bugfix", notes="n", status="read"),
        _member("uptodate", pending=False),
        _member("unread-one", severity="bugfix", notes="n", status="unread"),
        _member("errored", error="boom", notes="n"),
    ]
    ordered = [m["container_name"] for m in _sort_updates_stack_members(members, "read", "asc")]
    assert ordered == ["errored", "unread-one", "read-one", "uptodate"]


def test_container_image_and_detected_sorts():
    members = [
        _member("bbb", severity="bugfix", notes="n", created_at="2026-02-01"),
        _member("aaa", severity="bugfix", notes="n", created_at="2026-03-01"),
    ]
    assert [m["container_name"] for m in _sort_updates_stack_members(members, "container", "asc")] == ["aaa", "bbb"]
    assert [m["container_name"] for m in _sort_updates_stack_members(members, "image", "desc")] == ["bbb", "aaa"]
    assert [m["container_name"] for m in _sort_updates_stack_members(members, "detected", "desc")] == ["aaa", "bbb"]


def test_stack_detail_template_renders_sortable_headers_like_its_logs_twin():
    text = (TEMPLATES / "stack_detail.html").read_text()
    assert 'import "_sort_header.html" as sh' in text
    for column in ("container", "image", "detected", "importance", "read"):
        assert f"'{column}'" in text, f"stack_detail.html lost the sortable {column} header"
