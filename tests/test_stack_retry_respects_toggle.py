"""A real-world bug report: the stack detail page's manual Retry button called
stacks.regenerate_stack_analysis(..., force=True) directly, which never checked the
Cross-Service Analysis toggle at all -- only the UI (a disabled button) prevented a normal
click from reaching it while the setting was off. A raw POST (or a race where the setting got
turned off between page load and the click landing) could still trigger a real AI call.
regenerate_stack_analysis now checks the toggle itself, so every caller is protected, not just
ones that go through the toggle-aware automatic pass (run_stack_analysis_pass)."""

from unittest.mock import patch

from app import db, stacks


def _c(name, repo="owner/repo", tag="latest", current_digest="sha256:old", latest_digest="sha256:new"):
    return {
        "container_name": name, "image_repo": repo, "tag": tag,
        "current_digest": current_digest, "latest_digest": latest_digest,
    }


def test_regenerate_stack_analysis_refuses_to_call_the_ai_when_the_toggle_is_off():
    db.set_cross_service_analysis_enabled("updates", False)
    members = [_c("sonarr"), _c("radarr")]
    with patch("app.stacks.analyze_stack_impact") as mock_analyze:
        stacks.regenerate_stack_analysis("bypass-test-stack", members, force=True)
    mock_analyze.assert_not_called()
    assert db.get_stack_analysis("bypass-test-stack", source="updates") is None


def test_regenerate_stack_analysis_refuses_even_with_force_true():
    """force=True only ever means "skip the content-hash cache" -- it must never mean "ignore
    the opt-in setting entirely," which is exactly what the old Retry-bypasses-the-toggle bug
    did (force=True was always passed by the Retry route)."""
    db.set_cross_service_analysis_enabled("updates", False)
    members = [_c("sonarr"), _c("radarr")]
    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", return_value="Should never be called.") as mock_analyze:
        stacks.regenerate_stack_analysis("bypass-test-stack-2", members, force=True)
    mock_analyze.assert_not_called()


def test_retry_route_does_not_call_the_ai_while_the_toggle_is_off(client):
    from pathlib import Path
    from app.config import settings
    from app import compose_lookup

    body = "services:\n  bypass-svc-a:\n    image: owner/bypass-svc-a\n  bypass-svc-b:\n    image: owner/bypass-svc-b\n"
    compose_file = Path(settings.compose_root) / "bypass-toggle-stack.yml"
    compose_file.write_text(body)
    try:
        db.set_cross_service_analysis_enabled("updates", False)
        db.upsert_container_state("bypass-svc-a", "owner/bypass-svc-a", "latest", "sha256:a")
        db.upsert_container_state("bypass-svc-b", "owner/bypass-svc-b", "latest", "sha256:b")
        stack_id = compose_lookup.match_container_to_stack(
            "bypass-svc-a", compose_lookup.build_stack_index()
        )["stack_id"]

        with patch("app.stacks.analyze_stack_impact") as mock_analyze:
            resp = client.post("/updates/stack/retry", params={"stack_id": stack_id})
            assert resp.status_code == 200
            import time
            from app import check_state
            for _ in range(30):
                if not check_state.get_state("updates")["running"]:
                    break
                time.sleep(0.1)
        mock_analyze.assert_not_called()
        assert db.get_stack_analysis(stack_id, source="updates") is None
    finally:
        compose_file.unlink()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM container_state WHERE container_name IN ('bypass-svc-a', 'bypass-svc-b')")
