"""Phase 2 of the Overview redesign: a "Healthy for N days"/"Issues for N days" streak per
module, and a cross-module "Attention Required" feed ranking Updates' and Logs/Compose's two
different severity vocabularies onto one shared critical/warning scale."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import db


def _cleanup_container(container_name: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM container_state WHERE container_name = ?", (container_name,))
        conn.execute("DELETE FROM updates WHERE container_name = ?", (container_name,))


def _cleanup_findings(source: str, subject: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM findings WHERE source = ? AND subject = ?", (source, subject))


def _cleanup_streak(feature: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key IN (?, ?)",
                      (f"health_streak_{feature}_state", f"health_streak_{feature}_since"))


def test_health_streak_is_none_until_first_observed():
    _cleanup_streak("streaktest")
    streak = db.get_feature_health_streak("streaktest")
    assert streak == {"healthy": None, "since": None}


def test_health_streak_resets_only_on_an_actual_transition():
    _cleanup_streak("streaktest")
    first_since = db.update_feature_health_streak("streaktest", healthy_now=True)
    # Calling again with the same state must not reset the clock.
    second_since = db.update_feature_health_streak("streaktest", healthy_now=True)
    assert first_since == second_since

    # Backdate the recorded "since" so a genuine transition is provably a fresh timestamp, not
    # just "whatever now() happens to be" matching by coincidence.
    with db.get_conn() as conn:
        conn.execute("UPDATE app_settings SET value = ? WHERE key = ?",
                      ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
                       "health_streak_streaktest_since"))

    flipped_since = db.update_feature_health_streak("streaktest", healthy_now=False)
    assert flipped_since != first_since
    streak = db.get_feature_health_streak("streaktest")
    assert streak["healthy"] is False
    _cleanup_streak("streaktest")


def test_overview_shows_a_healthy_streak_line(client):
    _cleanup_streak("compose")
    resp = client.get("/")
    card = resp.text[resp.text.index('id="card-compose"'):]
    assert "Healthy since today" in card or "Healthy for" in card
    _cleanup_streak("compose")


def _seed_container_with_update(container_name: str, severity: str):
    db.upsert_container_state(container_name, f"owner/{container_name}", "latest", "sha256:new")
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name=container_name, image_repo=f"owner/{container_name}", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown=None, source_url=None, release_notes_raw=None, severity=severity,
        )


def test_attention_items_excludes_low_severity_and_ranks_critical_first():
    fid_warn, _ = db.upsert_finding("logs", "attn-subject", "Elevated memory", "reliability", "warning", "d")
    fid_suggestion, _ = db.upsert_finding("compose", "attn-subject-2", "Consider adding healthcheck",
                                           "reliability", "suggestion", "d")
    fid_critical, _ = db.upsert_finding("compose", "attn-subject-3", "Docker socket exposed",
                                         "security", "critical", "d")
    try:
        items = db.list_attention_items(limit=10)
        titles = [i["title"] for i in items]
        assert "Docker socket exposed" in titles
        assert "Elevated memory" in titles
        # Suggestion-tier findings never count as "needing attention".
        assert "Consider adding healthcheck" not in titles
        # Critical ranks above warning.
        assert titles.index("Docker socket exposed") < titles.index("Elevated memory")
    finally:
        _cleanup_findings("logs", "attn-subject")
        _cleanup_findings("compose", "attn-subject-2")
        _cleanup_findings("compose", "attn-subject-3")


def test_attention_items_excludes_silenced_and_low_severity_updates():
    _seed_container_with_update("attn-update-breaking", "breaking")
    _seed_container_with_update("attn-update-bugfix", "bugfix")
    _seed_container_with_update("attn-update-silenced", "breaking")
    db.set_container_silenced("attn-update-silenced", True)
    try:
        items = db.list_attention_items(limit=10)
        titles = [i["title"] for i in items]
        assert "attn-update-breaking" in titles
        # A plain bugfix update isn't something that needs "attention".
        assert "attn-update-bugfix" not in titles
        # Silenced containers never surface here either, same as the Updates page itself.
        assert "attn-update-silenced" not in titles
    finally:
        _cleanup_container("attn-update-breaking")
        _cleanup_container("attn-update-bugfix")
        _cleanup_container("attn-update-silenced")


def test_overview_attention_panel_renders_with_display_names_and_links(client):
    fid, _ = db.upsert_finding("compose", "attn-render-subject", "Docker socket exposed",
                                "security", "critical", "d")
    try:
        resp = client.get("/")
        assert "Attention Required" in resp.text
        assert "Docker socket exposed" in resp.text
        assert f'href="/findings/{fid}"' in resp.text
    finally:
        _cleanup_findings("compose", "attn-render-subject")
