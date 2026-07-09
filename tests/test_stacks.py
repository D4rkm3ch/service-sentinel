"""Stage 12: cross-service stack analysis. Was fully coded (naming, caching, rendering) but
never actually wired into any real check -- stacks.run_stack_analysis_pass() was never called
from persist.py or anywhere else, so the feature was completely inert in production despite
looking finished. This file covers stacks.py's own logic in isolation (grouping, the
content-hash cache, force-regeneration); tests/test_stage6_persist_release_notes.py-adjacent
integration coverage for *whether persist.py actually calls this now* lives in
tests/test_persist_stack_analysis.py."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app import db, stacks
from app.config import settings


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
    db.set_deep_analysis_enabled("updates", False)
    yield
    db.reset_updates_data()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM stacks")
        conn.execute("DELETE FROM stack_analyses")
    db.set_deep_analysis_enabled("updates", False)


def _write_compose(filename: str, services: dict[str, str]) -> Path:
    lines = ["services:"]
    for name, image in services.items():
        lines.append(f"  {name}:")
        lines.append(f"    image: {image}")
    path = Path(settings.compose_root) / filename
    path.write_text("\n".join(lines) + "\n")
    return path


def _c(name, repo="owner/repo", tag="latest", current_digest="sha256:old", latest_digest="sha256:new"):
    return {
        "container_name": name, "image_repo": repo, "tag": tag,
        "current_digest": current_digest, "latest_digest": latest_digest,
    }


# ---------------------------------------------------------------------------
# _group_containers_by_stack
# ---------------------------------------------------------------------------

def test_two_members_of_a_real_stack_are_grouped_together():
    compose_file = _write_compose("arr.yml", {"sonarr": "linuxserver/sonarr", "radarr": "linuxserver/radarr"})
    try:
        containers = [_c("sonarr", repo="linuxserver/sonarr"), _c("radarr", repo="linuxserver/radarr")]
        groups = stacks._group_containers_by_stack(containers)
        assert len(groups) == 1
        (members,) = groups.values()
        assert {m["container_name"] for m in members} == {"sonarr", "radarr"}
    finally:
        compose_file.unlink()


def test_a_container_with_no_compose_match_is_never_grouped():
    containers = [_c("standalone")]
    assert stacks._group_containers_by_stack(containers) == {}


def test_a_stack_with_only_one_member_present_in_this_batch_is_excluded():
    """The compose file defines 2 services, but only one of them is in *this* container list
    (e.g. a scoped single-container check) -- nothing to cross-analyze from just one side."""
    compose_file = _write_compose("arr2.yml", {"sonarr": "linuxserver/sonarr", "radarr": "linuxserver/radarr"})
    try:
        containers = [_c("sonarr", repo="linuxserver/sonarr")]
        assert stacks._group_containers_by_stack(containers) == {}
    finally:
        compose_file.unlink()


def test_a_single_service_compose_file_is_never_grouped_even_alone_in_the_batch():
    compose_file = _write_compose("solo.yml", {"metube": "alexta69/metube"})
    try:
        containers = [_c("metube", repo="alexta69/metube")]
        assert stacks._group_containers_by_stack(containers) == {}
    finally:
        compose_file.unlink()


# ---------------------------------------------------------------------------
# regenerate_stack_analysis
# ---------------------------------------------------------------------------

def test_fewer_than_two_members_never_calls_the_ai():
    with patch("app.stacks.analyze_stack_impact") as mock_analyze:
        stacks.regenerate_stack_analysis("stack1", [_c("sonarr")])
    mock_analyze.assert_not_called()


def test_a_genuinely_new_stack_calls_the_ai_and_persists_the_result():
    members = [_c("sonarr", repo="linuxserver/sonarr"), _c("radarr", repo="linuxserver/radarr")]
    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", return_value="They share a downloads volume.") as mock_analyze:
        stacks.regenerate_stack_analysis("stack1", members)

    mock_analyze.assert_called_once()
    saved = db.get_stack_analysis("stack1")
    assert saved["analysis_markdown"] == "They share a downloads volume."


def test_the_ai_call_receives_real_release_notes_not_just_image_and_tag():
    """Regression test for a real-world report: passing only "sonarr (repo:tag)" with no actual
    notes text gave the model nothing to reason about, so it fell back to generic, useless
    answers ("yes, there is a network") true of every compose stack. The prompt must carry each
    pending member's actual summary/notes text."""
    db.record_update(
        container_name="sonarr", image_repo="linuxserver/sonarr", tag="latest",
        old_digest="sha256:old", new_digest="sha256:new",
        summary_markdown="Requires postgres >= 15.", source_url=None, release_notes_raw="raw notes",
    )
    members = [_c("sonarr", repo="linuxserver/sonarr"), _c("radarr", repo="linuxserver/radarr")]
    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", return_value="Analysis.") as mock_analyze:
        stacks.regenerate_stack_analysis("stack1", members)

    changed_summary = mock_analyze.call_args[0][2]
    assert "Requires postgres >= 15." in changed_summary
    assert "radarr: no pending update." in changed_summary


