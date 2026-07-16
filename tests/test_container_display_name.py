"""An explicit ask: stacks and compose files were already renameable (db.stacks/db.compose_files,
each with their own rename UI), but a bare container name -- the one identity shown everywhere on
both the Updates and Logs sides -- had no override at all. "No point to only have it in special
places." Adds db.container_names, a shared display-name override keyed by container_name
(independent of both container_state and log_watch_state, since a container can appear in either,
both, or neither), a rename route on each feature's own "container's own page" (Updates'
/updates/{id}, Logs' /logs/container/{name} and /findings/{id}), and propagates the resolved name
into every table listing that currently shows a raw container_name (Updates' own table, the
Tracked Containers table, both stack detail pages, and Logs' Issues/All Containers tables)."""

from pathlib import Path
from unittest.mock import patch

from app import compose_lookup, db
from app.config import settings

db.init_db()


def _compose_file(name, *services):
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _stack_id_for(container_name):
    return compose_lookup.match_container_to_stack(container_name, compose_lookup.build_stack_index())["stack_id"]


def _seed_update(name, release_notes_raw="Some release notes."):
    db.upsert_container_state(name, f"owner/{name}", "latest", "sha256:new")
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name=name, image_repo=f"owner/{name}", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown=None, source_url=None, error=None, severity="bugfix",
            release_notes_raw=release_notes_raw,
        )


def _cleanup_update(name):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM container_state WHERE container_name = ?", (name,))
        conn.execute("DELETE FROM updates WHERE container_name = ?", (name,))
    db.reset_container_display_name(name)


# ---------------------------------------------------------------------------
# db.container_names itself
# ---------------------------------------------------------------------------

def test_get_container_display_name_is_none_until_a_name_is_set():
    assert db.get_container_display_name("cdn-unset-container") is None
    db.set_container_display_name("cdn-unset-container", "My Container")
    try:
        assert db.get_container_display_name("cdn-unset-container") == "My Container"
    finally:
        db.reset_container_display_name("cdn-unset-container")


def test_set_container_display_name_upserts_not_duplicates():
    db.set_container_display_name("cdn-upsert-container", "First Name")
    db.set_container_display_name("cdn-upsert-container", "Second Name")
    try:
        assert db.get_container_display_name("cdn-upsert-container") == "Second Name"
    finally:
        db.reset_container_display_name("cdn-upsert-container")


def test_get_container_display_names_is_batched_not_one_connection_per_container():
    import sqlite3
    names = [f"cdn-batch-{i}" for i in range(10)]
    for i, name in enumerate(names):
        db.set_container_display_name(name, f"Renamed {i}")
    try:
        original_connect = sqlite3.connect
        connect_calls = []

        def counting_connect(*args, **kwargs):
            connect_calls.append(1)
            return original_connect(*args, **kwargs)

        with patch("app.db.sqlite3.connect", side_effect=counting_connect):
            result = db.get_container_display_names(names)

        assert connect_calls == [1], f"expected one connection for the whole batch, got {len(connect_calls)}"
        assert result == {name: f"Renamed {i}" for i, name in enumerate(names)}
    finally:
        for name in names:
            db.reset_container_display_name(name)


def test_get_container_display_names_omits_containers_with_no_override():
    db.set_container_display_name("cdn-partial-a", "Renamed A")
    try:
        result = db.get_container_display_names(["cdn-partial-a", "cdn-partial-b"])
        assert result == {"cdn-partial-a": "Renamed A"}
    finally:
        db.reset_container_display_name("cdn-partial-a")


# ---------------------------------------------------------------------------
# compose_lookup.subject_display_name -- the logs branch
# ---------------------------------------------------------------------------

def test_subject_display_name_for_logs_falls_back_to_the_raw_container_name():
    assert compose_lookup.subject_display_name("logs", "cdn-no-override") == "cdn-no-override"


def test_subject_display_name_for_logs_uses_the_override_when_set():
    db.set_container_display_name("cdn-override-name", "Friendly Name")
    try:
        assert compose_lookup.subject_display_name("logs", "cdn-override-name") == "Friendly Name"
    finally:
        db.reset_container_display_name("cdn-override-name")


# ---------------------------------------------------------------------------
# Updates' own rename route (its "container's own page" is an update's detail page)
# ---------------------------------------------------------------------------

def test_updates_detail_page_shows_rename_controls_and_the_display_name(client):
    _seed_update("cdn-updates-detail")
    try:
        update = db.get_latest_update_for_container("cdn-updates-detail")
        resp = client.get(f"/updates/{update['id']}")
        assert resp.status_code == 200
        assert 'action="/updates/container/cdn-updates-detail/rename"' in resp.text
        assert "Rename this container" in resp.text
    finally:
        _cleanup_update("cdn-updates-detail")


