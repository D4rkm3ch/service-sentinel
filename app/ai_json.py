import json


def extract_json(text: str):
    """Strips markdown code fences if present and parses the remaining text as JSON.
    Returns None if it still isn't valid JSON, rather than raising — callers should treat
    that as 'the model didn't give us usable structure' and fail gracefully."""
    cleaned = text.strip()
    cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None
