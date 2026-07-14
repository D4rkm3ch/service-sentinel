"""Every "Check Now" button in the app (and only those -- not Reset & re-check, Regenerate AI
Response, Silence/Unsilence, or any other action button) carries the check-now-btn marker class
base.html's JS uses to decide which buttons get Cancel semantics. A quick HTTP-level guard
against a future edit accidentally adding the class to (or omitting it from) the wrong button."""


def test_updates_page_check_now_button_has_the_marker_class_but_others_do_not(client):
    page = client.get("/updates").text
    check_now_start = page.index('hx-post="/updates/check-now"')
    snippet = page[max(0, check_now_start - 200):check_now_start]
    assert "check-now-btn" in snippet

    regenerate_start = page.index('hx-post="/updates/reset-and-recheck"') if 'hx-post="/updates/reset-and-recheck"' in page else None
    reset_form = page[page.index('action="/updates/reset-and-recheck"'):page.index('action="/updates/reset-and-recheck"') + 300]
    assert "check-now-btn" not in reset_form

    regen_form = page[page.index('action="/updates/regenerate-all"'):page.index('action="/updates/regenerate-all"') + 300]
    assert "check-now-btn" not in regen_form


def test_logs_and_compose_pages_check_now_buttons_have_the_marker_class(client):
    for feature in ("logs", "compose"):
        page = client.get(f"/{feature}").text
        check_now_start = page.index(f'hx-post="/{feature}/check-now"')
        snippet = page[max(0, check_now_start - 200):check_now_start]
        assert "check-now-btn" in snippet
