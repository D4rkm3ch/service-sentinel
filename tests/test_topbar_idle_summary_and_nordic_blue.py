"""Two follow-up UI overhaul features: (1) the topbar's idle-state health summary
(_compact_health_summary / GET /checks/status), which replaces the blank space in the topbar's
center region with a compact combined status whenever nothing is running, and (2) real,
functional accent picker options -- Nordic Blue (the app's default) and Emerald Green first,
later joined by Sunset Amber, Royal Violet, Crimson Red, Ocean Teal, and three
accessibility-motivated accents: Graphite Grey (a genuinely neutral palette), Red-Green Safe
(deuteranopia/protanopia), and Blue-Yellow Safe (tritanopia) -- see style.css's own blocks for
the color research behind each."""

from pathlib import Path

from app import check_state, db

db.init_db()

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
STYLE = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"


def _reset():
    # release_running() alone doesn't clear last_result/last_run_at (see check_state.py's own
    # docstring on why) -- reset the module-level state dict directly so a set_finished() call
    # in one test never bleeds its "last checked" timestamp into the next. get_state() also
    # falls back to a DB-persisted copy (app_settings.last_check_result_{feature}) when the
    # in-memory value is None, so that has to be cleared too, not just the in-memory dict.
    for feature in check_state.FEATURES:
        check_state._state[feature] = {"running": False, "last_result": None, "last_run_at": None}
    with db.get_conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key LIKE 'last_check_result_%'")
    db.reset_updates_data()
    db.reset_logs_data()
    db.reset_compose_data()
    for feature in ("updates", "logs", "compose"):
        db.set_feature_enabled(feature, True)


def setup_function(_):
    _reset()


def teardown_function(_):
    _reset()


# ---------------------------------------------------------------------------
# Idle health summary
# ---------------------------------------------------------------------------

def test_no_checks_run_yet_reports_idle(client):
    resp = client.get("/checks/status")
    data = resp.json()
    assert data["summary_text"] == "No checks run yet"
    assert data["summary_status"] == "idle"


def test_all_features_disabled_reports_idle(client):
    """Folded into the same "No checks run yet" wording as the never-checked case -- the topbar
    only ever shows one of exactly three forms (see _compact_health_summary's docstring), not a
    fourth "disabled" variant."""
    for feature in ("updates", "logs", "compose"):
        db.set_feature_enabled(feature, False)
    resp = client.get("/checks/status")
    data = resp.json()
    assert data["summary_text"] == "No checks run yet"
    assert data["summary_status"] == "idle"


def test_everything_clean_reports_ok(client):
    """A container with everything resolved (no pending update row at all) must still read as
    "checked, all clear" rather than "never checked" -- see check_state.set_finished(), which
    stamps last_run_at regardless of whether anything was actually found."""
    for feature in check_state.FEATURES:
        check_state.set_finished(feature, {"checked": 1})
    resp = client.get("/checks/status")
    data = resp.json()
    assert data["summary_text"] == "All Clear"
    assert data["summary_status"] == "ok"


def test_open_issues_are_counted_and_reported_as_warn(client):
    check_state.set_finished("updates", {"checked": 1})
    db.record_update(
        container_name="needs-update", image_repo="owner/repo", tag="latest",
        old_digest="sha256:a", new_digest="sha256:b", summary_markdown="x",
        source_url=None, error=None, severity="feature", release_notes_raw="x",
        upgrade_guidance=None,
    )
    resp = client.get("/checks/status")
    data = resp.json()
    assert data["summary_text"] == "1 Update pending • 0 Runtime issues • 0 Configuration issues"
    assert data["summary_status"] == "warn"


def test_disabled_features_still_count_toward_the_breakdown(client):
    """A real-world report: a disabled feature's own real, nonzero count used to be silently
    excluded from the breakdown, which could read as "All Clear" while an issue sat right there
    on that disabled feature. The enabled toggle only pauses a feature's own scheduled/automatic
    checks -- it must never hide an already-found result, so this has to keep reporting the
    pending update even with Updates disabled."""
    db.record_update(
        container_name="needs-update", image_repo="owner/repo", tag="latest",
        old_digest="sha256:a", new_digest="sha256:b", summary_markdown="x",
        source_url=None, error=None, severity="feature", release_notes_raw="x",
        upgrade_guidance=None,
    )
    db.set_feature_enabled("updates", False)
    resp = client.get("/checks/status")
    data = resp.json()
    assert data["summary_text"] == "1 Update pending • 0 Runtime issues • 0 Configuration issues"
    assert data["summary_status"] == "warn"


