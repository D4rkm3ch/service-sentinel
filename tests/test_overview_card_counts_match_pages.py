"""A real-world report: the Overview page said "Up to date" while the Updates page itself
showed 25 pending -- the card's count came from a different, narrower query (unread updates
only / raw active-finding rows regardless of silenced state) than what each feature's own page
actually displays by default. The Overview hero metric must always agree with what a click into
that tab shows."""

import re
from unittest.mock import patch

from app import db


def _runtime_issue_count(card_html: str) -> int:
    """Reads the Runtime card's own hero number back out of its rendered HTML ("3 Issues" / "1
    Issue" / "All clean"), for tests that need to assert on it moving by some amount."""
    if "All clean" in card_html:
        return 0
    match = re.search(r'feature-card-hero-text">\s*(\d+)\s*Issues?', card_html)
    assert match, "could not find the Runtime card's hero issue count in its HTML"
    return int(match.group(1))


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

        # The count badge that used to sit beside "Updates Found" is gone (the Overview hero
        # checked just above is now the only place that total is reported), so the agreement
        # between the two is checked against the rows the page actually renders instead.
        # Scoped to the pending-updates table specifically -- the silenced container still
        # appears further down in "Tracked Containers", which lists every tracked container
        # regardless of whether its update is silenced.
        updates_resp = client.get("/updates")
        table = updates_resp.text[updates_resp.text.index('id="updates-collapse-body"'):]
        table = table[:table.index("Tracked Containers")]
        assert "ovcount-visible" in table
        assert "ovcount-silenced" not in table
    finally:
        _cleanup_container("ovcount-visible")
        _cleanup_container("ovcount-silenced")


def test_overview_runtime_hero_counts_subjects_not_raw_finding_rows(client):
    # Baseline read before seeding anything -- the `client` fixture is session-scoped (one DB
    # shared across the whole test run, see conftest.py), so other subjects can legitimately
    # already be active here depending on what ran earlier. Asserting an absolute "1 Issue" was
    # only ever correct by coincidence of execution order: a real report where this test failed
    # in isolation (passing fine as part of the full suite) traced back to exactly that -- an
    # unrelated test's own "logs" finding was still active at this point. Asserting the count's
    # own delta instead of its absolute value is immune to that regardless of what else is active.
    resp = client.get("/")
    card = resp.text[resp.text.index('id="card-logs"'):resp.text.index('id="card-compose"')]
    baseline = _runtime_issue_count(card)

    fid1, _ = db.upsert_finding("logs", "ovcount-subject", "First issue", "reliability", "warning", "desc")
    fid2, _ = db.upsert_finding("logs", "ovcount-subject", "Second issue", "reliability", "warning", "desc 2")
    try:
        resp = client.get("/")
        card = resp.text[resp.text.index('id="card-logs"'):resp.text.index('id="card-compose"')]
        # Two active findings on the same subject -- the Issues table shows one row for it, so
        # the hero metric must go up by exactly 1, not 2.
        assert _runtime_issue_count(card) == baseline + 1

        db.set_finding_status(fid1, "silenced")
        db.set_finding_status(fid2, "silenced")
        resp = client.get("/")
        card = resp.text[resp.text.index('id="card-logs"'):resp.text.index('id="card-compose"')]
        assert _runtime_issue_count(card) == baseline
    finally:
        _cleanup_findings("logs", "ovcount-subject")
