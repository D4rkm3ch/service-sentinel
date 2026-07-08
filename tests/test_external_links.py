"""render_markdown() (app/main.py) adds target="_blank" rel="noopener" to every external
(http/https) link a markdown-rendered block produces -- release notes, AI summaries/overviews,
finding descriptions and suggested fixes can all contain links the app itself didn't author.
Internal same-page anchors (the stack overview's "#row-<service>" jump links) must be left
alone since they're not supposed to open a new tab."""

from app.main import render_markdown


def test_external_link_gets_target_blank():
    html = render_markdown("See the [changelog](https://example.com/CHANGELOG.md) for details.")
    assert 'href="https://example.com/CHANGELOG.md"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener"' in html


def test_internal_anchor_link_is_left_alone():
    # Mirrors what _linkify_stack_mentions() in main.py injects into raw markdown text before
    # it reaches render_markdown() -- real inline HTML, not a markdown link.
    html = render_markdown('Update <a href="#row-sonarr">sonarr</a> and restart the stack.')
    assert '<a href="#row-sonarr">' in html
    assert 'target="_blank"' not in html


def test_multiple_external_links_all_get_the_attributes():
    text = "Use [Watchtower](https://containrrr.dev/watchtower) or read the [full CHANGELOG.md](https://github.com/owner/repo/blob/main/CHANGELOG.md)."
    html = render_markdown(text)
    assert html.count('target="_blank"') == 2
    assert html.count('rel="noopener"') == 2


def test_http_link_also_gets_the_attributes_not_just_https():
    html = render_markdown("[old mirror](http://example.com/notes)")
    assert 'target="_blank"' in html
