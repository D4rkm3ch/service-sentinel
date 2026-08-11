"""A real-world report: sending a chat message while the AI provider was struggling meant the
whole app became unusable until that request finally gave up -- couldn't even navigate to a
different page. Root cause: chat.answer() is synchronous (the provider SDKs' blocking HTTP
clients, plus ai_provider.py's own retry/backoff using real time.sleep(), not asyncio's), and
POST /chat/send called it directly inside its `async def` body. This app serves every request
through one asyncio event loop; a blocking call made straight from an async route handler
freezes that loop for its entire duration, so nothing else -- another page load, the topbar's
once-a-second status poll, another browser tab entirely -- can be served until it returns.
Fixed by routing the call through starlette's run_in_threadpool, which runs it on a worker
thread and awaits the result without blocking the loop. The four AI-provider "Test & Save" key
routes had the identical bug (a hung/unreachable endpoint blocking everything else) and got the
same fix.

Uses the shared session-scoped `client` fixture (see conftest.py) purely to guarantee the app's
lifespan (APScheduler) has already started before these run -- entering a second TestClient(app)
in this process raises SchedulerAlreadyRunningError, so the actual requests below go through
httpx's ASGI transport directly instead, which doesn't re-run lifespan at all."""

import asyncio
import time
from unittest.mock import patch

import httpx

from app import db
from app.main import app

db.init_db()


def _configure_anthropic():
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("sk-test")


def _unconfigure():
    db.set_ai_provider("anthropic")
    db.set_anthropic_api_key("")


def test_chat_send_routes_the_blocking_call_through_run_in_threadpool(client):
    """Fast, deterministic regression check on the actual fix -- chat.answer must be reached via
    run_in_threadpool, not called directly, or the concurrency guarantee below silently regresses
    the next time this route is touched."""
    _configure_anthropic()
    try:
        with patch("app.main.run_in_threadpool") as mock_pool:
            async def fake_pool(fn, *args, **kwargs):
                return fn(*args, **kwargs)
            mock_pool.side_effect = fake_pool
            with patch("app.chat.ai_provider.complete_chat", return_value="ok"):
                resp = client.post("/chat/send", json={"history": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 200
        assert mock_pool.called
        # First positional arg to run_in_threadpool is the function being offloaded.
        offloaded = mock_pool.call_args.args[0]
        assert offloaded.__name__ == "answer"
    finally:
        _unconfigure()


def test_a_slow_chat_reply_does_not_block_a_concurrent_request(client):
    """The real proof: while chat.answer() is artificially slow, a concurrent request to another
    endpoint must still complete quickly. Before the fix this would have to wait for the slow
    call to finish first, since both were served by the same single event loop."""
    _configure_anthropic()

    def slow_answer(history):
        time.sleep(0.5)
        return "ok"

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            with patch("app.main.chat.answer", side_effect=slow_answer):
                chat_task = asyncio.ensure_future(
                    ac.post("/chat/send", json={"history": [{"role": "user", "content": "hi"}]})
                )
                # Let the chat request actually start (and hand off to the threadpool) before
                # firing the concurrent one.
                await asyncio.sleep(0.1)

                other_start = time.monotonic()
                other_resp = await ac.get("/healthz")
                other_elapsed = time.monotonic() - other_start

                chat_resp = await chat_task
        return other_resp, other_elapsed, chat_resp

    try:
        other_resp, other_elapsed, chat_resp = asyncio.run(run())
    finally:
        _unconfigure()

    assert other_resp.status_code in (200, 503)  # healthz's own docker/db reachability, not this test's concern
    assert other_elapsed < 0.35, (
        f"a concurrent request took {other_elapsed:.2f}s while chat.answer() was still running "
        "-- the event loop was blocked"
    )
    assert chat_resp.json()["ok"] is True


def test_the_ai_key_test_routes_also_route_through_run_in_threadpool(client):
    """Same bug, same fix, for the four "Test & Save" routes that ping a provider/endpoint
    live -- an unreachable one (a typo'd URL, a down local server) is exactly the slow-to-fail
    case this matters for."""
    calls = []

    def fake_test_anthropic_key(key):
        calls.append(key)
        return True, "API key works."

    try:
        with patch("app.main.run_in_threadpool") as mock_pool:
            async def fake_pool(fn, *args, **kwargs):
                return fn(*args, **kwargs)
            mock_pool.side_effect = fake_pool
            with patch("app.ai_provider.test_anthropic_key", side_effect=fake_test_anthropic_key):
                resp = client.post("/settings/ai/anthropic-key", data={"api_key": "sk-test"})

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert calls == ["sk-test"]
        assert mock_pool.called
    finally:
        db.set_anthropic_api_key("")
