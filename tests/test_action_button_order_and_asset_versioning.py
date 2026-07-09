"""Two follow-up fixes from the same feedback round:
1. The Updates page header and the stack detail page now put Check Now first, then Regenerate
   AI Response, then Reset & re-check (left to right) -- matching the per-item detail page's
   order, which was already correct. The stack page was also missing a Check Now button
   entirely, and its Regenerate AI Response button wasn't colored like every other one.
2. New CSS classes (button-silence/button-info) appeared to not be applying at all on a real
   deployment even though the HTML carried the right classes -- root cause was style.css being
   served with no cache-busting, so browsers kept using a stale cached copy after a deploy.
   Now every <link> to it carries a content-hash query string that changes whenever the file's
   contents do."""

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app import check_state, compose_lookup, db
from app.config import settings
from app.docker_client import TrackedContainer
from app.main import _static_asset_version

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
    yield
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")


def _compose_file(name, *services):
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _stack_id_for(container_name):
    return compose_lookup.match_container_to_stack(container_name, compose_lookup.build_stack_index())["stack_id"]


def _wait_until_not_running():
    for _ in range(30):
        if not check_state.get_state("updates")["running"]:
            return
        time.sleep(0.1)


def test_updates_header_buttons_are_ordered_check_now_then_regenerate_then_reset(client):
    resp = client.get("/updates")
    check_now_pos = resp.text.index('hx-post="/updates/check-now"')
    regen_pos = resp.text.index('action="/updates/regenerate-all"')
    reset_pos = resp.text.index('action="/updates/reset-and-recheck"')
    assert check_now_pos < regen_pos < reset_pos


def test_stack_page_has_a_check_now_button_ordered_before_regenerate_and_reset(client):
    compose_file = _compose_file("button-order-stack.yml", "button-order-svc")
    try:
        stack_id = _stack_id_for("button-order-svc")

        resp = client.get(f"/updates/stack?id={stack_id}")
        assert resp.status_code == 200
        check_now_pos = resp.text.index("/updates/stack/check-now?stack_id=")
        regen_pos = resp.text.index("Regenerate AI Response")
        reset_pos = resp.text.index("/updates/stack/reset-and-recheck?stack_id=")
        assert check_now_pos < regen_pos < reset_pos
    finally:
        compose_file.unlink()


def test_stack_page_regenerate_button_has_the_warn_color_class(client):
    """Regression guard: the stack page's Regenerate AI Response button was plain green
    (missing button-warn) while every other Regenerate button in the app was amber."""
    compose_file = _compose_file("button-color-stack.yml", "button-color-svc")
    try:
        stack_id = _stack_id_for("button-color-svc")

        resp = client.get(f"/updates/stack?id={stack_id}")
        regen_pos = resp.text.index("Regenerate AI Response")
        button_start = resp.text.rindex("<button", 0, regen_pos)
        assert "button-warn" in resp.text[button_start:regen_pos]
    finally:
        compose_file.unlink()


def test_stack_check_now_route_exists_and_only_touches_its_own_members(client):
    compose_file = _compose_file("check-now-stack.yml", "cn-sonarr", "cn-radarr")
    try:
        for name in ("cn-sonarr", "cn-radarr", "cn-unrelated"):
            db.upsert_container_state(name, f"owner/{name}", "latest", "sha256:old")
        stack_id = _stack_id_for("cn-sonarr")

        def _fake_containers():
            return [
                TrackedContainer(name=n, image_repo=f"owner/{n}", tag="latest", current_digest="sha256:old", labels={})
                for n in ("cn-sonarr", "cn-radarr", "cn-unrelated")
            ]

        with patch("app.reconcile.list_tracked_containers", return_value=_fake_containers()), \
             patch("app.reconcile.get_latest_digest", return_value="sha256:new"), \
             patch("app.persist.release_notes.get_release_notes", return_value=("Notes", "https://example.com")):
            resp = client.post("/updates/stack/check-now", params={"stack_id": stack_id})
            assert resp.status_code == 200
            _wait_until_not_running()

        rows = {r["container_name"]: r for r in db.list_tracked_containers_with_status()}
        assert rows["cn-sonarr"]["status"] == "update_available"
        assert rows["cn-radarr"]["status"] == "update_available"
        assert rows["cn-unrelated"]["status"] == "up_to_date"  # not a stack member, untouched
    finally:
        compose_file.unlink()


def test_static_asset_version_is_a_short_content_hash(client):
    version = _static_asset_version()
    assert isinstance(version, str)
    assert 6 <= len(version) <= 16

    resp = client.get("/updates")
    assert f'/static/style.css?v={version}' in resp.text
