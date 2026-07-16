"""Phase 2 of the Overview redesign: a "Healthy for N days"/"Issues for N days" streak per
module, and a per-module "top issues" feed (shown inside each module's own row, not a separate
panel) ranked by severity across Updates' and Logs/Compose's two different vocabularies. Nothing
is excluded by severity -- an earlier version hid bugfix/suggestion-tier items entirely, which
read as broken the moment a module's hero count said e.g. "3 Issues" but none of them showed up
as a box because all three happened to be low severity."""

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


def test_attention_items_include_low_severity_and_rank_critical_first():
    fid_warn, _ = db.upsert_finding("logs", "attn-subject", "Elevated memory", "reliability", "warning", "d")
    fid_suggestion, _ = db.upsert_finding("compose", "attn-subject-2", "Consider adding healthcheck",
                                           "reliability", "suggestion", "d")
    fid_critical, _ = db.upsert_finding("compose", "attn-subject-3", "Docker socket exposed",
                                         "security", "critical", "d")
    try:
        logs_blurbs = [i["blurb"] for i in db.list_attention_items_for_feature("logs", limit=10)]
        compose_blurbs = [i["blurb"] for i in db.list_attention_items_for_feature("compose", limit=10)]
        assert "Elevated memory" in logs_blurbs
        assert "Docker socket exposed" in compose_blurbs
        # A suggestion-tier finding still shows up (a module's hero count includes it too).
        assert "Consider adding healthcheck" in compose_blurbs
        # Critical ranks above suggestion within its own module.
        assert compose_blurbs.index("Docker socket exposed") < compose_blurbs.index("Consider adding healthcheck")
    finally:
        _cleanup_findings("logs", "attn-subject")
        _cleanup_findings("compose", "attn-subject-2")
        _cleanup_findings("compose", "attn-subject-3")


def test_overview_renders_an_unclassified_pending_update_without_crashing(client):
    """A real crash: an update whose severity hasn't been classified yet (record_update's own
    default is "", which list_tracked_containers_with_status then normalizes to None) reached
    severity_label() unguarded once low-severity items stopped being filtered out entirely,
    raising AttributeError on None.capitalize()."""
    db.upsert_container_state("attn-unclassified", "owner/attn-unclassified", "latest", "sha256:new")
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name="attn-unclassified", image_repo="owner/attn-unclassified", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown=None, source_url=None, release_notes_raw=None,
        )
    try:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "attn-unclassified" in resp.text
    finally:
        _cleanup_container("attn-unclassified")


def test_attention_items_dedupe_findings_by_subject_to_the_worst_one():
    db.upsert_finding("logs", "attn-multi-subject", "Minor log noise", "reliability", "suggestion", "d")
    fid_critical, _ = db.upsert_finding("logs", "attn-multi-subject", "Container crash-looping",
                                         "reliability", "critical", "d")
    try:
        items = db.list_attention_items_for_feature("logs", limit=10)
        matches = [i for i in items if i["name"] == "attn-multi-subject"]
        # One box per subject, not one per finding -- matching list_subjects_with_findings'
        # own per-subject counting -- and it's the subject's most severe finding that wins.
        assert len(matches) == 1
        assert matches[0]["blurb"] == "Container crash-looping"
        assert matches[0]["url"] == f"/findings/{fid_critical}"
    finally:
        _cleanup_findings("logs", "attn-multi-subject")


def test_attention_items_excludes_silenced_updates():
    _seed_container_with_update("attn-update-breaking", "breaking")
    _seed_container_with_update("attn-update-bugfix", "bugfix")
    _seed_container_with_update("attn-update-silenced", "breaking")
    db.set_container_silenced("attn-update-silenced", True)
    try:
        names = [i["name"] for i in db.list_attention_items_for_feature("updates", limit=10)]
        assert "attn-update-breaking" in names
        # A plain bugfix update still shows up now (matches the hero's own pending count).
        assert "attn-update-bugfix" in names
        # Silenced containers never surface here either, same as the Updates page itself.
        assert "attn-update-silenced" not in names
    finally:
        _cleanup_container("attn-update-breaking")
        _cleanup_container("attn-update-bugfix")
        _cleanup_container("attn-update-silenced")


def test_attention_items_for_feature_caps_the_list():
    names = [f"attn-cap-{i}" for i in range(5)]
    for name in names:
        _seed_container_with_update(name, "breaking")
    try:
        updates_items = db.list_attention_items_for_feature("updates", limit=3)
        assert len(updates_items) == 3
    finally:
        for name in names:
            _cleanup_container(name)


def test_overview_card_shows_top_issue_slots_with_display_names_and_links(client):
    fid, _ = db.upsert_finding("compose", "attn-render-subject", "Docker socket exposed",
                                "security", "critical", "d")
    try:
        resp = client.get("/")
        card = resp.text[resp.text.index('id="card-compose"'):]
        assert "Docker socket exposed" in card
        assert f'href="/findings/{fid}"' in card
    finally:
        _cleanup_findings("compose", "attn-render-subject")


def test_overview_hero_color_reflects_worst_severity_present(client):
    """A breaking-change update must read as critical (red), not just "not healthy" -- the
    same amber every other issue used to get regardless of how severe it actually was."""
    _seed_container_with_update("attn-hero-breaking", "breaking")
    try:
        resp = client.get("/")
        card = resp.text[resp.text.index('id="card-updates"'):resp.text.index('id="card-logs"')]
        assert "hero-critical" in card
    finally:
        _cleanup_container("attn-hero-breaking")


def test_overview_hero_color_is_neutral_for_a_plain_bugfix_update(client):
    _seed_container_with_update("attn-hero-bugfix", "bugfix")
    try:
        resp = client.get("/")
        card = resp.text[resp.text.index('id="card-updates"'):resp.text.index('id="card-logs"')]
        assert "hero-neutral" in card
    finally:
        _cleanup_container("attn-hero-bugfix")
