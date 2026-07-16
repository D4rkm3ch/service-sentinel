"""A real-world audit found Updates stacks had no bulk Silence/Unsilence, unlike Logs stacks --
Updates already had per-container silence (an EOL container that will always show a new tag),
just never got the stack-wide "mute every member at once" version Logs has. Mirrors
test_logs_full_parity_actions.py's own stack-silence coverage, adapted for Updates' persistent
container_state.silenced flag instead of findings' active/silenced status."""

from pathlib import Path

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


def test_updates_stack_silence_and_unsilence_cascade_to_every_member(client):
    compose_file = _compose_file("updates-stack-silence.yml", "up-silence-a", "up-silence-b")
    try:
        db.upsert_container_state("up-silence-a", "owner/up-silence-a", "latest", "sha256:a")
        db.upsert_container_state("up-silence-b", "owner/up-silence-b", "latest", "sha256:b")
        stack_id = _stack_id_for("up-silence-a")

        resp = client.post("/updates/stack/silence", params={"stack_id": stack_id})
        assert resp.status_code == 200
        assert "badge-silenced\">Silenced</span>" in resp.text
        assert db.get_container_state("up-silence-a")["silenced"] == 1
        assert db.get_container_state("up-silence-b")["silenced"] == 1

        resp = client.post("/updates/stack/unsilence", params={"stack_id": stack_id})
        assert resp.status_code == 200
        assert db.get_container_state("up-silence-a")["silenced"] == 0
        assert db.get_container_state("up-silence-b")["silenced"] == 0
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM container_state WHERE container_name IN ('up-silence-a', 'up-silence-b')")


def test_updates_stack_shows_partially_silenced_when_only_some_members_are_silenced(client):
    compose_file = _compose_file("updates-stack-partial.yml", "up-partial-a", "up-partial-b")
    try:
        db.upsert_container_state("up-partial-a", "owner/up-partial-a", "latest", "sha256:a")
        db.upsert_container_state("up-partial-b", "owner/up-partial-b", "latest", "sha256:b")
        db.set_container_silenced("up-partial-a", True)
        stack_id = _stack_id_for("up-partial-a")

        resp = client.get(f"/updates/stack?id={stack_id}")
        assert "badge-partially-silenced\">Partially Silenced</span>" in resp.text
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM container_state WHERE container_name IN ('up-partial-a', 'up-partial-b')")


def test_updates_stack_silence_with_missing_stack_id_is_rejected(client):
    resp = client.post("/updates/stack/silence")
    assert resp.status_code == 400


def test_updates_stack_unsilence_with_missing_stack_id_is_rejected(client):
    resp = client.post("/updates/stack/unsilence")
    assert resp.status_code == 400


def test_updates_stack_page_includes_the_silence_toggle_button(client):
    compose_file = _compose_file("updates-stack-toggle-ui.yml", "up-toggle-a")
    try:
        db.upsert_container_state("up-toggle-a", "owner/up-toggle-a", "latest", "sha256:a")
        stack_id = _stack_id_for("up-toggle-a")

        resp = client.get(f"/updates/stack?id={stack_id}")
        assert f"/updates/stack/silence?stack_id={stack_id}" in resp.text
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM container_state WHERE container_name = 'up-toggle-a'")


def test_set_containers_silenced_is_batched_not_one_connection_per_container():
    """Regression guard mirroring the app's other connection-count tests -- the bulk stack
    silence/unsilence routes must use db.set_containers_silenced (one connection for the whole
    list), not loop calling the single-container db.set_container_silenced."""
    import sqlite3
    from unittest.mock import patch

    names = [f"batch-silence-{i}" for i in range(10)]
    for name in names:
        db.upsert_container_state(name, f"owner/{name}", "latest", "sha256:a")

    original_connect = sqlite3.connect
    connect_calls = []

    def counting_connect(*args, **kwargs):
        connect_calls.append(1)
        return original_connect(*args, **kwargs)

    try:
        with patch("app.db.sqlite3.connect", side_effect=counting_connect):
            db.set_containers_silenced(names, True)
        assert connect_calls == [1], f"expected one connection for the whole batch, got {len(connect_calls)}"
        for name in names:
            assert db.get_container_state(name)["silenced"] == 1
    finally:
        with db.get_conn() as conn:
            qs = ",".join("?" * len(names))
            conn.execute(f"DELETE FROM container_state WHERE container_name IN ({qs})", names)
