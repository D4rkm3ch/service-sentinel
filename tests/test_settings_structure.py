"""Settings is grouped by MODULE, and every panel collapses.

It used to be grouped by mechanism -- one panel each for Deep Analysis, Cross-Service Analysis,
Update Checks, Lookback Window, Scheduling and Notifications. That reads sensibly as a list of
concerns and badly as a page: a single module's settings were scattered across up to six of
those panels, so configuring Updates alone meant six separate stops down a 3,525px page, and the
two panels nobody opens twice (AI keys, the login) sat at the top while the two that actually
get revisited were three and a half screens below them.

Now each module owns one panel holding everything about it, the three module panels are the same
shape as each other, what genuinely applies to all three is in General at the top, and install-
day credentials are in Setup at the bottom. Every panel starts collapsed and remembers whether
it was left open, so the page is five rows at rest instead of four and a half screens.

Browser-verified alongside these: the collapsed page is five rows and ~795px tall, clicking a
header slides it open, the state survives a reload, Enter on a focused header toggles it, and a
deep link from an Overview card opens the panel it points into and scrolls to it."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "app" / "templates" / "settings.html"
BASE = ROOT / "app" / "templates" / "base.html"
CSS = ROOT / "app" / "static" / "style.css"

MODULES = ("updates", "logs", "compose")


def _panel_bodies(text):
    """The rendered page sliced into one chunk per module panel."""
    bounds = [text.index(f'id="settings-{m}-body"') for m in MODULES]
    bounds.append(text.index('id="settings-setup-body"'))
    return {m: text[bounds[i]:bounds[i + 1]] for i, m in enumerate(MODULES)}


# ---------------------------------------------------------------------------
# The grouping
# ---------------------------------------------------------------------------

def test_the_page_is_five_panels_in_a_deliberate_order(client):
    """General first (it's what the modules below inherit from), the three modules in the app's
    usual order, then Setup -- the one panel you close after install and never reopen."""
    text = client.get("/settings").text
    # Matched on the panel headings specifically -- a bare ">Updates<" also hits the sidebar's
    # own nav link, which sits above all of this in the document.
    order = [text.index(f'settings-heading-lg">{title}</h2>') for title in
             ("General", "Updates", "Runtime Health", "Configuration Health", "Setup")]
    assert order == sorted(order)
    assert text.count('class="panel settings-panel"') == 5


def test_the_old_mechanism_panels_are_gone(client):
    """Each of these was a top-level panel; every one of them now lives inside the module it
    belongs to, or -- for the two that were genuinely shared -- inside General."""
    text = client.get("/settings").text
    for stale in (">Deep Analysis<", ">Cross-Service Analysis<", ">Update Checks<",
                  ">Scheduling<", ">Access Control</h2>", ">AI Provider</h2>"):
        assert stale not in text, f"{stale} should no longer be a panel of its own"


def test_every_module_panel_has_the_same_shape(client):
    """Structurally identical by construction -- they're generated from one loop -- so the layout
    only has to be learned once."""
    bodies = _panel_bodies(client.get("/settings").text)
    for feature, body in bodies.items():
        for heading in ("<h4>Schedule</h4>", "<h4>AI Analysis</h4>", "<h4>Notifications</h4>"):
            assert heading in body, f"{feature} is missing {heading}"


def test_a_module_panel_reads_in_the_order_you_reason_about_it(client):
    """When it runs, what it looks at, how deeply it thinks, how it tells you."""
    body = _panel_bodies(client.get("/settings").text)["logs"]
    assert (body.index("<h4>Schedule</h4>")
            < body.index("<h4>Lookback Window</h4>")
            < body.index("<h4>AI Analysis</h4>")
            < body.index("<h4>Notifications</h4>"))


def test_the_asymmetries_between_modules_are_only_the_real_ones(client):
    """Updates alone talks to registries, so it alone has retries. Configuration Health hashes
    files rather than reading a time range, so it has no lookback window, and it has no
    cross-service analysis either."""
    bodies = _panel_bodies(client.get("/settings").text)
    assert "update_retries_input" in bodies["updates"]
    assert "update_retries_input" not in bodies["logs"] + bodies["compose"]
    assert "<h4>Lookback Window</h4>" not in bodies["compose"]
    assert "cross_service_analysis_compose" not in bodies["compose"]


def test_general_holds_exactly_what_all_three_modules_share(client):
    text = client.get("/settings").text
    general = text[text.index('id="settings-general-body"'):text.index('id="settings-updates-body"')]
    assert "<h4>Timezone</h4>" in general
    assert "<h4>Default Schedule</h4>" in general
    # The master notification switch and the Apprise URL are delivery, shared by all three; each
    # module still chooses what it sends and at which level in its own panel.
    assert "<h4>Notification Delivery</h4>" in general
    assert 'id="apprise_urls_field"' in general
    assert "notify_master_enabled" in general
    assert "severity_buttons" not in general


def test_setup_holds_the_install_day_credentials_including_access_control(client):
    """Access control is offered up front in the first-launch onboarding modal, and stays here
    so the choice made there can be revisited -- or made for the first time by anyone who
    dismissed the modal."""
    text = client.get("/settings").text
    setup = text[text.index('id="settings-setup-body"'):]
    assert "<h4>AI Provider</h4>" in setup
    assert "<h4>Access Control</h4>" in setup
    assert 'id="auth_secret_field"' in setup
    assert "github_api_key_field" in setup or "github" in setup


def test_access_control_is_offered_in_onboarding_as_well_as_settings(client):
    """Two entry points to the same routes, deliberately: the modal so it isn't missed on a fresh
    install, the Settings subsection so it can be changed afterwards."""
    modal = (ROOT / "app" / "templates" / "_access_control_onboarding_modal.html").read_text()
    settings = client.get("/settings").text
    assert "/settings/access-control/credentials" in modal
    assert "/settings/access-control/credentials" in settings
    assert "/settings/access-control/lan-bypass" in modal
    assert "/settings/access-control/lan-bypass" in settings


# ---------------------------------------------------------------------------
# Collapsing
# ---------------------------------------------------------------------------

def test_every_panel_renders_collapsed(client):
    text = client.get("/settings").text
    assert text.count('class="settings-panel-header collapsible-header collapsed"') == 5
    assert text.count('class="collapse-body"') == 5


def test_collapsed_bodies_actually_clip(client):
    """.collapse-body sits at overflow:visible at rest for the feature pages' sticky-<th> sake.
    Those bodies start open so nothing needed clipping; these start at max-height:0, and without
    a rule for the shut state every collapsed panel's contents spilled down the page over the
    panels below it."""
    css = CSS.read_text()
    start = css.index(".settings-panel-header.collapsed + .collapse-body {")
    assert "overflow: hidden" in css[start:css.index("}", start)]
    assert 'style="max-height:0"' in client.get("/settings").text


def test_each_panel_carries_the_key_its_state_is_remembered_under(client):
    text = client.get("/settings").text
    for key in ("general", "updates", "logs", "compose", "setup"):
        assert f'data-collapse-key="{key}"' in text


def test_the_headers_behave_like_buttons(client):
    """Checked per header rather than by counting across the page -- the sidebar toggle and the
    accent swatch legitimately carry aria-expanded of their own."""
    text = client.get("/settings").text
    for key in ("general", "updates", "logs", "compose", "setup"):
        at = text.index(f'data-collapse-key="{key}"')
        header = text[text.rindex("<div", 0, at):text.index(">", at) + 1]
        assert 'role="button"' in header, key
        assert 'tabindex="0"' in header, key
        assert 'aria-expanded="false"' in header, key
        assert f'aria-controls="settings-{key}-body"' in header, key
    # Enter and Space have to do what a click does, since the headers are focusable.
    base = BASE.read_text()
    assert 'if (evt.key !== "Enter" && evt.key !== " ") return;' in base
    assert "evt.preventDefault(); // Space would otherwise scroll the page" in base


def test_the_saved_state_is_restored_inline_rather_than_with_the_other_scripts():
    """The panels render collapsed, so restoring any later than this -- with the rest of the
    page's scripts at the end of base.html -- means every panel left open visibly snaps shut and
    back open on each page load."""
    text = SETTINGS.read_text()
    assert "service-sentinel-settings-open" in text
    # Inline in this template, after the panels and before base.html's own script block.
    assert text.index("service-sentinel-settings-open") > text.index('id="settings-setup-body"')


def test_a_deep_link_beats_the_saved_state():
    """The Overview page's Schedule and Notifications rows link straight into a subsection of one
    of these panels. Landing on a collapsed panel would show nothing at all."""
    text = SETTINGS.read_text()
    assert "function openForHash()" in text
    assert "body.contains(target)" in text
    assert "target.scrollIntoView()" in text
    # Back/forward between two anchors on this same page is a same-document navigation, so
    # nothing above re-runs by itself.
    assert "window.addEventListener('hashchange', openForHash)" in text


def test_the_anchors_the_overview_cards_link_to_still_exist(client):
    text = client.get("/settings").text
    card = (ROOT / "app" / "templates" / "_feature_card.html").read_text()
    for feature in MODULES:
        for kind in ("schedule", "notify"):
            assert f'href="/settings#{{{{ card.feature }}}}_{kind}_section"' in card
            assert f'id="{feature}_{kind}_section"' in text


def test_the_ai_analysis_blurb_matches_how_many_toggles_the_module_has(client):
    """Configuration Health has no cross-service analysis, so "Both cost more tokens" was
    describing a second toggle that isn't there."""
    bodies = _panel_bodies(client.get("/settings").text)
    assert "Both cost noticeably more tokens" in bodies["updates"]
    assert "Both cost noticeably more tokens" in bodies["logs"]
    assert "Costs noticeably more tokens" in bodies["compose"]
    assert "Both cost" not in bodies["compose"]
