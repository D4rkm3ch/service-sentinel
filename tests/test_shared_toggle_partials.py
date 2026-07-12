"""Every Mark as Read/Unread and Silence/Unsilence button in the app renders from ONE shared
partial each (_read_toggle.html / _silence_toggle.html, with _read_badge.html /
_silence_badge.html for the title-row badges and the *_response.html wrappers for the
post-toggle htmx swap). Previously each scope -- Updates detail, finding detail, Logs stack,
Logs/Compose subject pages -- carried its own inline copy of the markup in its page template
PLUS a matching response partial, so the same button existed in up to eight places that had to
be kept in sync by hand. These tests pin the consolidation: the scope-specific copies stay
deleted, the pages actually include the shared partials, and every scope's toggle POST still
responds with its own correct endpoints."""

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"

SHARED = {
    "_read_toggle.html", "_read_badge.html", "_read_toggle_response.html",
    "_silence_toggle.html", "_silence_badge.html", "_silence_toggle_response.html",
}


def test_scope_specific_toggle_partial_copies_stay_deleted():
    leftovers = [
        p.name for p in TEMPLATES.glob("*.html")
        if ("read_toggle" in p.name or "silence_toggle" in p.name) and p.name not in SHARED
    ]
    assert leftovers == [], f"scope-specific toggle partials crept back in: {leftovers}"


def test_pages_include_the_shared_partials_instead_of_inlining_button_markup():
    """The one-source-of-truth rule: no page template may carry its own copy of the toggle
    button markup -- the hx-post'ing button-info/button-silence buttons must only exist in the
    shared partials. (Pages still have plain buttons like Check Now; this only checks the two
    toggle button classes.)"""
    for page in TEMPLATES.glob("*.html"):
        if page.name in SHARED or page.name.startswith("_"):
            continue
        text = page.read_text()
        for marker in ('class="button-info"', 'class="button-silence"'):
            assert marker not in text, (
                f"{page.name} inlines its own toggle button markup ({marker}) instead of "
                f"including the shared partial"
            )


def test_every_page_with_a_toggle_includes_the_shared_partials():
    expectations = {
        "detail.html": ["_read_toggle.html", "_silence_toggle.html", "_read_badge.html", "_silence_badge.html"],
        "finding_detail.html": ["_read_toggle.html", "_silence_toggle.html", "_read_badge.html", "_silence_badge.html"],
        "subject_findings.html": ["_read_toggle.html", "_silence_toggle.html", "_read_badge.html", "_silence_badge.html"],
        "logs_stack_detail.html": ["_silence_toggle.html", "_silence_badge.html"],
    }
    for page, partials in expectations.items():
        text = (TEMPLATES / page).read_text()
        for partial in partials:
            assert f'include "{partial}"' in text, f"{page} no longer includes {partial}"
