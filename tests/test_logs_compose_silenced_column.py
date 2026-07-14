"""Follow-up to the Updates container-Silence feature: Logs/Compose only ever silence at the
finding level (no per-container toggle), but the bottom "All containers"/"All compose files"
tables now get the same kind of dedicated Silenced column Updates' Tracked containers table
has -- derived as "has findings, but every one of them is currently silenced" rather than an
explicit flag, since there's no button to set one directly for a whole container/file here."""

from app import db


def _cleanup(source: str, subject: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM findings WHERE source = ? AND subject = ?", (source, subject))
        table = "log_watch_state" if source == "logs" else "compose_file_state"
        col = "container_name" if source == "logs" else "file_path"
        conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (subject,))


def test_logs_all_containers_table_has_a_silenced_column(client):
    db.set_log_watch_checkpoint("silenced-col-logs-container")
    fid, _ = db.upsert_finding("logs", "silenced-col-logs-container", "OOM crash", "crash", "critical", "desc")
    db.set_finding_status(fid, "silenced")

    resp = client.get("/logs")
    assert "sort-link" in resp.text and "Silenced" in resp.text
    assert "csort=silenced" in resp.text
    section = resp.text[resp.text.index('id="logs-containers-table"'):]
    row = section[section.index("silenced-col-logs-container"):]
    row = row[:row.index("</tr>")]
    assert "badge-silenced\">Silenced</span>" in row

    _cleanup("logs", "silenced-col-logs-container")


def test_logs_all_containers_table_shows_a_dash_for_a_container_with_no_silenced_findings(client):
    db.set_log_watch_checkpoint("silenced-col-logs-healthy")

    resp = client.get("/logs")
    section = resp.text[resp.text.index('id="logs-containers-table"'):]
    row = section[section.index("silenced-col-logs-healthy"):]
    row = row[:row.index("</tr>")]
    assert "badge-silenced" not in row
    assert '<span class="meta">-</span>' in row

    _cleanup("logs", "silenced-col-logs-healthy")


def test_a_container_with_a_mix_of_active_and_silenced_findings_is_not_marked_silenced(client):
    """Only fully-silenced (no active findings left at all) counts -- a container that still
    has at least one active finding is still an actual issue, not a silenced one."""
    db.set_log_watch_checkpoint("silenced-col-logs-mixed")
    fid1, _ = db.upsert_finding("logs", "silenced-col-logs-mixed", "OOM crash", "crash", "critical", "desc")
    db.set_finding_status(fid1, "silenced")
    db.upsert_finding("logs", "silenced-col-logs-mixed", "disk pressure", "resource", "warning", "desc2")

    resp = client.get("/logs")
    section = resp.text[resp.text.index('id="logs-containers-table"'):]
    row = section[section.index("silenced-col-logs-mixed"):]
    row = row[:row.index("</tr>")]
    assert "badge-silenced" not in row

    _cleanup("logs", "silenced-col-logs-mixed")


def test_compose_all_files_table_has_a_silenced_column(client):
    db.set_compose_file_hash("silenced-col-compose.yml", "hash-abc")
    fid, _ = db.upsert_finding("compose", "silenced-col-compose.yml", "Missing restart policy", "reliability", "critical", "desc")
    db.set_finding_status(fid, "silenced")

    resp = client.get("/compose")
    assert "sort-link" in resp.text and "Silenced" in resp.text
    assert "csort=silenced" in resp.text
    section = resp.text[resp.text.index('id="compose-files-table"'):]
    row = section[section.index("silenced-col-compose.yml"):]
    row = row[:row.index("</tr>")]
    assert "badge-silenced\">Silenced</span>" in row

    _cleanup("compose", "silenced-col-compose.yml")
