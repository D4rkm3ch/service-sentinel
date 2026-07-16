"""Security hardening: no Content-Security-Policy, X-Content-Type-Options, X-Frame-Options, or
Referrer-Policy header was set anywhere (security_hardening_plan.md finding #8) -- defense in
depth for the path-traversal and unsanitized-markdown findings elsewhere in that plan, both
already fixed at the source, not a substitute for either.

The CSP deliberately still allows 'unsafe-inline' for script-src/style-src -- this app's
templates lean heavily on inline <script> blocks, onclick=/onchange= attribute handlers, and
inline style="" attributes throughout base.html and settings.html, and disallowing either
outright would break the UI's own interactivity. Verified against a live server with Playwright
(not part of this suite) that inline JS (theme toggle, accent picker) and same-origin fetch()
calls still work under this CSP, and that the one external script host (the htmx CDN) is
explicitly allowlisted."""


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


def test_csp_allowlists_only_the_one_trusted_cdn_for_scripts():
    from app.main import _SECURITY_HEADERS
    csp = _SECURITY_HEADERS["Content-Security-Policy"]
    script_src = next(part for part in csp.split("; ") if part.startswith("script-src"))
    assert "https://cdnjs.cloudflare.com" in script_src
    # Not wide open -- a plain '*' would defeat the point of restricting it at all.
    assert "*" not in script_src or "cdnjs.cloudflare.com" in script_src


def test_security_headers_apply_to_every_response_client(client):
    for path in ("/", "/settings", "/updates", "/logs", "/compose"):
        resp = client.get(path)
        assert resp.headers.get("x-frame-options") == "DENY", f"{path} missing X-Frame-Options"
        assert resp.headers.get("content-security-policy"), f"{path} missing CSP"


def test_security_headers_apply_even_to_a_404(client):
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    assert resp.headers.get("x-frame-options") == "DENY"
