"""An explicit ask: the top Issues/Updates table on Updates/Logs/Compose should be collapsible
-- click the feature-header row (not its Check now/Regenerate/Reset buttons) to slide it shut,
with an up/down arrow indicating the toggle. Also a batch of page-text tweaks: drop the
redundant "Issues (N)" subheading Logs/Compose had (Updates never had one), rename "Updates (N)"
to "Updates Found (N)", capitalize "Tracked containers"/"Log health"/"Compose health", rename
Logs' "All containers" and Compose's "All compose files" to match, and center-align every badge
column project-wide (importance/read/silenced/severity/status).

Follow-up round: the collapse arrow was too small to notice, Log Health/Compose Health were
missing the "(N)" issue count Updates has, "All Tracked Compose Files" got shortened to "Tracked
Compose Files", and the Logs/Compose per-subject findings page (subject_findings.html) defaulted
to sorting by last-seen instead of severity like every other findings table in the app."""

from app import db


def _cleanup_update(container_name: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM container_state WHERE container_name = ?", (container_name,))
        conn.execute("DELETE FROM updates WHERE container_name = ?", (container_name,))


def test_updates_page_has_a_collapsible_header_targeting_a_real_collapse_body(client):
    resp = client.get("/updates")
    assert 'class="feature-header collapsible-header"' in resp.text
    assert 'data-collapse-target="updates-collapse-body"' in resp.text
    assert 'id="updates-collapse-body" class="collapse-body"' in resp.text
    assert 'class="collapse-arrow"' in resp.text


def test_logs_page_has_a_collapsible_header_targeting_a_real_collapse_body(client):
    resp = client.get("/logs")
    assert 'data-collapse-target="logs-collapse-body"' in resp.text
    assert 'id="logs-collapse-body" class="collapse-body"' in resp.text


def test_compose_page_has_a_collapsible_header_targeting_a_real_collapse_body(client):
    resp = client.get("/compose")
    assert 'data-collapse-target="compose-collapse-body"' in resp.text
    assert 'id="compose-collapse-body" class="collapse-body"' in resp.text


def test_base_html_has_the_collapse_toggle_script():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html").read_text()
    assert "collapsible-header" in text
    assert "topbar-right" in text
    assert "scrollHeight" in text


def test_updates_heading_says_updates_found_not_just_updates(client):
    resp = client.get("/updates")
    assert "Updates Found" in resp.text
    assert "<h1>\n    Updates\n" not in resp.text


def test_logs_and_compose_no_longer_show_the_redundant_issues_subheading(client):
    logs_resp = client.get("/logs")
    assert "<h2>Issues" not in logs_resp.text

    compose_resp = client.get("/compose")
    assert "<h2>Issues" not in compose_resp.text


def test_log_health_and_compose_health_headings_are_capitalized(client):
    assert "Log Health" in client.get("/logs").text
    assert "Compose Health" in client.get("/compose").text


def test_second_table_headings_renamed_and_capitalized(client):
    _seed = "heading-text-test-container"
    db.upsert_container_state(_seed, f"owner/{_seed}", "latest", "sha256:a")
    try:
        assert "Tracked Containers" in client.get("/updates").text
        assert "Tracked Containers" in client.get("/logs").text
        assert "Tracked Compose Files" in client.get("/compose").text
    finally:
        _cleanup_update(_seed)


def test_tracked_containers_table_silenced_column_is_centered(client):
    _seed = "silenced-col-centered-test"
    db.upsert_container_state(_seed, f"owner/{_seed}", "latest", "sha256:a")
    try:
        resp = client.get("/updates")
        section = resp.text[resp.text.index("Tracked Containers"):]
        header = section[:section.index("<tbody>")]
        assert "cell-centered" in header
        assert "sort=silenced" in header
    finally:
        _cleanup_update(_seed)


def test_issues_table_severity_and_read_columns_are_centered(client):
    fid, _ = db.upsert_finding("logs", "cell-centered-test-container", "OOM", "crash", "critical", "desc")
    db.set_finding_status(fid, "active")

    resp = client.get("/logs")
    section = resp.text[:resp.text.index("Tracked Containers")]
    assert 'class="cell-centered">' in section
    # Both Severity and Read headers should be wrapped in cell-centered <th>s.
    assert section.count('th class="cell-centered"') >= 2

    db.set_finding_status(fid, "silenced")


def test_logs_stack_detail_severity_and_read_columns_are_centered():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "templates" / "logs_stack_detail.html").read_text()
    assert '<th class="cell-centered">Severity</th>' in text
    assert '<th class="cell-centered">Read</th>' in text


def test_collapse_arrow_is_a_larger_font_size_than_the_original_11px():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()
    block = text[text.index(".collapse-arrow {"):text.index(".collapsible-header.collapsed .collapse-arrow")]
    assert "11px" not in block


def test_log_health_and_compose_health_show_an_issue_count_like_updates_does(client):
    fid, _ = db.upsert_finding("logs", "header-count-test-container", "OOM", "crash", "critical", "desc")
    db.set_finding_status(fid, "active")
    fid2, _ = db.upsert_finding("compose", "header-count-test.yml", "Missing restart policy", "reliability", "warning", "desc2")
    db.set_finding_status(fid2, "active")

    logs_resp = client.get("/logs")
    header = logs_resp.text[:logs_resp.text.index("</h1>")]
    assert 'id="logs-issues-count-badge"' in header
    assert "(0)" not in header  # the container we just seeded must be counted

    compose_resp = client.get("/compose")
    header = compose_resp.text[:compose_resp.text.index("</h1>")]
    assert 'id="compose-issues-count-badge"' in header
    assert "(0)" not in header

    db.set_finding_status(fid, "silenced")
    db.set_finding_status(fid2, "silenced")


def test_subject_findings_page_defaults_to_severity_sort_not_seen(client):
    fid_warn, _ = db.upsert_finding("logs", "default-sort-subject-test", "slow", "startup", "warning", "d1")
    fid_crit, _ = db.upsert_finding("logs", "default-sort-subject-test", "crash", "crash", "critical", "d2")
    db.set_finding_status(fid_warn, "active")
    db.set_finding_status(fid_crit, "active")

    resp = client.get("/logs/container/default-sort-subject-test")
    body = resp.text[resp.text.index("<tbody>"):]
    assert body.index("crash") < body.index("slow")

    db.set_finding_status(fid_warn, "silenced")
    db.set_finding_status(fid_crit, "silenced")


def test_compose_subject_findings_page_defaults_to_severity_sort_not_seen(client):
    fid_warn, _ = db.upsert_finding("compose", "default-sort-subject-test.yml", "Missing restart policy", "reliability", "warning", "d1")
    fid_crit, _ = db.upsert_finding("compose", "default-sort-subject-test.yml", "Privileged container", "security", "critical", "d2")

    resp = client.get("/compose/file?path=default-sort-subject-test.yml")
    body = resp.text[resp.text.index("<tbody>"):]
    assert body.index("Privileged container") < body.index("Missing restart policy")

    db.set_finding_status(fid_warn, "silenced")
    db.set_finding_status(fid_crit, "silenced")
