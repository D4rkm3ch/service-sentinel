"""Direct tests for log_filter.extract_suspicious_excerpt -- the pre-AI filter that decides
whether a container's logs cost a token at all (test_improvement_plan.md section 2: it runs on
real container log output, about as unpredictable an input as this app touches anywhere, and
was previously exercised only through log_watcher's mocks, never directly). recent_tail already
has its own dedicated file (test_log_filter_recent_tail.py)."""

from app.log_filter import CONTEXT_LINES, MAX_EXCERPT_CHARS, extract_suspicious_excerpt


def test_empty_input_returns_none():
    assert extract_suspicious_excerpt("") is None
    assert extract_suspicious_excerpt(None) is None


def test_clean_logs_return_none_the_common_case():
    """This None is what lets a clean container skip the AI call entirely -- the single most
    important behavior in this module."""
    logs = "\n".join(f"INFO handled request {i} in 12ms" for i in range(50))
    assert extract_suspicious_excerpt(logs) is None


def test_a_matching_line_is_kept_with_surrounding_context():
    lines = [f"line {i}" for i in range(10)]
    lines[5] = "ERROR: database connection lost"
    excerpt = extract_suspicious_excerpt("\n".join(lines))
    assert "ERROR: database connection lost" in excerpt
    # CONTEXT_LINES on each side come along for the ride.
    for i in range(5 - CONTEXT_LINES, 5 + CONTEXT_LINES + 1):
        assert f"line {i}" in excerpt or i == 5
    # But lines well outside the window don't.
    assert "line 0" not in excerpt
    assert "line 9" not in excerpt


def test_matching_is_case_insensitive():
    assert extract_suspicious_excerpt("Fatal: could not bind port") is not None
    assert extract_suspicious_excerpt("PANIC: runtime failure") is not None


def test_keyword_variants_match():
    for line in (
        "Connection refused by upstream",
        "request timed out after 30s",
        "request timeout after 30s",
        "worker crashed unexpectedly",
        "OOM killer invoked",
        "out of memory: killed process",
        "permission denied on /data",
        "Traceback (most recent call last):",
    ):
        assert extract_suspicious_excerpt(line) is not None, f"expected {line!r} to match"


def test_keyword_inside_a_longer_word_does_not_match():
    """\\b anchors: 'terror'/'errors' contain 'error' but only as a substring -- 'errors' DOES
    match ('error' + word-boundary 's'? no: \\berror\\b requires boundary after 'r', so 'errors'
    does NOT match). Guard the anchor behavior explicitly."""
    assert extract_suspicious_excerpt("the terrorists were discussed in the news feed") is None
    assert extract_suspicious_excerpt("mirrored volume resynced fine") is None


def test_multiple_matches_merge_their_context_windows():
    lines = [f"line {i}" for i in range(20)]
    lines[5] = "ERROR: first problem"
    lines[7] = "ERROR: second problem"
    excerpt = extract_suspicious_excerpt("\n".join(lines))
    # Overlapping windows merge -- every line from 3 through 9 present exactly once.
    assert excerpt.count("ERROR: first problem") == 1
    assert excerpt.count("ERROR: second problem") == 1
    assert "line 6" in excerpt  # between the two matches, covered by both windows


def test_oversized_excerpt_is_truncated_keeping_the_most_recent_matches():
    lines = [f"ERROR: repeated failure number {i} " + "x" * 200 for i in range(100)]
    excerpt = extract_suspicious_excerpt("\n".join(lines))
    assert len(excerpt) <= MAX_EXCERPT_CHARS + len("(truncated -- showing the most recent matches)\n")
    assert excerpt.startswith("(truncated")
    # The most recent (last) match survives; the oldest doesn't.
    assert "number 99" in excerpt
    assert "number 0 " not in excerpt
