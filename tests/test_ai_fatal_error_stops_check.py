"""Integration-level companion to test_ai_error_banner.py's unit tests on
ai_provider._classify_ai_error/_record_background_ai_error: proves the fatal-cancellation those
set in motion actually stops a real concurrent-phase loop from starting further queued work, the
same "in-flight finishes, queued stops" contract the sitewide Cancel button already gets.

A real-world report: a bad key or an exhausted quota used to burn through every remaining
container in the batch, each one repeating the identical failure, before the check finally ended.
Concurrency is pinned to 1 here so items are dispatched strictly one at a time -- deterministic,
rather than racing however many workers the default concurrency would spin up at once."""

from unittest.mock import MagicMock, patch

import anthropic

from app import ai_provider, check_state, db, persist

db.init_db()


def _reset():
    check_state.set_running("updates")
    check_state.release_running("updates")


def setup_function(_):
    _reset()
    db.dismiss_last_ai_error()


def teardown_function(_):
    _reset()
    db.dismiss_last_ai_error()


def test_a_fatal_ai_failure_stops_the_concurrent_phase_before_every_item_runs():
    check_state.set_running("updates")
    try:
        containers = [{"container_name": f"c{i}"} for i in range(6)]
        calls = []

        def worker(container):
            calls.append(container["container_name"])
            # A real call into ai_provider, same as persist.py's actual summarization worker --
            # the fatal-cancellation has to happen as a side effect of THIS call failing, not
            # something the test injects separately. Caught here the same way the real worker
            # (_summarize_container) catches it -- a failed item is never fatal to the pool.map
            # loop itself, only cancellation is.
            try:
                return ai_provider.complete_text(system=None, user_message="x", max_tokens=10)
            except anthropic.AuthenticationError:
                return None

        with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls, \
             patch("app.persist.ai_provider.concurrency_limit", return_value=1):
            mock_client_cls.return_value.messages.create.side_effect = anthropic.AuthenticationError(
                "bad key", response=MagicMock(), body=None
            )
            persist._run_concurrent_phase("summarizing", containers, worker, on_progress=None)

        assert len(calls) < len(containers), (
            f"expected the fatal failure to stop the phase before every item ran, got all {len(calls)}"
        )
        # The very first (and only, at concurrency 1) call is what fails -- everything after it
        # should have been skipped, not merely "fewer than all".
        assert len(calls) == 1
    finally:
        check_state.release_running("updates")


def test_a_non_fatal_context_length_failure_does_not_stop_the_phase():
    """The same shape, but the failure this time is item-specific (a request too big for the
    model's context window) rather than provider-wide -- every item should still get its own
    attempt."""
    check_state.set_running("updates")
    try:
        containers = [{"container_name": f"c{i}"} for i in range(4)]
        calls = []

        def worker(container):
            calls.append(container["container_name"])
            try:
                return ai_provider.complete_text(system=None, user_message="x", max_tokens=10)
            except ValueError:
                return None

        with patch("app.ai_provider.anthropic.Anthropic") as mock_client_cls, \
             patch("app.persist.ai_provider.concurrency_limit", return_value=1):
            mock_client_cls.return_value.messages.create.side_effect = ValueError(
                "This model's maximum context length is 8192 tokens"
            )
            persist._run_concurrent_phase("summarizing", containers, worker, on_progress=None)

        assert len(calls) == len(containers)
    finally:
        check_state.release_running("updates")
