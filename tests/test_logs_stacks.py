"""An explicit ask: Logs gets a Stack view like Updates' -- containers grouped by which
compose stack they belong to, same table-view/rename/functionality shape. Stack identity and
naming are shared with Updates (stack_id is just the compose file's own path -- see stacks.py),
so renaming a stack from either page's rename form updates the same row and shows on both."""

from pathlib import Path

import pytest

from app import compose_lookup, db
from app.config import settings

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM log_watch_state")
    yield
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM log_watch_state")


def _compose_file(name, *services):
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _stack_id_for(container_name):
    return compose_lookup.match_container_to_stack(container_name, compose_lookup.build_stack_index())["stack_id"]


def test_logs_stack_page_lists_every_member_with_its_findings_summary(client):
    compose_file = _compose_file("logs-stack-1.yml", "sonarr", "radarr")
    try:
        db.set_log_watch_checkpoint("sonarr")
        db.set_log_watch_checkpoint("radarr")
        fid, _ = db.upsert_finding("logs", "radarr", "OOM crash", "crash", "critical", "desc")
        db.set_finding_status(fid, "active")
        db.set_finding_read_status(fid, "unread")
        stack_id = _stack_id_for("sonarr")

        resp = client.get(f"/logs/stack?id={stack_id}")
        assert resp.status_code == 200
        assert "sonarr" in resp.text
        assert "radarr" in resp.text
        assert "badge-sev-critical" in resp.text
        assert "2 service" in resp.text
        # Regression guard: the stack page's Read column used to always show "Read" regardless
        # of actual read status, since _findings_summary never computed unread_count at all.
        # Searched from <tbody> onward -- the stack's own AI-generated display name can
        # coincidentally match a member's container name too (as it does here).
        tbody = resp.text[resp.text.index("<tbody>"):]
        row = tbody[tbody.index(">radarr<"):]
        assert "badge-unread\">Unread</span>" in row[:row.index("</tr>")]

        db.set_finding_status(fid, "silenced")
    finally:
        compose_file.unlink()


def test_logs_stack_page_shows_healthy_for_a_member_with_no_findings(client):
    compose_file = _compose_file("logs-stack-2.yml", "prowlarr", "bazarr")
    try:
        db.set_log_watch_checkpoint("prowlarr")
        db.set_log_watch_checkpoint("bazarr")
        stack_id = _stack_id_for("prowlarr")

        resp = client.get(f"/logs/stack?id={stack_id}")
        assert resp.status_code == 200
        assert "badge-healthy\">Healthy</span>" in resp.text
    finally:
        compose_file.unlink()


def test_logs_containers_table_links_to_the_stack_page(client):
    compose_file = _compose_file("logs-stack-3.yml", "lidarr", "readarr")
    try:
        db.set_log_watch_checkpoint("lidarr")
        db.set_log_watch_checkpoint("readarr")
        stack_id = _stack_id_for("lidarr")

        resp = client.get("/logs")
        assert f'/logs/stack?id={stack_id}' in resp.text
        assert 'sort-link' in resp.text and 'Stack' in resp.text  # sortable header, not a bare <th>
        assert 'csort=stack' in resp.text
    finally:
        compose_file.unlink()


def test_the_all_containers_table_actually_sorts_by_stack(client):
    """Regression guard: the Stack column header used to be a bare <th>, not a real sort
    link -- clicking it (or requesting csort=stack directly) did nothing at all."""
    compose_file = _compose_file("logs-stack-sort.yml", "aaa-service", "zzz-service")
    try:
        db.set_log_watch_checkpoint("aaa-service")
        db.set_log_watch_checkpoint("zzz-service")
        db.set_log_watch_checkpoint("mmm-ungrouped")  # no compose file match -- stays ungrouped

        resp = client.get("/logs?csort=stack&cdir=asc")
        assert resp.status_code == 200
        tbody = resp.text[resp.text.index('id="logs-containers-table"'):]
        aaa_pos = tbody.index("aaa-service")
        ungrouped_pos = tbody.index("mmm-ungrouped")
        # Grouped members (any stack_name) sort before ungrouped ones regardless of direction.
        assert aaa_pos < ungrouped_pos
    finally:
        compose_file.unlink()


def test_renaming_a_stack_from_the_logs_page_redirects_back_to_it(client):
    compose_file = _compose_file("logs-stack-4.yml", "overseerr", "tautulli")
    try:
        db.set_log_watch_checkpoint("overseerr")
        db.set_log_watch_checkpoint("tautulli")
        stack_id = _stack_id_for("overseerr")

        resp = client.post(
            "/updates/stack/rename",
            data={"stack_id": stack_id, "name": "Media Stack", "return_to": f"/logs/stack?id={stack_id}"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/logs/stack?id={stack_id}"

        # The rename is visible from the Logs stack page too -- shared stack identity.
        follow = client.get(resp.headers["location"])
        assert "Media Stack" in follow.text
    finally:
        compose_file.unlink()


def test_renaming_a_stack_without_return_to_still_defaults_to_the_updates_page(client):
    """Backward compatible: the Updates stack page's own rename form doesn't send return_to at
    all, and must keep working exactly as it did before this feature existed."""
    compose_file = _compose_file("logs-stack-5.yml", "gluetun", "qbittorrent")
    try:
        db.upsert_container_state("gluetun", "owner/gluetun", "latest", "sha256:old")
        db.upsert_container_state("qbittorrent", "owner/qbittorrent", "latest", "sha256:old")
        stack_id = _stack_id_for("gluetun")

        resp = client.post(
            "/updates/stack/rename",
            data={"stack_id": stack_id, "name": "Torrent Stack"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/updates/stack?id={stack_id}"
    finally:
        compose_file.unlink()
        db.reset_updates_data()
