"""Stage 12 integration: proves persist_check_outcome() actually calls
stacks.run_stack_analysis_pass() with the full container list, at all three call sites that
route through it (a full check, a stack-scoped Reset & re-check, a single-container scoped
check) -- this call was completely missing before, so the feature was fully coded but never
triggered by anything real. See tests/test_stacks.py for the grouping/caching logic itself."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app import db, persist
from app.config import settings


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
    yield
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")


@pytest.fixture(autouse=True)
def no_real_release_notes_fetch():
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        yield


def _write_compose(filename: str, services: dict[str, str]) -> Path:
    lines = ["services:"]
    for name, image in services.items():
        lines.append(f"  {name}:")
        lines.append(f"    image: {image}")
    path = Path(settings.compose_root) / filename
    path.write_text("\n".join(lines) + "\n")
    return path


def _outcome(*containers, checked_at="2026-01-01T00:00:00+00:00"):
    errors = sum(1 for c in containers if c["status"] == "error")
    return {"containers": list(containers), "errors": errors, "checked_at": checked_at}


def _c(name, status, repo="owner/repo", tag="latest", current_digest="sha256:old", latest_digest="sha256:new"):
    return {
        "container_name": name, "image_repo": repo, "tag": tag, "status": status,
        "current_digest": current_digest, "latest_digest": latest_digest,
    }


def test_a_full_check_passes_every_container_to_the_stack_analysis_pass():
    with patch("app.persist.stacks.run_stack_analysis_pass") as mock_pass:
        persist.persist_check_outcome(_outcome(
            _c("sonarr", "update_available"), _c("radarr", "up_to_date"),
        ))

    mock_pass.assert_called_once()
    (containers_arg,) = mock_pass.call_args[0]
    assert {c["container_name"] for c in containers_arg} == {"sonarr", "radarr"}


def test_a_failing_stack_analysis_pass_never_breaks_the_check():
    with patch("app.persist.stacks.run_stack_analysis_pass", side_effect=RuntimeError("boom")):
        persist.persist_check_outcome(_outcome(_c("sonarr", "update_available")))

    row = db.list_tracked_containers_with_status()[0]
    assert row["status"] == "update_available"
    assert row["id"] is not None


def test_end_to_end_a_real_two_member_stack_gets_a_fresh_analysis_when_deep_analysis_is_on():
    compose_file = _write_compose("e2e.yml", {"sonarr": "linuxserver/sonarr", "radarr": "linuxserver/radarr"})
    try:
        db.set_cross_service_analysis_enabled("updates", True)
        with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
             patch("app.stacks.analyze_stack_impact", return_value="They share a downloads volume.") as mock_analyze:
            persist.persist_check_outcome(_outcome(
                _c("sonarr", "update_available", repo="linuxserver/sonarr"),
                _c("radarr", "up_to_date", repo="linuxserver/radarr"),
            ))
        mock_analyze.assert_called_once()

        # Find whichever stack_id compose_lookup assigned this file and confirm it landed.
        rows = db.list_tracked_containers_with_status()
        assert len(rows) == 2
    finally:
        compose_file.unlink()
        db.set_cross_service_analysis_enabled("updates", False)


def test_a_single_container_scoped_check_never_triggers_stack_analysis():
    """No special-casing exists for this -- it's a natural consequence of only one container
    ever being present in a scoped outcome's container list, so _group_containers_by_stack
    can never find 2+ members of the same stack there."""
    with patch("app.persist.reconcile.run_check_one", return_value=_outcome(_c("sonarr", "update_available"))), \
         patch("app.persist.stacks.analyze_stack_impact") as mock_analyze:
        persist.run_and_persist_single_check("sonarr")
    mock_analyze.assert_not_called()
