"""UI overhaul: base.html's page shell moved from a topbar-only layout (inline nav links, a
separate check-running banner inside <main>) to a persistent left sidebar rail + a fixed topbar
whose center region now carries the check-running status, plus new topbar controls (Check All,
an accent-color swatch, a light/dark theme toggle). These are template-content and route-level
checks, not full browser rendering -- see style.css for the actual layout/theme CSS."""

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
STYLE = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"


def _base_html():
    return (TEMPLATES / "base.html").read_text()


# ---------------------------------------------------------------------------
# Sidebar nav
# ---------------------------------------------------------------------------

def test_sidebar_has_one_link_per_feature_in_the_original_nav_order():
    text = _base_html()
    sidebar = text[text.index('id="sidebar"'):text.index("</nav>")]
    hrefs = ['href="/"', 'href="/updates"', 'href="/logs"', 'href="/compose"', 'href="/settings"']
    positions = [sidebar.index(h) for h in hrefs]
    assert positions == sorted(positions), "nav order must match the original left-to-right tabs"


def test_sidebar_links_carry_the_active_class_the_same_way_the_old_tabs_did():
    text = _base_html()
    assert "'active' if active_tab == 'overview' else ''" in text
    assert "'active' if active_tab == 'updates' else ''" in text
    assert "'active' if active_tab == 'logs' else ''" in text
    assert "'active' if active_tab == 'compose' else ''" in text
    assert "'active' if active_tab == 'settings' else ''" in text


def test_old_inline_tabs_nav_is_gone():
    text = _base_html()
    assert 'class="tabs"' not in text
    assert "\n.tabs {" not in STYLE.read_text()


def test_sidebar_toggle_button_exists_and_carries_aria_state():
    text = _base_html()
    assert 'id="sidebar-toggle"' in text
    assert "aria-expanded=" in text[text.index('id="sidebar-toggle"'):text.index('id="sidebar-toggle"') + 300]


def test_sidebar_collapse_state_persists_via_its_own_localstorage_key():
    text = _base_html()
    assert "service-sentinel-sidebar" in text


def test_sidebar_state_is_restored_by_the_blocking_head_script_not_body():
    """Regression guard for a real-world report: restoring the sidebar's expanded state from a
    non-blocking script further down <body> meant a full page navigation painted collapsed
    first, then snapped open a moment later, every time. Moved to live on <html> (the same
    element the head's blocking script already restores data-theme/data-accent on) so it's
    settled before first paint, same as the other two."""
    text = _base_html()
    head = text[:text.index("</head>")]
    assert "service-sentinel-sidebar" in head
    assert "dataset.sidebar" in head
    after_head = text[text.index("</head>") + len("</head>"):]
    body_open = after_head.index("<body")
    body_tag = after_head[body_open:after_head.index(">", body_open) + 1]
    assert body_tag == '<body data-tab="{{ active_tab }}">'


def test_sidebar_has_a_watermark_element_for_the_dimmed_background_logo():
    text = _base_html()
    assert "sidebar-watermark" in text
    style = STYLE.read_text()
    assert ".sidebar-watermark" in style
    rule_start = style.index(".sidebar-watermark {")
    rule_end = style.index("}", rule_start)
    assert "opacity:" in style[rule_start:rule_end]


# ---------------------------------------------------------------------------
# Icon sprite -- every nav/topbar icon is a <use> against a <symbol>, not a duplicated inline
# <svg> per occurrence.
# ---------------------------------------------------------------------------

def test_icon_sprite_defines_one_symbol_per_nav_icon():
    text = _base_html()
    for icon_id in ("icon-menu", "icon-home", "icon-updates", "icon-logs", "icon-compose", "icon-settings", "icon-sun", "icon-moon"):
        assert f'id="{icon_id}"' in text


def test_sidebar_links_reference_the_sprite_not_inline_paths():
    text = _base_html()
    sidebar = text[text.index('id="sidebar"'):text.index("</nav>")]
    assert sidebar.count("<use href=") == 5
    assert "<path" not in sidebar  # no raw path data duplicated per link


# ---------------------------------------------------------------------------
# Topbar: three regions, check-running status now in the center, new end controls
# ---------------------------------------------------------------------------

def test_topbar_has_three_regions_in_document_order():
    text = _base_html()
    topbar = text[text.index('class="topbar"'):text.index("</header>")]
    start = topbar.index('class="topbar-start"')
    center = topbar.index('class="topbar-center"')
    end = topbar.index('class="topbar-end"')
    assert start < center < end


def test_check_running_status_lives_in_the_topbar_center_region():
    text = _base_html()
    center_start = text.index('class="topbar-center"')
    center_end = text.index("</header>")
    center = text[center_start:center_end]
    assert 'id="check-running-banner"' in center
    assert 'id="check-running-banner-text"' in center
    assert 'id="check-running-banner-cancel"' in center


