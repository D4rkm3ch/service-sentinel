"""Security hardening: no Content-Security-Policy, X-Content-Type-Options, X-Frame-Options, or
Referrer-Policy header was set anywhere (security_hardening_plan.md finding #8) -- defense in
depth for the path-traversal and unsanitized-markdown findings elsewhere in that plan, both
already fixed at the source, not a substitute for either.

The CSP deliberately still allows 'unsafe-inline' for script-src/style-src -- this app's
templates lean heavily on inline <script> blocks, onclick=/onchange= attribute handlers, and
inline style="" attributes throughout base.html and settings.html, and disallowing either
outright would break the UI's own interactivity. Verified against a live server with Playwright
(not part of this suite) that inline JS (theme toggle, accent picker) and same-origin fetch()
calls still work under this CSP. script-src used to also allowlist the htmx CDN
(cdnjs.cloudflare.com) as its one external host; htmx is vendored into /static now (see
base.html for why), so script-src is fully self-contained."""


def test_x_frame_options_is_deny(client):
    resp = client.get("/")
    assert resp.headers.get("x-frame-options") == "DENY"


def test_x_content_type_options_is_nosniff(client):
    resp = client.get("/")
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_referrer_policy_is_set(client):
    resp = client.get("/")
    assert resp.headers.get("referrer-policy") == "same-origin"


def test_content_security_policy_is_present_and_restrictive():
    from app.main import _SECURITY_HEADERS
    csp = _SECURITY_HEADERS["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp


def test_csp_script_src_allows_no_external_hosts():
    """htmx is vendored into /static (see base.html), so script-src needs no CDN allowance --
    'self' plus the documented 'unsafe-inline' exception is the complete list."""
    from app.main import _SECURITY_HEADERS
    csp = _SECURITY_HEADERS["Content-Security-Policy"]
    script_src = next(part for part in csp.split("; ") if part.startswith("script-src"))
    assert script_src == "script-src 'self' 'unsafe-inline'"


def test_htmx_is_served_from_static_not_a_cdn(client):
    """The whole UI is htmx-driven -- loading it from a CDN meant an isolated/egress-restricted
    network silently lost every button, toggle, and poll. Vendored file must exist, be served,
    and be what base.html actually references."""
    page = client.get("/")
    assert "cdnjs.cloudflare.com" not in page.text
    assert "/static/htmx.min.js" in page.text
    resp = client.get("/static/htmx.min.js")
    assert resp.status_code == 200
    assert "htmx" in resp.text[:2000]


def test_security_headers_apply_to_every_response_client(client):
    for path in ("/", "/settings", "/updates", "/logs", "/compose"):
        resp = client.get(path)
        assert resp.headers.get("x-frame-options") == "DENY", f"{path} missing X-Frame-Options"
        assert resp.headers.get("content-security-policy"), f"{path} missing CSP"


def test_security_headers_apply_even_to_a_404(client):
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    assert resp.headers.get("x-frame-options") == "DENY"
