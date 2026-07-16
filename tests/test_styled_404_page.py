"""A real-world report: navigating to a finding that had since been resolved (e.g. cleared by a
fresh Compose check's auto-resolution) landed on FastAPI's own bare {"detail": "..."} JSON body
instead of a page that looks like the rest of the app. A real browser 404 now renders a styled
page; htmx's own fragment requests and every non-404 status keep the exact same compact JSON
body they've always returned, since those get swapped into a small target element or inspected
programmatically, not rendered as a full page."""


def test_a_missing_finding_renders_the_styled_404_page_not_raw_json(client):
    resp = client.get("/findings/999999999")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Page not found" in resp.text
    assert "Finding not found" in resp.text
    assert "Back to Overview" in resp.text


def test_an_htmx_request_to_a_missing_route_still_gets_a_plain_json_body(client):
    """htmx's own status-poll/fragment requests must keep getting a small JSON body -- they get
    swapped into (or read out of) a specific DOM element, not rendered as a full page."""
    resp = client.get("/findings/999999999", headers={"HX-Request": "true"})
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "Finding not found"}


def test_a_non_404_error_still_gets_a_plain_json_body(client):
    resp = client.post("/settings/timezone", data={"timezone": "Not/A/Real/Zone"})
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "Unknown timezone"}


def test_updates_detail_404_also_renders_the_styled_page(client):
    resp = client.get("/updates/999999999")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Page not found" in resp.text


def test_generic_route_that_doesnt_exist_at_all_renders_the_styled_page(client):
    resp = client.get("/this-route-was-never-defined")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Page not found" in resp.text
    # Starlette's default detail for an unmatched route is "Not Found" -- the template falls
    # back to a generic message for that exact string rather than showing it verbatim.
    assert "The page you're looking for isn't available" in resp.text
