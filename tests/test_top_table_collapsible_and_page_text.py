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


def test_each_module_page_is_headed_by_its_module_name(client):
    """The heading used to read "Updates Found", which named the result rather than the module
    and left this page the odd one out beside "Runtime Health"/"Configuration Health". All three
    are now headed by the module itself -- see test_module_names.py for the rename."""
    assert '<span class="feature-title-text">Versions</span>' in client.get("/updates").text
    assert '<span class="feature-title-text">Runtime</span>' in client.get("/logs").text
    assert '<span class="feature-title-text">Configuration</span>' in client.get("/compose").text
    assert "Updates Found" not in client.get("/updates").text


def test_logs_and_compose_no_longer_show_the_redundant_issues_subheading(client):
    logs_resp = client.get("/logs")
    assert "<h2>Issues" not in logs_resp.text

    compose_resp = client.get("/compose")
    assert "<h2>Issues" not in compose_resp.text


def test_the_module_headings_are_title_cased(client):
    """The "Health" suffix these carried is gone (it wasn't earning its place next to two other
    long labels), but the capitalization this test was originally about still holds."""
    assert "Runtime" in client.get("/logs").text
    assert "runtime health" not in client.get("/logs").text.lower().replace("runtime health check", "")
    assert "Configuration" in client.get("/compose").text


def test_second_table_headings_renamed_and_capitalized(client):
    _seed = "heading-text-test-container"
    db.upsert_container_state(_seed, f"owner/{_seed}", "latest", "sha256:a")
    try:
        assert "Tracked Containers" in client.get("/updates").text
        assert "Tracked Containers" in client.get("/logs").text
        assert "Tracked Configuration Files" in client.get("/compose").text
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
    assert 'class="cell-centered severity-cell">' in section
    # Both Severity and Read headers should be wrapped in cell-centered <th>s (each also
    # carries its own severity-cell/read-cell class -- see style.css's compact mobile rules).
    assert 'th class="cell-centered severity-cell"' in section
    assert 'th class="cell-centered read-cell"' in section

    db.set_finding_status(fid, "silenced")


def test_logs_stack_detail_severity_and_read_columns_are_centered():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "templates" / "logs_stack_detail.html").read_text()
    # Headers are now sort links (see _sort_header.html), not bare <th>Label</th>.
    header = text[text.index("<thead>"):text.index("</thead>")]
    assert header.count('th class="cell-centered severity-cell"') == 1
    assert header.count('th class="cell-centered read-cell"') == 1
    assert "'severity'" in header
    assert "'read'" in header


def test_collapse_arrow_is_a_larger_font_size_than_the_original_11px():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()
    block = text[text.index(".collapse-arrow {"):text.index(".collapsible-header.collapsed .collapse-arrow")]
    assert "11px" not in block


def test_the_three_feature_headings_carry_no_count_but_the_tracked_tables_still_do(client):
    """These headings used to repeat the module's open/pending total, which the Overview page
    already reports for all three -- so the count came off them. The "Tracked Containers" and
    "Tracked Configuration Files" headings further down each page keep theirs: that number
    isn't shown anywhere else."""
    fid, _ = db.upsert_finding("logs", "header-count-test-container", "OOM", "crash", "critical", "desc")
    db.set_finding_status(fid, "active")
    fid2, _ = db.upsert_finding("compose", "header-count-test.yml", "Missing restart policy", "reliability", "warning", "desc2")
    db.set_finding_status(fid2, "active")

    for path, badge_id in (("/updates", "updates-count-badge"),
                           ("/logs", "logs-issues-count-badge"),
                           ("/compose", "compose-issues-count-badge")):
        resp = client.get(path)
        heading = resp.text[:resp.text.index("</h1>")]
        assert badge_id not in heading
        assert "heading-count" not in heading

    # ...while each page's own tracked-items table keeps its count.
    assert 'id="containers-count-badge"' in client.get("/updates").text
    assert 'id="logs-containers-table-count-badge"' in client.get("/logs").text
    assert 'id="compose-files-table-count-badge"' in client.get("/compose").text

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
    compose_path = "/tmp/rr-test-compose/default-sort-subject-test.yml"
    fid_warn, _ = db.upsert_finding("compose", compose_path, "Missing restart policy", "reliability", "warning", "d1")
    fid_crit, _ = db.upsert_finding("compose", compose_path, "Privileged container", "security", "critical", "d2")

    resp = client.get(f"/compose/file?path={compose_path}")
    body = resp.text[resp.text.index("<tbody>"):]
    assert body.index("Privileged container") < body.index("Missing restart policy")

    db.set_finding_status(fid_warn, "silenced")
    db.set_finding_status(fid_crit, "silenced")


def test_issues_table_last_seen_column_renamed_to_detected_like_updates(client):
    fid, _ = db.upsert_finding("logs", "detected-header-logs-test", "OOM", "crash", "critical", "desc")
    db.set_finding_status(fid, "active")

    resp = client.get("/logs")
    table = resp.text[resp.text.index('id="logs-issues-table"'):]
    header = table[:table.index("<tbody>")]
    assert "Detected" in header
    assert "Last seen" not in header

    db.set_finding_status(fid, "silenced")


def test_logs_stack_detail_last_seen_column_renamed_to_detected(client):
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "templates" / "logs_stack_detail.html").read_text()
    # Header is now a sort link (see _sort_header.html), not a bare <th>Detected</th>.
    assert "sh.sort_link('Detected', 'detected'" in text
    assert "Last seen" not in text


def test_subject_findings_page_seen_column_renamed_to_detected_and_drops_occurrence_prefix(client):
    # 2+ findings needed -- a subject with exactly one finding redirects straight to that
    # finding's own detail page instead of rendering this table at all.
    fid1, _ = db.upsert_finding("logs", "detected-format-test", "OOM", "crash", "critical", "desc")
    fid2, _ = db.upsert_finding("logs", "detected-format-test", "Disk pressure", "resource", "warning", "desc2")
    db.set_finding_status(fid1, "active")
    db.set_finding_status(fid2, "active")

    resp = client.get("/logs/container/detected-format-test")
    table = resp.text[resp.text.index("findings-table"):]
    header = table[:table.index("<tbody>")]
    assert "sort=seen" in header  # internal sort param is unchanged, only the label
    body = table[table.index("<tbody>"):]
    assert "×" not in body
    assert "last " not in body

    db.set_finding_status(fid1, "silenced")
    db.set_finding_status(fid2, "silenced")


def test_collapse_state_does_not_persist_across_page_loads():
    """The collapsed/expanded state of the top table must NOT survive navigating away and back
    or reloading -- every fresh page load should show the table expanded. The shared toggle
    handler gained a persistence path for the Settings page's panels, which do want to be
    remembered; it's gated behind a data-collapse-key attribute this header deliberately doesn't
    have, so nothing about the table's own behavior changed."""
    from pathlib import Path
    templates = Path(__file__).resolve().parent.parent / "app" / "templates"
    header = (templates / "_feature_header.html").read_text()
    assert "collapsible-header" in header
    assert "data-collapse-key" not in header, "this table must not opt in to remembered state"
