"""Security hardening: no route had any rate limit (security_hardening_plan.md finding #11).
Combined with the no-authentication finding, anyone who could reach the app could trigger checks
repeatedly -- real AI provider spend -- as fast as the server would accept connections. Fixed
with a simple per-IP sliding-window limit on check-triggering routes specifically (check-now,
reset-and-recheck, regenerate/regenerate-all, and check-all), since those are the ones with a
real cost attached.

This file deliberately re-enables RATE_LIMITING_ENABLED for its own scope (conftest.py disables
it suite-wide -- see that file's own comment for why a real wall-clock-timed window is
incompatible with the rest of the test suite calling these same routes from one shared
TestClient identity) and resets the middleware's internal bucket state before/after every test,
so this file's own repeated calls never bleed into each other or into any other test file."""

from app import db, main

db.init_db()


def setup_function(_):
    main.RATE_LIMITING_ENABLED = True
    main._rate_limit_buckets.clear()


def teardown_function(_):
    main.RATE_LIMITING_ENABLED = False
    main._rate_limit_buckets.clear()


def test_requests_under_the_limit_all_succeed(client):
    for _ in range(5):
        resp = client.post("/updates/check-now")
        assert resp.status_code != 429


def test_exceeding_the_limit_returns_429(client):
    last_status = None
    for _ in range(main._RATE_LIMIT_MAX_REQUESTS + 5):
        resp = client.post("/updates/check-now")
        last_status = resp.status_code
    assert last_status == 429


def test_429_response_includes_a_retry_after_header(client):
    for _ in range(main._RATE_LIMIT_MAX_REQUESTS):
        client.post("/updates/check-now")
    resp = client.post("/updates/check-now")
    assert resp.status_code == 429
    assert "retry-after" in {k.lower() for k in resp.headers.keys()}


def test_the_limit_is_shared_across_every_check_triggering_route_not_per_route(client):
    """A real attacker (or a buggy script) could spread requests across different
    check-triggering routes to dodge a per-route limit -- the budget must be shared. Uses the
    shared session-scoped `client` fixture rather than opening a second TestClient against the
    same app -- entering a second TestClient context manager would re-fire the startup event and
    raise SchedulerAlreadyRunningError (see conftest.py's own comment on why every test file
    must share the one fixture)."""
    routes = ["/updates/check-now", "/logs/check-now", "/compose/check-now", "/checks/check-all"]
    statuses = []
    for i in range(main._RATE_LIMIT_MAX_REQUESTS + 4):
        resp = client.post(routes[i % len(routes)])
        statuses.append(resp.status_code)
    assert 429 in statuses


def test_get_requests_to_the_same_paths_are_not_rate_limited(client):
    """Only POST is meaningful here -- there's no GET route at these paths in this app, but the
    middleware's own method check should never block a GET regardless."""
    for _ in range(main._RATE_LIMIT_MAX_REQUESTS + 5):
        resp = client.get("/updates")
    assert resp.status_code != 429


def test_ordinary_read_only_pages_are_never_rate_limited(client):
    for _ in range(main._RATE_LIMIT_MAX_REQUESTS + 5):
        resp = client.get("/settings")
    assert resp.status_code == 200


def test_is_rate_limited_path_matches_every_known_check_triggering_route():
    paths = [
        "/updates/check-now", "/checks/check-all", "/logs/check-now", "/compose/check-now",
        "/updates/reset-and-recheck", "/updates/regenerate-all", "/logs/reset-and-recheck",
        "/logs/regenerate-all", "/compose/reset-and-recheck", "/compose/regenerate-all",
        "/updates/stack/check-now", "/updates/stack/reset-and-recheck",
        "/logs/stack/check-now", "/logs/stack/reset-and-recheck",
        "/logs/container/plex/check-now", "/logs/container/plex/reset-and-recheck",
        "/logs/container/plex/regenerate",
        "/compose/file/check-now", "/compose/file/reset-and-recheck", "/compose/file/regenerate",
        "/updates/42/check-now", "/updates/42/reset-and-recheck", "/updates/42/regenerate",
    ]
    for path in paths:
        assert main._is_rate_limited_path(path), f"{path} should be rate-limited"


def test_is_rate_limited_path_does_not_match_ordinary_read_only_routes():
    for path in ("/settings", "/updates", "/logs", "/compose", "/healthz", "/updates/42"):
        assert not main._is_rate_limited_path(path), f"{path} should NOT be rate-limited"


def test_rate_limiting_disabled_flag_bypasses_the_limiter(client):
    main.RATE_LIMITING_ENABLED = False
    try:
        last_status = None
        for _ in range(main._RATE_LIMIT_MAX_REQUESTS + 10):
            resp = client.post("/updates/check-now")
            last_status = resp.status_code
        assert last_status != 429
    finally:
        main.RATE_LIMITING_ENABLED = True
