"""A real-world report: a homelab with 24 containers matching a suspicious keyword in one check
sent every excerpt to the AI as a single combined triage call (~150K characters, needing a
response covering ~20 findings at once). That call needed several truncation-retry rounds just
to squeak through in a manual reproduction, and the real Reset & Re-Check the operator ran
came back with zero findings for the entire check -- not just one container, all of them --
strongly suggesting that run hit the retry ceiling and came back unparseable, silently
discarding every finding for every container in the batch. Fixed by chunking the excerpt set
into bounded-size groups before each is sent to the AI as its own call (same "many independent
calls, not one giant fragile one" principle persist.py's Updates pipeline already uses), so a
single chunk's failure only loses that chunk's findings, not the whole check's."""

import threading
from unittest.mock import patch

from app import check_state, db, log_watcher

db.init_db()


def test_chunk_excerpts_keeps_everything_in_one_chunk_when_small():
    excerpts = {"a": "x" * 100, "b": "y" * 100}
    chunks = log_watcher._chunk_excerpts(excerpts)
    assert chunks == [excerpts]


def test_chunk_excerpts_splits_on_the_character_budget():
    big = "x" * (log_watcher._MAX_BATCH_EXCERPT_CHARS - 1000)
    excerpts = {"a": big, "b": "y" * 2000, "c": "z" * 100}
    chunks = log_watcher._chunk_excerpts(excerpts)
    assert len(chunks) == 2
    assert chunks[0] == {"a": big}
    assert chunks[1] == {"b": "y" * 2000, "c": "z" * 100}


def test_chunk_excerpts_splits_on_the_container_count_even_if_small():
    excerpts = {f"c{i}": "small" for i in range(log_watcher._MAX_BATCH_CONTAINERS + 3)}
    chunks = log_watcher._chunk_excerpts(excerpts)
    assert len(chunks) == 2
    assert len(chunks[0]) == log_watcher._MAX_BATCH_CONTAINERS
    assert len(chunks[1]) == 3


def test_chunk_excerpts_never_drops_a_single_oversized_container():
    """A container whose own excerpt alone exceeds the per-chunk budget still gets its own
    chunk -- never silently dropped, never merged awkwardly with the next one."""
    huge = "x" * (log_watcher._MAX_BATCH_EXCERPT_CHARS * 2)
    excerpts = {"huge": huge, "small": "y" * 10}
    chunks = log_watcher._chunk_excerpts(excerpts)
    assert chunks == [{"huge": huge}, {"small": "y" * 10}]


def test_a_failed_chunk_does_not_lose_findings_from_other_chunks():
    """The core regression guard: previously one combined call meant one failure lost every
    container's findings. Now a failure in one chunk must not affect another chunk's results."""
    names = [f"c{i}" for i in range(log_watcher._MAX_BATCH_CONTAINERS + 1)]  # forces 2 chunks

    def fake_analyze(chunk, include_fix=False, active_findings_by_container=None):
        if "c0" in chunk:
            raise RuntimeError("simulated truncated/unparseable response")
        return [{"container": name, "title": "Real issue", "category": "error",
                  "severity": "warning", "description": "desc"} for name in chunk]

    with patch("app.log_watcher.get_container_logs_since", return_value="ERROR: boom"), \
         patch("app.log_watcher.extract_suspicious_excerpt", side_effect=lambda text: text), \
         patch("app.log_watcher.analyze_logs_batch", side_effect=fake_analyze), \
         patch("app.log_watcher.notify_findings_digest"):
        result = log_watcher.run_log_check_for(names)

    # The chunk containing c0 failed, but the second chunk's findings must still have landed.
    assert result["findings_found"] == 1
    assert result["errors"] == 1
    from app import db
    survivors = [n for n in names if n != "c0" and db.list_findings_for_subject("logs", n, include_silenced=True)]
    assert len(survivors) == 1