def test_real_data_with_every_feature_disabled_never_reports_no_checks_run_yet(client):
    """Regression guard for a real-world report: with historical updates/findings already
    present but every feature toggled off, the summary must not regress to the pristine "No
    checks run yet" state -- it still reports the real count."""
    db.record_update(
        container_name="needs-update-2", image_repo="owner/repo", tag="latest",
        old_digest="sha256:a", new_digest="sha256:b", summary_markdown="x",
        source_url=None, error=None, severity="feature", release_notes_raw="x",
        upgrade_guidance=None,
    )
    for feature in ("updates", "logs", "compose"):
        db.set_feature_enabled(feature, False)
    resp = client.get("/checks/status")
    data = resp.json()
    assert data["summary_text"] == "1 Update pending • 0 Runtime issues • 0 Configuration issues"
    assert data["summary_status"] == "warn"


def test_idle_summary_lives_in_the_topbar_center_and_is_hidden_by_default():
    text = (TEMPLATES / "base.html").read_text()
    center_start = text.index('class="topbar-center"')
    center_end = text.index("</header>")
    center = text[center_start:center_end]
    assert 'id="topbar-idle-summary"' in center
    assert 'id="topbar-idle-summary-text"' in center
    assert 'id="topbar-idle-summary-dot"' in center
    idle_start = center.index('id="topbar-idle-summary"')
    idle_tag = center[idle_start - 40:idle_start + 100]
    assert "display:none" in idle_tag


def test_status_dot_has_ok_and_warn_color_variants():
    style = STYLE.read_text()
    assert ".status-dot-ok" in style
    assert ".status-dot-warn" in style
    assert ".status-dot-idle" in style


# ---------------------------------------------------------------------------
# Nordic Blue -- the new real, functional default accent
# ---------------------------------------------------------------------------

def test_nordic_blue_is_the_hardcoded_default_accent():
    text = (TEMPLATES / "base.html").read_text()
    assert 'data-accent="nordic"' in text
    assert '<html lang="en" data-theme="dark" data-accent="nordic" data-sidebar="collapsed">' in text


def test_head_script_also_restores_the_saved_accent():
    text = (TEMPLATES / "base.html").read_text()
    head = text[:text.index("</head>")]
    assert "service-sentinel-accent" in head
    assert "dataset.accent" in head


ALL_ACCENTS = (
    "nordic", "emerald", "amber", "violet", "crimson", "teal",
    "graphite", "cvd-redgreen", "cvd-blueyellow",
)


def test_all_nine_accents_have_real_css_blocks_for_both_themes():
    style = STYLE.read_text()
    for theme in ("dark", "light"):
        for accent in ALL_ACCENTS:
            selector = f':root[data-theme="{theme}"][data-accent="{accent}"]'
            assert selector in style


def test_all_nine_options_carry_a_data_accent_attribute_in_the_picker():
    text = (TEMPLATES / "base.html").read_text()
    menu_start = text.index('id="accent-picker-menu"')
    menu_end = text.index("</div>\n      </div>", menu_start)  # closes #accent-picker-menu, then .accent-picker
    menu = text[menu_start:menu_end]

    options = menu.split("<button")[1:]  # each option's own opening tag through its closing </button>
    assert len(options) == 9

    labels = {
        "nordic": "Nordic Blue", "emerald": "Emerald Green", "amber": "Sunset Amber",
        "violet": "Royal Violet", "crimson": "Crimson Red", "teal": "Ocean Teal",
        "graphite": "Graphite Grey", "cvd-redgreen": "Red-Green Safe",
        "cvd-blueyellow": "Blue-Yellow Safe",
    }
    assert len(labels) == len(options)

    for accent, label in labels.items():
        option = next(o for o in options if f'data-label="{label}"' in o)
        assert f'data-accent="{accent}"' in option
        assert "disabled" not in option


def test_accent_picker_js_persists_the_real_picks_via_localstorage():
    text = (TEMPLATES / "base.html").read_text()
    picker_script = text[text.index("Accent color picker"):text.index("Collapsible top table")]
    assert "service-sentinel-accent" in picker_script
    assert "localStorage.setItem" in picker_script
    assert "option.dataset.accent" in picker_script