def test_an_unchanged_fingerprint_skips_the_ai_call_on_the_next_pass():
    members = [_c("sonarr", latest_digest="sha256:v2"), _c("radarr", latest_digest="sha256:v2")]
    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", return_value="First analysis."):
        stacks.regenerate_stack_analysis("stack1", members)

    with patch("app.stacks.analyze_stack_impact") as mock_analyze:
        stacks.regenerate_stack_analysis("stack1", members)  # identical digests again
    mock_analyze.assert_not_called()


def test_a_changed_digest_triggers_a_fresh_ai_call():
    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", return_value="First analysis."):
        stacks.regenerate_stack_analysis("stack1", [_c("sonarr", latest_digest="sha256:v1"), _c("radarr", latest_digest="sha256:v1")])

    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", return_value="Second analysis.") as mock_analyze:
        stacks.regenerate_stack_analysis("stack1", [_c("sonarr", latest_digest="sha256:v2"), _c("radarr", latest_digest="sha256:v1")])

    mock_analyze.assert_called_once()
    assert db.get_stack_analysis("stack1")["analysis_markdown"] == "Second analysis."


def test_force_regenerates_even_with_an_unchanged_fingerprint():
    members = [_c("sonarr", latest_digest="sha256:v2"), _c("radarr", latest_digest="sha256:v2")]
    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", return_value="First analysis."):
        stacks.regenerate_stack_analysis("stack1", members)

    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", return_value="Forced re-analysis.") as mock_analyze:
        stacks.regenerate_stack_analysis("stack1", members, force=True)

    mock_analyze.assert_called_once()
    assert db.get_stack_analysis("stack1")["analysis_markdown"] == "Forced re-analysis."


def test_a_registry_error_falls_back_to_current_digest_for_the_fingerprint():
    """latest_digest is None this round (a registry error) -- the fingerprint must fall back to
    current_digest rather than treating "unknown" as its own distinct, ever-changing value that
    would force a real AI call on every single check while the registry stays down."""
    members_first = [_c("sonarr", current_digest="sha256:same", latest_digest="sha256:same"),
                      _c("radarr", current_digest="sha256:same", latest_digest="sha256:same")]
    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", return_value="Analysis."):
        stacks.regenerate_stack_analysis("stack1", members_first)

    members_error = [_c("sonarr", current_digest="sha256:same", latest_digest=None),
                      _c("radarr", current_digest="sha256:same", latest_digest="sha256:same")]
    with patch("app.stacks.analyze_stack_impact") as mock_analyze:
        stacks.regenerate_stack_analysis("stack1", members_error)
    mock_analyze.assert_not_called()


def test_a_failed_ai_call_never_raises_and_leaves_no_stale_write():
    members = [_c("sonarr"), _c("radarr")]
    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", side_effect=RuntimeError("provider down")):
        stacks.regenerate_stack_analysis("stack1", members)  # must not raise
    assert db.get_stack_analysis("stack1") is None


def test_a_blank_analysis_result_is_not_persisted():
    members = [_c("sonarr"), _c("radarr")]
    with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
         patch("app.stacks.analyze_stack_impact", return_value=None):
        stacks.regenerate_stack_analysis("stack1", members)
    assert db.get_stack_analysis("stack1") is None


# ---------------------------------------------------------------------------
# run_stack_analysis_pass
# ---------------------------------------------------------------------------

def test_disabled_by_default_never_calls_the_ai_even_with_a_real_stack():
    compose_file = _write_compose("gated.yml", {"sonarr": "linuxserver/sonarr", "radarr": "linuxserver/radarr"})
    try:
        containers = [_c("sonarr", repo="linuxserver/sonarr"), _c("radarr", repo="linuxserver/radarr")]
        with patch("app.stacks.analyze_stack_impact") as mock_analyze:
            stacks.run_stack_analysis_pass(containers)
        mock_analyze.assert_not_called()
    finally:
        compose_file.unlink()


def test_enabled_regenerates_every_qualifying_stack():
    compose_file = _write_compose("enabled.yml", {"sonarr": "linuxserver/sonarr", "radarr": "linuxserver/radarr"})
    try:
        db.set_deep_analysis_enabled("updates", True)
        containers = [_c("sonarr", repo="linuxserver/sonarr"), _c("radarr", repo="linuxserver/radarr")]
        with patch("app.stacks.generate_stack_name", return_value="Arr Stack"), \
             patch("app.stacks.analyze_stack_impact", return_value="Cross-service notes.") as mock_analyze:
            stacks.run_stack_analysis_pass(containers)
        mock_analyze.assert_called_once()
    finally:
        compose_file.unlink()


def test_no_qualifying_stacks_never_touches_the_ai_even_when_enabled():
    db.set_deep_analysis_enabled("updates", True)
    with patch("app.stacks.analyze_stack_impact") as mock_analyze:
        stacks.run_stack_analysis_pass([_c("standalone")])
    mock_analyze.assert_not_called()
