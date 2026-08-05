"""The three monitored modules are Versions, Runtime and Configuration.

They were Updates, Runtime Health and Configuration Health, which was two problems at once.

The first: "Updates" was the only one of the three without a category noun. A bare plural reads
as a thing you DO, so it looked like a narrow "go and check for updates" action sitting beside
two names that clearly described a domain -- and it was genuinely ambiguous about whose updates,
your containers' or Service Sentinel's own. "Versions" can't be misread either way.

The second: the app carried two registers for the same three things -- short in the sidebar
(Updates/Runtime/Configuration), long everywhere else (Updates/Runtime Health/Configuration
Health). Rendering the alternatives side by side settled it: three "Health"s stacked down the
Overview read as noise rather than as a family, and the suffix wasn't earning the extra length.
One register now, used everywhere.

What deliberately did NOT change: the noun for what each module FINDS. A module and its findings
are different things, and conflating them is what made "0 Versions pending" read wrong when it
was tried -- versions aren't pending, updates are. So the copy still says "updates pending",
"updates found" and "issues", and only the module names moved.

Also unchanged: the feature keys (updates/logs/compose), the routes, the DB, and every settings
toggle. This is display text."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

NAMES = {"updates": "Versions", "logs": "Runtime", "compose": "Configuration"}
PATHS = {"updates": "/updates", "logs": "/logs", "compose": "/compose"}


def test_the_sidebar_names_all_three_modules(client):
    text = client.get("/").text
    nav = text[text.index('id="sidebar"'):text.index("</nav>")]
    for name in NAMES.values():
        assert f'<span class="sidebar-label">{name}</span>' in nav, name


def test_the_overview_cards_use_the_same_names_as_the_sidebar(client):
    """One register, so a card and the nav link beside it can't disagree."""
    text = client.get("/").text
    for name in NAMES.values():
        assert name in text, name
    assert "Runtime Health" not in text
    assert "Configuration Health" not in text


def test_each_module_page_is_headed_and_titled_by_its_own_name(client):
    for feature, name in NAMES.items():
        text = client.get(PATHS[feature]).text
        assert f'<span class="feature-title-text">{name}</span>' in text, feature
        assert f"<title>{name} - Service Sentinel</title>" in text, feature


def test_the_settings_panels_use_the_same_names(client):
    text = client.get("/settings").text
    for name in NAMES.values():
        assert f'settings-heading-lg">{name}</h2>' in text, name


def test_the_assistants_context_snapshot_uses_the_same_names():
    """The chat answers from this snapshot, so a module named one thing in the UI and another in
    the snapshot would have it talking about "Updates" while the operator is looking at
    "Versions"."""
    from app import chat
    assert dict(chat._SECTIONS) == NAMES


def test_the_old_long_names_are_gone_from_every_page(client):
    for path in ("/", "/updates", "/logs", "/compose", "/settings"):
        text = client.get(path).text
        assert "Runtime Health" not in text, path
        assert "Configuration Health" not in text, path
        assert "Updates Found" not in text, path


def test_the_finding_nouns_are_deliberately_untouched(client):
    """A module and what it finds are different things. Renaming both is what made "0 Versions
    pending" read wrong -- versions aren't pending, updates are."""
    # The check summary still counts updates and findings, not "versions".
    state = (ROOT / "app" / "check_state.py").read_text()
    assert "update{'s' if found != 1 else ''} found" in state
    assert "version" not in state.lower().replace("versions of", "")
    # And so does the topbar summary.
    source = (ROOT / "app" / "main.py").read_text()
    assert 'f"{count} Update{plural} pending"' in source
    assert "Versions pending" not in source


def test_the_rename_is_display_text_only(client):
    """The feature keys, routes and DB are untouched -- an existing install keeps every setting,
    every silence and every checkpoint across the upgrade."""
    for path in PATHS.values():
        assert client.get(path).status_code == 200
    source = (ROOT / "app" / "main.py").read_text()
    assert '_CARD_TAB_URLS = {"updates": "/updates", "logs": "/logs", "compose": "/compose"}' in source
    # Settings still keys everything off the original feature names.
    settings = client.get("/settings").text
    for feature in NAMES:
        assert f'id="{feature}_schedule_section"' in settings
        assert f'id="{feature}_notify_section"' in settings


def test_the_back_links_point_at_the_renamed_modules():
    detail = (ROOT / "app" / "templates" / "detail.html").read_text()
    stack = (ROOT / "app" / "templates" / "stack_detail.html").read_text()
    assert "← Back to Versions" in detail
    assert "← Back to Versions" in stack
    for name in ("Runtime", "Configuration"):
        assert name in (ROOT / "app" / "templates" / "finding_detail.html").read_text()