def test_renaming_a_container_from_its_updates_detail_page_takes_effect(client):
    _seed_update("cdn-updates-effect")
    try:
        update = db.get_latest_update_for_container("cdn-updates-effect")
        resp = client.post(
            "/updates/container/cdn-updates-effect/rename",
            data={"name": "Renamed From Updates"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/updates/{update['id']}"

        resp = client.get(f"/updates/{update['id']}")
        assert "Renamed From Updates" in resp.text
    finally:
        _cleanup_update("cdn-updates-effect")


def test_renaming_a_container_from_updates_ignores_a_blank_name(client):
    _seed_update("cdn-updates-blank")
    try:
        client.post("/updates/container/cdn-updates-blank/rename", data={"name": "  "}, follow_redirects=False)
        assert db.get_container_display_name("cdn-updates-blank") is None
    finally:
        _cleanup_update("cdn-updates-blank")


# ---------------------------------------------------------------------------
# Propagation into table listings
# ---------------------------------------------------------------------------

def test_renamed_container_shows_the_new_name_on_the_updates_found_table(client):
    _seed_update("cdn-updates-table")
    db.set_container_display_name("cdn-updates-table", "Table Renamed App")
    try:
        resp = client.get("/updates")
        assert "Table Renamed App" in resp.text
        assert ">cdn-updates-table<" not in resp.text
    finally:
        _cleanup_update("cdn-updates-table")


def test_renamed_container_shows_the_new_name_on_the_tracked_containers_table(client):
    db.upsert_container_state("cdn-tracked-table", "owner/cdn-tracked-table", "latest", "sha256:a")
    db.set_container_display_name("cdn-tracked-table", "Tracked Renamed App")
    try:
        resp = client.get("/updates")
        assert "Tracked Renamed App" in resp.text
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM container_state WHERE container_name = 'cdn-tracked-table'")
        db.reset_container_display_name("cdn-tracked-table")


def test_renamed_container_shows_the_new_name_on_the_updates_stack_page(client):
    compose_file = _compose_file("cdn-stack.yml", "cdn-stack-a", "cdn-stack-b")
    db.upsert_container_state("cdn-stack-a", "owner/cdn-stack-a", "latest", "sha256:a")
    db.upsert_container_state("cdn-stack-b", "owner/cdn-stack-b", "latest", "sha256:b")
    db.set_container_display_name("cdn-stack-a", "Stack Renamed App")
    try:
        stack_id = _stack_id_for("cdn-stack-a")
        resp = client.get(f"/updates/stack?id={stack_id}")
        assert "Stack Renamed App" in resp.text
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM container_state WHERE container_name IN ('cdn-stack-a', 'cdn-stack-b')")
        db.reset_container_display_name("cdn-stack-a")


def test_renamed_container_shows_the_new_name_on_the_logs_issues_table(client):
    db.upsert_finding("logs", "cdn-logs-issues", "some issue", "crash", "warning", "desc")
    db.set_container_display_name("cdn-logs-issues", "Logs Issues Renamed")
    try:
        resp = client.get("/logs")
        assert "Logs Issues Renamed" in resp.text
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM findings WHERE source = 'logs' AND subject = 'cdn-logs-issues'")
        db.reset_container_display_name("cdn-logs-issues")


def test_renamed_container_shows_the_new_name_on_the_logs_stack_page(client):
    compose_file = _compose_file("cdn-logs-stack.yml", "cdn-logs-stack-a", "cdn-logs-stack-b")
    # stack_member_names_for_logs (unlike Updates' own stack_member_names) is keyed off
    # log_watch_state, not findings -- "every container the log watcher has ever checked."
    db.set_log_watch_checkpoint("cdn-logs-stack-a")
    db.set_log_watch_checkpoint("cdn-logs-stack-b")
    db.upsert_finding("logs", "cdn-logs-stack-a", "issue a", "crash", "warning", "desc")
    db.upsert_finding("logs", "cdn-logs-stack-b", "issue b", "crash", "warning", "desc")
    db.set_container_display_name("cdn-logs-stack-a", "Logs Stack Renamed")
    try:
        stack_id = _stack_id_for("cdn-logs-stack-a")
        resp = client.get(f"/logs/stack?id={stack_id}")
        assert "Logs Stack Renamed" in resp.text
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM findings WHERE source = 'logs' AND subject IN ('cdn-logs-stack-a', 'cdn-logs-stack-b')"
            )
            conn.execute(
                "DELETE FROM log_watch_state WHERE container_name IN ('cdn-logs-stack-a', 'cdn-logs-stack-b')"
            )
        db.reset_container_display_name("cdn-logs-stack-a")


def test_updates_container_sort_key_sorts_by_the_displayed_name(client):
    """A renamed container's own alphabetical position should follow what's actually shown, not
    the hidden raw identity underneath it."""
    _seed_update("cdn-sort-zzz")
    _seed_update("cdn-sort-aaa")
    db.set_container_display_name("cdn-sort-zzz", "Aaa Renamed To Sort First")
    try:
        resp = client.get("/updates", params={"sort": "container", "dir": "asc"})
        assert resp.text.index("Aaa Renamed To Sort First") < resp.text.index("cdn-sort-aaa")
    finally:
        _cleanup_update("cdn-sort-zzz")
        _cleanup_update("cdn-sort-aaa")
