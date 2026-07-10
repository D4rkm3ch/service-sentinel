"""Follow-up: the new Silenced column (Tracked Containers on Updates, or the equivalent bottom
table on Logs/Compose) rendered as a bare <th>, not a real sort link -- clicking it did nothing,
same class of bug the Stack column had before it was fixed."""

from app import db


def _cleanup_update(container_name: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM container_state WHERE container_name = ?", (container_name,))
        conn.execute("DELETE FROM updates WHERE container_name = ?", (container_name,))


def _cleanup_log(container_name: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM log_watch_state WHERE container_name = ?", (container_name,))
        conn.execute("DELETE FROM findings WHERE source = 'logs' AND subject = ?", (container_name,))


def test_tracked_containers_actually_sorts_by_silenced(client):
    db.upsert_container_state("silence-sort-a", "owner/silence-sort-a", "latest", "sha256:a")
    db.upsert_container_state("silence-sort-z", "owner/silence-sort-z", "latest", "sha256:z")
    db.set_container_silenced("silence-sort-z", True)  # alphabetically last, but silenced

    resp = client.get("/updates?csort=silenced&cdir=asc")
    assert resp.status_code == 200
    section = resp.text[resp.text.index("Tracked Containers"):]
    z_pos = section.index("silence-sort-z")
    a_pos = section.index("silence-sort-a")
    # Silenced sorts first ascending, so the silenced one (z) should appear before the
    # non-silenced one (a) despite the reverse alphabetical order.
    assert z_pos < a_pos

    _cleanup_update("silence-sort-a")
    _cleanup_update("silence-sort-z")


def test_logs_all_containers_actually_sorts_by_silenced(client):
    db.set_log_watch_checkpoint("silence-sort-logs-a")
    db.set_log_watch_checkpoint("silence-sort-logs-z")
    fid, _ = db.upsert_finding("logs", "silence-sort-logs-z", "old warning", "leak", "warning", "desc")
    db.set_finding_status(fid, "silenced")

    resp = client.get("/logs?csort=silenced&cdir=asc")
    assert resp.status_code == 200
    section = resp.text[resp.text.index('id="logs-containers-table"'):]
    z_pos = section.index("silence-sort-logs-z")
    a_pos = section.index("silence-sort-logs-a")
    assert z_pos < a_pos

    _cleanup_log("silence-sort-logs-a")
    _cleanup_log("silence-sort-logs-z")
