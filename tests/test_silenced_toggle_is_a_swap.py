"""Follow-up correction: the "Show silenced" toggle used to be additive -- toggling it on
merely revealed previously-hidden silenced entries ON TOP of whatever was already showing. The
actual ask is a genuine swap: by default only non-silenced (actionable) rows show; toggled on,
ONLY silenced rows show and the non-silenced ones disappear. Covers Updates' pending list and
Logs/Compose's Issues table, which all share this exact toggle."""

from unittest.mock import patch

from app import db


def _seed_update(container_name: str, silenced: bool):
    db.upsert_container_state(container_name, f"owner/{container_name}", "latest", "sha256:new")
    with patch("app.persist.release_notes.get_release_notes", return_value=(None, None)):
        db.record_update(
            container_name=container_name, image_repo=f"owner/{container_name}", tag="latest",
            old_digest="sha256:old", new_digest="sha256:new",
            summary_markdown=None, source_url=None, release_notes_raw=None,
        )
    if silenced:
        db.set_container_silenced(container_name, True)


def _cleanup_update(container_name: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM container_state WHERE container_name = ?", (container_name,))
        conn.execute("DELETE FROM updates WHERE container_name = ?", (container_name,))


def _cleanup_finding(source: str, subject: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM findings WHERE source = ? AND subject = ?", (source, subject))


def test_updates_show_silenced_hides_the_non_silenced_ones_instead_of_just_adding_to_them(client):
    _seed_update("swap-test-silenced", silenced=True)
    _seed_update("swap-test-active", silenced=False)

    resp = client.get("/updates?show_silenced=1")
    updates_section = resp.text[:resp.text.index("Tracked Containers")]
    assert "swap-test-silenced" in updates_section
    # The whole point: a genuinely non-silenced pending update must NOT still be present once
    # "show silenced" is toggled on -- the old additive behavior left it there.
    assert "swap-test-active" not in updates_section

    resp = client.get("/updates")
    updates_section = resp.text[:resp.text.index("Tracked Containers")]
    assert "swap-test-active" in updates_section
    assert "swap-test-silenced" not in updates_section

    _cleanup_update("swap-test-silenced")
    _cleanup_update("swap-test-active")


def _issues_section(resp_text: str) -> str:
    """Scopes assertions to just the Issues table -- the "Tracked Containers"/"All Tracked
    Compose Files" table below it always lists everything regardless of the toggle (same as
    Updates' own Tracked Containers table), so a plain full-page substring check would
    false-pass here."""
    return resp_text[:resp_text.index("Tracked Containers")]


def test_logs_issues_show_silenced_hides_subjects_with_an_active_finding(client):
    db.set_log_watch_checkpoint("swap-test-logs-silenced")
    fid1, _ = db.upsert_finding("logs", "swap-test-logs-silenced", "old noisy warning", "leak", "warning", "desc")
    db.set_finding_status(fid1, "silenced")

    db.set_log_watch_checkpoint("swap-test-logs-active")
    db.upsert_finding("logs", "swap-test-logs-active", "still happening", "crash", "critical", "desc2")

    resp = client.get("/logs?show_silenced=1")
    issues = _issues_section(resp.text)
    assert "swap-test-logs-silenced" in issues
    assert "swap-test-logs-active" not in issues

    resp = client.get("/logs")
    issues = _issues_section(resp.text)
    assert "swap-test-logs-active" in issues
    assert "swap-test-logs-silenced" not in issues

    _cleanup_finding("logs", "swap-test-logs-silenced")
    _cleanup_finding("logs", "swap-test-logs-active")


def test_a_subject_with_both_active_and_silenced_findings_only_shows_in_the_default_view(client):
    """A genuine mix is still actionable -- it belongs in the default (non-silenced) view, not
    the fully-silenced one, even though it does have some silenced findings sitting alongside."""
    db.set_log_watch_checkpoint("swap-test-logs-mixed")
    fid_silenced, _ = db.upsert_finding("logs", "swap-test-logs-mixed", "old warning", "leak", "warning", "desc")
    db.set_finding_status(fid_silenced, "silenced")
    db.upsert_finding("logs", "swap-test-logs-mixed", "new crash", "crash", "critical", "desc2")

    resp = client.get("/logs")
    assert "swap-test-logs-mixed" in _issues_section(resp.text)

    resp = client.get("/logs?show_silenced=1")
    assert "swap-test-logs-mixed" not in _issues_section(resp.text)

    _cleanup_finding("logs", "swap-test-logs-mixed")


def test_compose_issues_show_silenced_is_also_a_swap(client):
    db.upsert_finding("compose", "swap-test-compose-active.yml", "still an issue", "reliability", "critical", "desc")
    fid, _ = db.upsert_finding("compose", "swap-test-compose-silenced.yml", "old issue", "reliability", "warning", "desc2")
    db.set_finding_status(fid, "silenced")

    resp = client.get("/compose?show_silenced=1")
    assert "swap-test-compose-silenced.yml" in resp.text
    assert "swap-test-compose-active.yml" not in resp.text

    resp = client.get("/compose")
    assert "swap-test-compose-active.yml" in resp.text
    assert "swap-test-compose-silenced.yml" not in resp.text

    _cleanup_finding("compose", "swap-test-compose-active.yml")
    _cleanup_finding("compose", "swap-test-compose-silenced.yml")
