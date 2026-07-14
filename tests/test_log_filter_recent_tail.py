"""log_filter.recent_tail -- the fallback excerpt used when a container has open findings but
its own extract_suspicious_excerpt found nothing suspicious this fetch (log_watcher.py). Without
this, a container that's gone quiet after a fix would give the AI no evidence at all to judge
"still happening or resolved?" against."""

from app.log_filter import CLEAN_TAIL_CHARS, recent_tail


def test_recent_tail_returns_none_for_empty_input():
    assert recent_tail("") is None
    assert recent_tail(None) is None


def test_recent_tail_returns_whole_text_when_under_the_cap():
    text = "line one\nline two\nall clear here\n"
    assert recent_tail(text) == text


def test_recent_tail_truncates_and_marks_long_text():
    text = "x" * (CLEAN_TAIL_CHARS * 3)
    tail = recent_tail(text)
    assert tail.startswith("(truncated")
    assert tail.endswith("x" * CLEAN_TAIL_CHARS)


def test_recent_tail_keeps_the_most_recent_content():
    text = "OLD" * 3000 + "NEWEST-LINE"
    tail = recent_tail(text)
    assert tail.endswith("NEWEST-LINE")
