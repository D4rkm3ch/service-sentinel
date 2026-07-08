"""render_markdown() (app/main.py) adds target="_blank" rel="noopener" to every external
(http/https) link a markdown-rendered block produces -- release notes, AI summaries/overviews,
finding descriptions and suggested fixes can all contain links the app itself didn't author.
A same-page anchor link is left alone since it's not supposed to open a new tab."""

from app.main import render_markdown


def test_external_link_gets_target_blank():
    html = render_markdown("See the [changelog](https://example.com/CHANGELOG.md) for details.")
    assert 'href="https://example.com/CHANGELOG.md"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener"' in html


def test_internal_anchor_link_is_left_alone():
    html = render_markdown('Update <a href="#some-anchor">this</a> and restart the stack.')
    assert '<a href="#some-anchor">' in html
    assert 'target="_blank"' not in html


def test_multiple_external_links_all_get_the_attributes():
    text = "Use [Watchtower](https://containrrr.dev/watchtower) or read the [full CHANGELOG.md](https://github.com/owner/repo/blob/main/CHANGELOG.md)."
    html = render_markdown(text)
    assert html.count('target="_blank"') == 2
    assert html.count('rel="noopener"') == 2


def test_http_link_also_gets_the_attributes_not_just_https():
    html = render_markdown("[old mirror](http://example.com/notes)")
    assert 'target="_blank"' in html
