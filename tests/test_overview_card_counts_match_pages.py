"""A real-world report: the Overview page said "Up to date" while the Updates page itself
showed 25 pending -- the card's count came from a different, narrower query (unread updates
only / raw active-finding rows regardless of silenced state) than what each feature's own page
actually displays by default. The Overview hero metric must always agree with what a click into
that tab shows."""

from unittest.mock import patch

from app import db


def _seed_container_with_update(container_name: str):
    db.upsert_container_state(container_name, f"owner/{container_name}", "latest", "sha256:new")
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name=container_name, image_repo=f"owner/{container_name}", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown=None, source_url=None, release_notes_raw=None,
        )


def _cleanup_container(container_name: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM container_state WHERE container_name = ?", (container_name,))
        conn.execute("DELETE FROM updates WHERE container_name = ?", (container_name,))


def _cleanup_findings(source: str, subject: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM findings WHERE source = ? AND subject = ?", (source, subject))


def test_overview_updates_hero_excludes_silenced_containers(client):
    _seed_container_with_update("ovcount-visible")
    _seed_container_with_update("ovcount-silenced")
    db.set_container_silenced("ovcount-silenced", True)
    try:
        resp = client.get("/")
        card = resp.text[resp.text.index('id="card-updates"'):resp.text.index('id="card-logs"')]
        assert "1 pending update" in card

        updates_resp = client.get("/updates")
        assert 'id="updates-count-badge">(1)' in updates_resp.text
    finally:
        _cleanup_container("ovcount-visible")
        _cleanup_container("ovcount-silenced")


def test_overview_runtime_hero_counts_subjects_not_raw_finding_rows(client):
    fid1, _ = db.upsert_finding("logs", "ovcount-subject", "First issue", "reliability", "warning", "desc")
    fid2, _ = db.upsert_finding("logs", "ovcount-subject", "Second issue", "reliability", "warning", "desc 2")
    try:
        resp = client.get("/")
        card = resp.text[resp.text.index('id="card-logs"'):resp.text.index('id="card-compose"')]
        # Two active findings on the same subject -- the Issues table shows one row for it, so
        # the hero metric must say "1 Issue", not "2 Issues".
        assert "1 Issue" in card
        assert "2 Issue" not in card

        db.set_finding_status(fid1, "silenced")
        db.set_finding_status(fid2, "silenced")
        resp = client.get("/")
        card = resp.text[resp.text.index('id="card-logs"'):resp.text.index('id="card-compose"')]
        assert "All clean" in card
    finally:
        _cleanup_findings("logs", "ovcount-subject")
