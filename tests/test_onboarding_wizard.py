"""First-launch setup is a three-step wizard, not a single access-control question.

It started as one modal asking whether to require a login, because that was the one decision
nothing else prompted for. But a fresh install also can't do anything at all without an AI
provider, and will quietly hit GitHub's 60-requests-an-hour anonymous ceiling on any sizeable
stack -- both of which were only discoverable by going and finding them in Settings.

So the modal now walks all three, in the order a fresh install needs them: the AI provider first
because nothing works without one, GitHub second because it only raises a rate limit, and access
control last because it's the only one that changes how the very next request is authenticated
(answering it reloads the page) and the only one that dismisses the wizard for good.

Every field posts to exactly the same route its Settings counterpart does, so there is no second
copy of any save-or-test logic to keep in step, and every step also lives in Settings >
Connections & Access -- nothing here is reachable only from a modal that shows once.

The gating itself (when the modal appears at all, and what dismisses it) is
test_access_control_onboarding.py's business, not this file's.

Browser-verified alongside these: the wizard opens on step 1, changing the provider swaps the key
hint and placeholder, picking the OpenAI-compatible option swaps in the base-URL/model fields,
saving with an empty field refuses rather than advancing, and Skip/Back move between steps
without losing what was typed."""

from pathlib import Path

from app import db

MODAL = Path(__file__).resolve().parent.parent / "app" / "templates" / "_onboarding_modal.html"
SETTINGS_TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "templates" / "settings.html"


def _modal(client):
    db.set_auth_onboarding_done(False)
    db.clear_auth_secret()
    try:
        return client.get("/settings/onboarding-modal").text
    finally:
        db.set_auth_onboarding_done(False)


def test_the_wizard_walks_ai_then_github_then_access_control(client):
    text = _modal(client)
    steps = [text.index(f'data-step="{n}"') for n in (1, 2, 3)]
    assert steps == sorted(steps)
    assert "Connect an AI provider" in text
    assert "Set up access control" in text
    assert text.index("Step 1 of 3") < text.index("Step 2 of 3") < text.index("Step 3 of 3")


def test_only_the_first_step_is_visible_on_arrival(client):
    text = _modal(client)
    assert '<div class="onboarding-step" data-step="1">' in text
    assert '<div class="onboarding-step hidden" data-step="2">' in text
    assert '<div class="onboarding-step hidden" data-step="3">' in text


def test_the_first_two_steps_can_be_skipped(client):
    """Neither is something to block a first launch on -- they're both changeable in Settings,
    and the AI key in particular may not be to hand at that moment."""
    text = _modal(client)
    assert "Skip for now" in text
    assert ">Skip<" in text
    # And you can go back, so a skipped step isn't a one-way door within the wizard either.
    assert text.count("onboardingGoTo(1)") >= 1
    assert text.count("onboardingGoTo(2)") >= 2


def test_every_step_posts_to_the_same_route_its_settings_counterpart_does(client):
    """No second copy of any save/test logic -- the modal is a different arrangement of the same
    controls, not a parallel implementation."""
    modal = _modal(client)
    settings = client.get("/settings").text
    for route in ("/settings/ai/provider", "/settings/ai/github-token",
                  "/settings/access-control/credentials", "/settings/access-control/lan-bypass"):
        assert route in modal, route
    # The per-provider key routes are named in the modal's own provider table.
    for route in ("/settings/ai/anthropic-key", "/settings/ai/gemini-key",
                  "/settings/ai/openai-key", "/settings/ai/openai-compat"):
        assert route in modal, route
        assert route in settings, route


def test_the_provider_choice_is_saved_even_when_the_key_step_is_skipped(client):
    """Otherwise picking Gemini and skipping the key would leave the app on Anthropic, and the
    Settings page would disagree with what was just chosen."""
    text = _modal(client)
    handler = text[text.index("function onboardingProviderChanged"):text.index("function onboardingSaveAiKey")]
    assert "fetch('/settings/ai/provider'" in handler


def test_all_four_providers_are_offered(client):
    text = _modal(client)
    select = text[text.index('id="onboarding_provider_select"'):text.index("</select>")]
    for value in ("anthropic", "gemini", "openai", "openai_compat"):
        assert f'value="{value}"' in select


def test_the_wizard_opens_on_the_provider_already_configured(client):
    """Not always Anthropic -- an install that got as far as picking a provider and then reloaded
    shouldn't be shown a different one pre-selected."""
    original = db.get_ai_provider()
    try:
        db.set_ai_provider("openai")
        text = _modal(client)
        select = text[text.index('id="onboarding_provider_select"'):text.index("</select>")]
        assert 'value="openai" selected' in select
        # And the local-model step's own fields are the ones hidden in that case, not shown.
        assert 'id="onboarding_compat_fields" class="hidden"' in text
    finally:
        db.set_ai_provider(original)


def test_the_local_model_option_asks_for_a_base_url_and_model_not_just_a_key(client):
    text = _modal(client)
    for field in ("onboarding_compat_base_url", "onboarding_compat_model", "onboarding_compat_key"):
        assert f'id="{field}"' in text


def test_nothing_the_wizard_offers_is_reachable_only_from_the_wizard(client):
    """It shows once and never returns, so every one of its steps has to have a permanent home."""
    settings = client.get("/settings").text
    panel = settings[settings.index('id="settings-connections-body"'):settings.index('id="settings-updates-body"')]
    assert "<h4>AI Provider</h4>" in panel
    assert "<h4>GitHub" in panel
    assert "<h4>Access Control</h4>" in panel


def test_the_route_and_template_are_no_longer_named_for_access_control_alone(client):
    """It covers three things now; a path saying otherwise would be misleading to the next
    person looking for where first-launch setup lives."""
    assert client.get("/settings/onboarding-modal").status_code == 200
    assert client.get("/settings/access-control/onboarding-modal").status_code == 404
    base = (Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html").read_text()
    assert 'hx-get="/settings/onboarding-modal"' in base
