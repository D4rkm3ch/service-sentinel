"""Incidental bug found and fixed alongside the Stage 6 link-in-new-tab work: the AI overview
shown above a subject's findings list (Logs/Compose detail pages) was rendered as raw,
un-processed markdown text -- **bold** and similar showed up as literal asterisks instead of
formatted HTML, unlike every other AI-authored block in the app (summaries, suggested fixes,
stack analysis), which all go through render_markdown(). Mocks _get_or_build_overview directly
since real findings/AI-generation aren't the point here -- just that whatever markdown comes
back gets rendered, not echoed raw."""

from unittest.mock import patch


def test_logs_container_overview_is_rendered_as_html_not_raw_markdown(client):
    with patch("app.main._get_or_build_overview", return_value="**Bold** and a [link](https://example.com)"):
        resp = client.get("/logs/container/some-container")

    assert resp.status_code == 200
    assert "<strong>Bold</strong>" in resp.text
    assert "**Bold**" not in resp.text
    assert 'target="_blank"' in resp.text  # render_markdown() also new-tabs external links


def test_compose_file_overview_is_rendered_as_html_not_raw_markdown(client):
    with patch("app.main._get_or_build_overview", return_value="**Bold** finding summary"):
        resp = client.get("/compose/file", params={"path": "/tmp/rr-test-compose/some/compose.yml"})

    assert resp.status_code == 200
    assert "<strong>Bold</strong>" in resp.text
    assert "**Bold**" not in resp.text
