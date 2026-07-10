"""A real-world ask: Logs and Compose should default to showing the worst problems first, not
whatever happened to be seen most recently or alphabetically -- the Issues table defaults to
severity (critical first), and the All containers/All files table defaults to status (an
active issue before healthy).

Findings are forced back to "active" right after upsert (not just relying on upsert's own
default) since the `client` fixture's database is session-scoped and persists across repeated
runs -- a re-run would otherwise silently find its own leftover, already-silenced rows."""


def test_logs_page_defaults_to_severity_and_status_sort():
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text()
    match = re.search(r'def logs_page\(request: Request, show_silenced: bool = False,\s*\n\s*(.+)\)', text)
    assert match, "logs_page signature not found"
    sig = match.group(1)
    assert 'sort: str = "severity"' in sig
    assert 'dir: str = "asc"' in sig
    assert 'csort: str = "status"' in sig


def test_compose_page_defaults_to_severity_and_status_sort():
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text()
    match = re.search(r'def compose_page\(request: Request, show_silenced: bool = False,\s*\n\s*(.+)\)', text)
    assert match, "compose_page signature not found"
    sig = match.group(1)
    assert 'sort: str = "severity"' in sig
    assert 'dir: str = "asc"' in sig
    assert 'csort: str = "status"' in sig


def test_logs_page_lists_critical_issues_before_lower_severity_ones(client):
    from app import db
    fid_warn, _ = db.upsert_finding("logs", "default-sort-test-warning", "slow", "startup", "warning", "desc")
    fid_crit, _ = db.upsert_finding("logs", "default-sort-test-critical", "crash", "crash", "critical", "desc")
    db.set_finding_status(fid_warn, "active")
    db.set_finding_status(fid_crit, "active")

    resp = client.get("/logs")
    assert resp.status_code == 200
    crit_pos = resp.text.index("default-sort-test-critical")
    warn_pos = resp.text.index("default-sort-test-warning")
    assert crit_pos < warn_pos

    db.set_finding_status(fid_warn, "silenced")
    db.set_finding_status(fid_crit, "silenced")


def test_logs_page_lists_issue_containers_before_healthy_ones(client):
    from app import db
    db.set_log_watch_checkpoint("default-sort-test-healthy-container")
    db.set_log_watch_checkpoint("default-sort-test-issue-container")
    fid, _ = db.upsert_finding("logs", "default-sort-test-issue-container", "crash", "crash", "critical", "desc")
    db.set_finding_status(fid, "active")

    resp = client.get("/logs")
    assert resp.status_code == 200
    all_containers_section = resp.text[resp.text.index("Tracked Containers"):]
    issue_pos = all_containers_section.index("default-sort-test-issue-container")
    healthy_pos = all_containers_section.index("default-sort-test-healthy-container")
    assert issue_pos < healthy_pos

    db.set_finding_status(fid, "silenced")
