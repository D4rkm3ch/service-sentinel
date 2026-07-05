"""Filters raw container logs down to the lines actually worth showing an AI, before any
API call happens. Most of any day's logs are routine noise — this is what keeps the log
watcher cheap regardless of how chatty a container is.
"""

import re

SUSPICIOUS_PATTERNS = re.compile(
    r"\b(error|exception|fatal|traceback|panic|denied|failed|failure|critical|"
    r"oom|out of memory|segfault|refused|unreachable|timed? ?out|crash(ed)?)\b",
    re.IGNORECASE,
)

CONTEXT_LINES = 2
MAX_EXCERPT_CHARS = 8000


def extract_suspicious_excerpt(log_text: str) -> str | None:
    """Returns a trimmed excerpt containing only lines that matched a suspicious keyword,
    each with a couple of lines of surrounding context, or None if nothing matched at all
    (the common case — this is what lets a clean container skip the AI call entirely)."""
    if not log_text:
        return None

    lines = log_text.splitlines()
    matched_indices = {i for i, line in enumerate(lines) if SUSPICIOUS_PATTERNS.search(line)}
    if not matched_indices:
        return None

    keep = set()
    for i in matched_indices:
        for j in range(max(0, i - CONTEXT_LINES), min(len(lines), i + CONTEXT_LINES + 1)):
            keep.add(j)

    excerpt_lines = [lines[i] for i in sorted(keep)]
    excerpt = "\n".join(excerpt_lines)

    if len(excerpt) > MAX_EXCERPT_CHARS:
        excerpt = excerpt[-MAX_EXCERPT_CHARS:]
        excerpt = "(truncated — showing the most recent matches)\n" + excerpt

    return excerpt
