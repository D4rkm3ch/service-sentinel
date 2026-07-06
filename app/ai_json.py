import json
import re


def extract_json(text: str):
    """Strips markdown code fences if present and parses the remaining text as JSON.
    Falls back to locating the first complete {...} or [...] substring if the text doesn't
    parse cleanly on its own — models occasionally add a stray word of narration despite
    being told not to. Returns None if nothing usable can be found, rather than raising —
    callers should treat that as 'the model didn't give us usable structure' and fail
    gracefully."""
    cleaned = text.strip()
    cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None
