"""The stack detail page's cross-service analysis blurb and its "Regenerate AI Response"
button must both fully respect the Updates: Cross-Service Analysis toggle -- a real-world
report showed the blurb (and a working-looking button) still rendering even with the toggle
off, which misrepresents a feature the operator explicitly opted out of as still active. With
the toggle off: no blurb at all (regardless of whether one is cached from before the toggle was
turned off), and the button is disabled with an explanatory tooltip rather than clickable."""

from pathlib import Path

import pytest

from app import db
from app.config import settings

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
    db.set_cross_service_analysis_enabled("updates", False)
    yield
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
    db.set_cross_service_analysis_enabled("updates", False)


def _compose_file(name, *services):
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _stack_id_for(container_name):
    from app import compose_lookup
    return compose_lookup.match_container_to_stack(container_name, compose_lookup.build_stack_index())["stack_id"]


def test_a_cached_blurb_is_hidden_entirely_when_the_toggle_is_off(client):
    compose_file = _compose_file("gating.yml", "sonarr", "radarr")
    try:
        db.upsert_container_state("sonarr", "owner/sonarr", "latest", "sha256:old")
        db.upsert_container_state("radarr", "owner/radarr", "latest", "sha256:old")
        stack_id = _stack_id_for("sonarr")
        db.set_stack_analysis(stack_id, "abc123", "sonarr needs radarr updated first.")
        db.set_cross_service_analysis_enabled("updates", False)

        resp = client.get(f"/updates/stack?id={stack_id}")
        assert "sonarr needs radarr updated first." not in resp.text
        assert "overview-summary" not in resp.text
    finally:
        compose_file.unlink()


def test_the_blurb_shows_again_once_the_toggle_is_back_on(client):
    compose_file = _compose_file("gating2.yml", "sonarr", "radarr")
    try:
        db.upsert_container_state("sonarr", "owner/sonarr", "latest", "sha256:old")
        db.upsert_container_state("radarr", "owner/radarr", "latest", "sha256:old")
        stack_id = _stack_id_for("sonarr")
        db.set_stack_analysis(stack_id, "abc123", "sonarr needs radarr updated first.")
        db.set_cross_service_analysis_enabled("updates", True)

        resp = client.get(f"/updates/stack?id={stack_id}")
        assert "updated first" in resp.text
        assert "overview-summary" in resp.text
    finally:
        compose_file.unlink()


def test_regenerate_button_is_disabled_with_a_tooltip_when_the_toggle_is_off(client):
    compose_file = _compose_file("gating3.yml", "sonarr", "radarr")
    try:
        db.upsert_container_state("sonarr", "owner/sonarr", "latest", "sha256:old")
        db.upsert_container_state("radarr", "owner/radarr", "latest", "sha256:old")
        stack_id = _stack_id_for("sonarr")
        db.set_cross_service_analysis_enabled("updates", False)

        resp = client.get(f"/updates/stack?id={stack_id}")
        assert 'hx-post="/updates/stack/retry' not in resp.text
        assert "Enable Cross-Service Analysis in Settings" in resp.text
    finally:
        compose_file.unlink()


def test_regenerate_button_is_clickable_when_the_toggle_is_on(client):
    compose_file = _compose_file("gating4.yml", "sonarr", "radarr")
    try:
        db.upsert_container_state("sonarr", "owner/sonarr", "latest", "sha256:old")
        db.upsert_container_state("radarr", "owner/radarr", "latest", "sha256:old")
        stack_id = _stack_id_for("sonarr")
        db.set_cross_service_analysis_enabled("updates", True)

        resp = client.get(f"/updates/stack?id={stack_id}")
        assert 'hx-post="/updates/stack/retry' in resp.text
    finally:
        compose_file.unlink()