def test_check_all_button_and_theme_controls_live_in_the_topbar_end_region():
    text = _base_html()
    end_start = text.index('class="topbar-end"')
    end_text = text[end_start:text.index("</header>")]
    assert 'id="check-all-btn"' in end_text
    assert 'id="accent-swatch-btn"' in end_text
    assert 'id="theme-toggle-btn"' in end_text


def test_accent_picker_offers_several_named_color_options():
    """The picker shows real, distinctly-named choices rather than a single disabled
    placeholder -- all seven actually switch the app's accent color, including Graphite Grey,
    the accessibility-motivated one (see test_topbar_idle_summary_and_nordic_blue.py's own
    accent tests)."""
    text = _base_html()
    menu = text[text.index('id="accent-picker-menu"'):text.index("</div>", text.index('id="accent-picker-menu"'))]
    for name in ("Emerald Green", "Nordic Blue", "Sunset Amber", "Royal Violet", "Crimson Red", "Ocean Teal",
                 "Graphite Grey"):
        assert name in menu
    assert menu.count("accent-picker-option") >= 7
    assert "disabled" not in text[text.index('id="accent-swatch-btn"'):text.index('id="accent-swatch-btn"') + 200]


def test_accent_picker_options_never_hit_the_network():
    """Persistence is client-side only (localStorage), same pattern as the theme toggle --
    clicking an option never calls out to the server."""
    text = _base_html()
    picker_script = text[text.index("Accent color picker"):text.index("Collapsible top table")]
    assert "fetch(" not in picker_script
    assert "hx-post" not in picker_script


# ---------------------------------------------------------------------------
# Check All wiring -- disabled by the same sitewide poll as every other check-starting control
# ---------------------------------------------------------------------------

def test_check_all_button_is_included_in_the_sitewide_disable_selector():
    text = _base_html()
    assert "#check-all-btn" in text


def test_check_all_button_posts_to_the_check_all_route_on_click():
    text = _base_html()
    assert '"/checks/check-all"' in text


# ---------------------------------------------------------------------------
# Theme system
# ---------------------------------------------------------------------------

def test_html_tag_has_a_hardcoded_dark_default_for_the_no_js_fallback():
    """Nordic Blue is the real app default accent (see style.css's accent family blocks), not
    just a placeholder, so it's hardcoded here the same way data-theme="dark" is. The sidebar's
    collapsed default lives on the same tag now too (see the head script's own comment on why),
    and the chat widget's closed default alongside it for the same reason."""
    text = _base_html()
    assert '<html lang="en" data-theme="dark" data-accent="nordic" data-sidebar="collapsed" data-chat="closed">' in text


def test_head_has_a_blocking_inline_theme_script_before_the_stylesheet_link():
    text = _base_html()
    script_start = text.index("<script>")
    stylesheet_pos = text.index('rel="stylesheet"')
    assert script_start < stylesheet_pos
    head_script = text[script_start:text.index("</script>", script_start)]
    assert "localStorage" in head_script
    assert "service-sentinel-theme" in head_script


def test_theme_toggle_writes_the_same_localstorage_key_the_head_script_reads():
    text = _base_html()
    assert text.count("service-sentinel-theme") >= 2


def test_style_defines_both_a_dark_and_a_light_theme_block():
    style = STYLE.read_text()
    assert ':root[data-theme="dark"]' in style
    assert ':root[data-theme="light"]' in style
    light_start = style.index(':root[data-theme="light"]')
    light_block = style[light_start:light_start + style[light_start:].index("}")]
    for token in ("--bg", "--bg-panel", "--border", "--text", "--text-dim", "--accent", "--warn", "--error", "--info", "--violet", "color-scheme: light"):
        assert token in light_block


def test_no_bare_white_or_black_rgba_background_tints_remain_outside_the_theme_blocks():
    """Every rgba(255,255,255,...)/rgba(0,0,0,...) hover/background TINT must go through a
    theme-aware variable now -- a literal one anywhere else would look wrong (invisible or
    inverted) in the theme it wasn't written for. Scoped to background/background-color
    specifically -- a black box-shadow (a drop shadow, e.g. the accent picker's popover) is a
    deliberate, correct exception: shadows read as "shadow" in both themes without needing to
    flip to white in light mode, unlike a background tint."""
    style = STYLE.read_text()
    dark_start = style.index(':root[data-theme="dark"]')
    light_end_marker = ':root[data-theme="light"]'
    after_theme_blocks = style[style.index(light_end_marker):]
    after_theme_blocks = after_theme_blocks[after_theme_blocks.index("}") + 1:]
    assert "background: rgba(255, 255, 255," not in after_theme_blocks
    assert "background: rgba(0, 0, 0," not in after_theme_blocks
    assert "background-color: rgba(255, 255, 255," not in after_theme_blocks
    assert "background-color: rgba(0, 0, 0," not in after_theme_blocks
    assert dark_start >= 0  # sanity: the theme blocks were actually found above
