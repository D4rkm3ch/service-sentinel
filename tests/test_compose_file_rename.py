"""Compose's counterpart to the existing Stack rename feature (see test_logs_stacks.py) --
an operator can override a compose file's display name (normally just its services: keys
joined together, see compose_lookup.subject_display_name) the same way they already can for
a stack. Simpler than the stack rename pair: no AI-generated name to protect a services_hash
for, and only one detail page to redirect back to."""

from pathlib import Path

from app import compose_lookup, db
from app.config import settings

db.init_db()


def _compose_file(name, *services):
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return str(path)


def test_display_name_defaults_to_the_computed_service_list():
    path = _compose_file("rename-1.yml", "sonarr", "radarr")
    try:
        assert compose_lookup.subject_display_name("compose", path) == "sonarr, radarr"
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)


def test_manual_override_takes_priority_over_the_computed_name():
    path = _compose_file("rename-2.yml", "sonarr", "radarr")
    try:
        db.set_compose_file_name(path, "Media Stack", "manual")
        assert compose_lookup.subject_display_name("compose", path) == "Media Stack"
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)


def test_reset_falls_back_to_the_computed_name_again():
    path = _compose_file("rename-3.yml", "sonarr", "radarr")
    try:
        db.set_compose_file_name(path, "Media Stack", "manual")
        db.reset_compose_file_name(path)
        assert compose_lookup.subject_display_name("compose", path) == "sonarr, radarr"
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)


def test_rename_route_sets_a_manual_override_and_redirects_to_the_file_page(client):
    path = _compose_file("rename-4.yml", "plex")
    try:
        resp = client.post(
            "/compose/file/rename",
            data={"path": path, "name": "Media Server"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/compose/file?path={path}"
        assert compose_lookup.subject_display_name("compose", path) == "Media Server"
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)


def test_rename_route_ignores_a_blank_name(client):
    path = _compose_file("rename-5.yml", "plex")
    try:
        resp = client.post("/compose/file/rename", data={"path": path, "name": "   "}, follow_redirects=False)
        assert resp.status_code == 303
        assert db.get_compose_file_name(path) is None
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)


def test_reset_name_route_clears_the_override_and_redirects_to_the_file_page(client):
    path = _compose_file("rename-6.yml", "plex")
    try:
        db.set_compose_file_name(path, "Media Server", "manual")
        resp = client.post("/compose/file/reset-name", data={"path": path}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/compose/file?path={path}"
        assert db.get_compose_file_name(path) is None
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)


def test_compose_file_detail_page_shows_the_rename_controls(client):
    """The reset-to-computed-name button was removed from the UI (it's not necessary alongside
    plain rename) -- the backend route (see test_reset_name_route_clears_the_override_and_
    redirects_to_the_file_page below) is untouched, just nothing in the template links to it
    anymore."""
    path = _compose_file("rename-7.yml", "plex", "tautulli")
    try:
        db.upsert_finding("compose", path, "issue one", "reliability", "warning", "desc")
        db.upsert_finding("compose", path, "issue two", "reliability", "warning", "desc")

        resp = client.get(f"/compose/file?path={path}")
        assert resp.status_code == 200
        assert 'action="/compose/file/rename"' in resp.text
        assert 'action="/compose/file/reset-name"' not in resp.text
        assert "Rename this compose file" in resp.text
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE source = 'compose' AND subject = ?", (path,))


def test_single_finding_compose_page_shows_rename_controls(client):
    """A compose file with exactly one finding redirects straight to finding_detail.html (see
    compose_file_detail's own redirect) rather than subject_findings.html -- that page had no
    rename controls at all until now, compose-only same as the multi-finding page's."""
    path = _compose_file("rename-9.yml", "plex")
    try:
        finding_id, _ = db.upsert_finding("compose", path, "issue one", "reliability", "warning", "desc")

        resp = client.get(f"/findings/{finding_id}")
        assert resp.status_code == 200
        assert 'action="/compose/file/rename"' in resp.text
        assert "Rename this compose file" in resp.text
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE source = 'compose' AND subject = ?", (path,))


def test_single_finding_logs_page_does_not_show_rename_controls(client):
    finding_id, _ = db.upsert_finding("logs", "rename-log-subject-single", "issue one", "crash", "warning", "desc")
    try:
        resp = client.get(f"/findings/{finding_id}")
        assert resp.status_code == 200
        assert 'action="/compose/file/rename"' not in resp.text
        assert "Rename this compose file" not in resp.text
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE source = 'logs' AND subject = 'rename-log-subject-single'")


def test_logs_subject_page_does_not_show_rename_controls(client):
    """The rename feature is Compose-only -- a Logs container name isn't something an operator
    would want to override the same way (there's no equivalent "computed from services:" name
    to override in the first place)."""
    db.upsert_finding("logs", "rename-log-subject", "issue one", "crash", "warning", "desc")
    db.upsert_finding("logs", "rename-log-subject", "issue two", "crash", "warning", "desc")
    try:
        resp = client.get("/logs/container/rename-log-subject")
        assert resp.status_code == 200
        assert 'action="/compose/file/rename"' not in resp.text
        assert "Rename this compose file" not in resp.text
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE source = 'logs' AND subject = 'rename-log-subject'")


def test_compose_file_detail_page_shows_the_raw_path_for_traceability(client):
    """A real-world report: an auto-generated display name built from generic service names
    (e.g. "app, database, scraper") gives the operator zero way to tell which actual file it's
    even looking at. The raw path is now always shown, regardless of what the display name
    says, so it's always traceable back to a real file on disk."""
    path = _compose_file("rename-10.yml", "app", "database", "scraper")
    try:
        db.upsert_finding("compose", path, "issue one", "reliability", "warning", "desc")
        db.upsert_finding("compose", path, "issue two", "reliability", "warning", "desc")

        resp = client.get(f"/compose/file?path={path}")
        assert resp.status_code == 200
        assert "app, database, scraper" in resp.text
        assert path in resp.text
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE source = 'compose' AND subject = ?", (path,))


def test_single_finding_compose_page_shows_the_raw_path_for_traceability(client):
    path = _compose_file("rename-11.yml", "app")
    try:
        finding_id, _ = db.upsert_finding("compose", path, "issue one", "reliability", "warning", "desc")

        resp = client.get(f"/findings/{finding_id}")
        assert resp.status_code == 200
        assert path in resp.text
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE source = 'compose' AND subject = ?", (path,))


def test_logs_subject_page_does_not_show_a_raw_path_line(client):
    """Logs' display_name already IS the raw container name -- no separate identifier to add."""
    db.upsert_finding("logs", "rename-log-no-path-line", "issue one", "crash", "warning", "desc")
    db.upsert_finding("logs", "rename-log-no-path-line", "issue two", "crash", "warning", "desc")
    try:
        resp = client.get("/logs/container/rename-log-no-path-line")
        assert resp.status_code == 200
        assert "subject-path" not in resp.text
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE source = 'logs' AND subject = 'rename-log-no-path-line'")


def test_renamed_file_shows_the_new_name_on_the_main_compose_list(client):
    path = _compose_file("rename-8.yml", "plex")
    try:
        db.set_compose_file_hash(path, "hash1")
        db.set_compose_file_name(path, "Media Server", "manual")

        resp = client.get("/compose")
        assert resp.status_code == 200
        assert "Media Server" in resp.text
    finally:
        Path(path).unlink()
        db.reset_compose_file_name(path)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (path,))