def test_triage_progress_reports_across_all_chunks_not_just_one():
    names = [f"p{i}" for i in range(log_watcher._MAX_BATCH_CONTAINERS + 1)]
    progress_calls = []

    with patch("app.log_watcher.get_container_logs_since", return_value="ERROR: boom"), \
         patch("app.log_watcher.extract_suspicious_excerpt", side_effect=lambda text: text), \
         patch("app.log_watcher.analyze_logs_batch", return_value=[]), \
         patch("app.log_watcher.notify_findings_digest"):
        log_watcher.run_log_check_for(names, on_progress=lambda *args: progress_calls.append(args))

    triage_calls = [c for c in progress_calls if c[0] == "triage_logs"]
    assert triage_calls[0] == ("triage_logs", 0, 2)
    assert triage_calls[-1] == ("triage_logs", 2, 2)


def test_chunks_are_triaged_concurrently_not_one_after_another():
    """A real-world report: 'only grabbing 9 responses but it's taking close to 50-60 seconds'
    -- tracked to chunks being dispatched in a plain sequential for-loop, so total wait time was
    every chunk's own AI latency added together. Locks in that chunks now run concurrently
    (capped by ai_provider.concurrency_limit(), same shape as persist.py's Updates pipeline):
    with enough containers to force several chunks and each simulated AI call taking a fixed
    delay, total elapsed time must be well under what running them one at a time would cost.
    Pins the concurrency setting itself (so all 3 chunks fit in one batch) rather than relying
    on its production default (lowered to 2 after real-world Gemini per-minute rate-limiting),
    since this test's point is proving genuine concurrency, not exercising whatever the current
    default happens to be."""
    import time

    names = [f"conc{i}" for i in range(log_watcher._MAX_BATCH_CONTAINERS * 3)]  # forces 3 chunks
    delay = 0.3

    def slow_analyze(chunk, include_fix=False, active_findings_by_container=None):
        time.sleep(delay)
        return []

    with patch("app.log_watcher.get_container_logs_since", return_value="ERROR: boom"), \
         patch("app.log_watcher.extract_suspicious_excerpt", side_effect=lambda text: text), \
         patch("app.log_watcher.analyze_logs_batch", side_effect=slow_analyze), \
         patch("app.log_watcher.notify_findings_digest"), \
         patch("app.ai_provider.concurrency_limit", return_value=4):
        start = time.monotonic()
        log_watcher.run_log_check_for(names)
        elapsed = time.monotonic() - start

    # Sequential would take >= 3 * delay; concurrent (2+ workers) should land well under that.
    assert elapsed < delay * 3 * 0.7, f"chunks look sequential -- took {elapsed:.2f}s for 3 chunks at {delay}s each"


def test_cancel_stops_queued_chunks_but_lets_in_flight_ones_finish():
    """Same Cancel-button contract as compose's own test (see
    test_compose_check_concurrency.py) -- a chunk already picked up by a worker thread still
    gets triaged for real, but chunks still queued behind the concurrency cap are skipped once
    Cancel is clicked."""
    import time

    names = [f"cancel{i}" for i in range(log_watcher._MAX_BATCH_CONTAINERS * 6)]  # forces 6 chunks
    delay = 0.2
    call_count = 0
    call_lock = threading.Lock()

    def slow_analyze(chunk, include_fix=False, active_findings_by_container=None):
        nonlocal call_count
        with call_lock:
            call_count += 1
            is_first = call_count == 1
        if is_first:
            check_state.request_cancel("logs")
        time.sleep(delay)
        return []

    try:
        with patch("app.log_watcher.get_container_logs_since", return_value="ERROR: boom"), \
             patch("app.log_watcher.extract_suspicious_excerpt", side_effect=lambda text: text), \
             patch("app.log_watcher.analyze_logs_batch", side_effect=slow_analyze), \
             patch("app.log_watcher.notify_findings_digest"), \
             patch("app.ai_provider.concurrency_limit", return_value=2):
            result = log_watcher.run_log_check_for(names)

        assert result["cancelled"] is True
        assert call_count < 6, f"expected some chunks to be skipped, but all {call_count} got triaged"
    finally:
        check_state.set_running("logs")
        check_state.release_running("logs")
