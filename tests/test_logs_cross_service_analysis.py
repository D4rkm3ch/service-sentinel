"""An explicit ask: take Cross-Service Analysis (already built for Updates -- a stack-wide AI
blurb reasoning about whether one service's issue could affect others in the same compose
stack) and apply the same thing to Logs. Off by default, its own toggle, not offered for
Compose (a compose file's services are already grouped together in the same file). Updates'
and Logs' analyses for the same physical stack_id must stay independent -- stack_id is just
the compose file's own path, shared identity across both features."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app import compose_lookup, db, stacks
from app.config import settings

db.init_db()


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
        conn.execute("DELETE FROM log_watch_state")
        conn.execute("DELETE FROM findings")
    db.set_cross_service_analysis_enabled("logs", False)
    yield
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
        conn.execute("DELETE FROM log_watch_state")
        conn.execute("DELETE FROM findings")
    db.set_cross_service_analysis_enabled("logs", False)


def _compose_file(name, *services):
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _stack_id_for(container_name):
    return compose_lookup.match_container_to_stack(container_name, compose_lookup.build_stack_index())["stack_id"]


def test_cross_service_analysis_for_logs_defaults_to_off():
    assert db.get_cross_service_analysis_enabled("logs") is False


def test_not_offered_for_compose():
    """Compose's own settings key was never created -- there's no code path that would even
    read a cross_service_analysis_compose_enabled key."""
    assert db.get_cross_service_analysis_enabled("compose") is False  # falls back to the default, never seeded


def test_regenerate_log_stack_analysis_refuses_when_the_toggle_is_off():
    db.set_cross_service_analysis_enabled("logs", False)
    with patch("app.stacks.analyze_log_stack_impact") as mock_analyze:
        stacks.regenerate_log_stack_analysis("logtest-stack", ["a", "b"], force=True)
    mock_analyze.assert_not_called()


def test_regenerate_log_stack_analysis_calls_the_ai_and_persists_when_enabled():
    db.set_cross_service_analysis_enabled("logs", True)
    fid, _ = db.upsert_finding("logs", "log-cs-a", "OOM crash", "crash", "critical", "desc")
    with patch("app.stacks.get_or_generate_stack_name", return_value="Log Stack"), \
         patch("app.stacks.analyze_log_stack_impact", return_value="a and b share a volume.") as mock_analyze:
        stacks.regenerate_log_stack_analysis("logtest-stack-2", ["log-cs-a", "log-cs-b"])
    mock_analyze.assert_called_once()
    saved = db.get_stack_analysis("logtest-stack-2", source="logs")
    assert saved["analysis_markdown"] == "a and b share a volume."


def test_updates_and_logs_analyses_for_the_same_stack_id_do_not_collide():
    db.set_stack_analysis("shared-stack-id", "hash1", "Updates says X.", source="updates")
    db.set_stack_analysis("shared-stack-id", "hash2", "Logs says Y.", source="logs")

    updates_row = db.get_stack_analysis("shared-stack-id", source="updates")
    logs_row = db.get_stack_analysis("shared-stack-id", source="logs")
    assert updates_row["analysis_markdown"] == "Updates says X."
    assert logs_row["analysis_markdown"] == "Logs says Y."
    # stack_id is handed back stripped of its internal ":source" suffix.
    assert updates_row["stack_id"] == "shared-stack-id"
    assert logs_row["stack_id"] == "shared-stack-id"


def test_run_log_stack_analysis_pass_is_a_noop_when_disabled(client):
    compose_file = _compose_file("log-cs-disabled.yml", "log-cs-svc-a", "log-cs-svc-b")
    try:
        db.set_log_watch_checkpoint("log-cs-svc-a")
        db.set_log_watch_checkpoint("log-cs-svc-b")
        with patch("app.stacks.analyze_log_stack_impact") as mock_analyze:
            stacks.run_log_stack_analysis_pass(["log-cs-svc-a", "log-cs-svc-b"])
        mock_analyze.assert_not_called()
    finally:
        compose_file.unlink()


def test_run_log_stack_analysis_pass_regenerates_every_qualifying_stack_when_enabled():
    compose_file = _compose_file("log-cs-enabled.yml", "log-cs-svc-c", "log-cs-svc-d")
    try:
        db.set_cross_service_analysis_enabled("logs", True)
        db.set_log_watch_checkpoint("log-cs-svc-c")
        db.set_log_watch_checkpoint("log-cs-svc-d")
        with patch("app.stacks.get_or_generate_stack_name", return_value="Log Stack"), \
             patch("app.stacks.analyze_log_stack_impact", return_value="Findings.") as mock_analyze:
            stacks.run_log_stack_analysis_pass(["log-cs-svc-c", "log-cs-svc-d"])
        mock_analyze.assert_called_once()
    finally:
        compose_file.unlink()


def test_logs_stack_page_shows_the_regenerate_button_only_when_toggle_is_on(client):
    compose_file = _compose_file("log-cs-page.yml", "log-cs-page-a", "log-cs-page-b")
    try:
        db.set_log_watch_checkpoint("log-cs-page-a")
        db.set_log_watch_checkpoint("log-cs-page-b")
        stack_id = _stack_id_for("log-cs-page-a")

        db.set_cross_service_analysis_enabled("logs", False)
        resp = client.get(f"/logs/stack?id={stack_id}")
        assert f'/logs/stack/retry?stack_id={stack_id}' not in resp.text
        assert "disabled" in resp.text

        db.set_cross_service_analysis_enabled("logs", True)
        resp = client.get(f"/logs/stack?id={stack_id}")
        assert 'hx-post="/logs/stack/retry?stack_id=' in resp.text
    finally:
        compose_file.unlink()


def test_logs_stack_retry_route_regenerates_and_shows_a_spinner(client):
    compose_file = _compose_file("log-cs-retry.yml", "log-cs-retry-a", "log-cs-retry-b")
    try:
        db.set_cross_service_analysis_enabled("logs", True)
        db.set_log_watch_checkpoint("log-cs-retry-a")
        db.set_log_watch_checkpoint("log-cs-retry-b")
        stack_id = _stack_id_for("log-cs-retry-a")

        with patch("app.stacks.get_or_generate_stack_name", return_value="Log Stack"), \
             patch("app.stacks.analyze_log_stack_impact", return_value="Analysis text."):
            resp = client.post("/logs/stack/retry", params={"stack_id": stack_id})
            assert resp.status_code == 200
            assert 'class="spinner"' in resp.text

            import time
            from app import check_state
            for _ in range(30):
                if not check_state.get_state("logs")["running"]:
                    break
                time.sleep(0.1)

        assert db.get_stack_analysis(stack_id, source="logs")["analysis_markdown"] == "Analysis text."
    finally:
        compose_file.unlink()


def test_logs_stack_retry_with_no_stack_id_is_rejected(client):
    resp = client.post("/logs/stack/retry")
    assert resp.status_code == 400
