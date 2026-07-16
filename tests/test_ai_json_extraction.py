"""Direct tests for ai_json.extract_json -- the last line of defense between an imperfect LLM
response and a stored finding (test_improvement_plan.md section 2 called this module out
specifically: small, pure, input-shape-sensitive, and previously exercised only incidentally
through callers' mocks, never directly). A malformed or truncated LLM response is not
hypothetical here; ai_provider.py already carries retry-on-truncation logic because it happens."""

from app.ai_json import extract_json


def test_clean_json_object_parses():
    assert extract_json('{"found": true, "notes": "x"}') == {"found": True, "notes": "x"}


def test_clean_json_array_parses():
    assert extract_json('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]


def test_markdown_json_fence_is_stripped():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_bare_markdown_fence_is_stripped():
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_leading_narration_falls_back_to_first_object():
    """The exact failure mode the docstring describes: a model adding a stray word of
    narration despite being told not to."""
    assert extract_json('Sure! Here is the JSON:\n{"a": 1}') == {"a": 1}


def test_trailing_narration_falls_back_to_object():
    assert extract_json('{"a": 1}\nLet me know if you need anything else!') == {"a": 1}


def test_narration_around_an_array_falls_back_too():
    assert extract_json('Here are the findings: [{"a": 1}] Hope that helps.') == [{"a": 1}]


def test_nested_braces_inside_the_object_survive_the_fallback():
    text = 'Noise before {"outer": {"inner": [1, 2]}} noise after'
    assert extract_json(text) == {"outer": {"inner": [1, 2]}}


def test_truncated_json_returns_none_not_a_raise():
    """A response cut off mid-object (max_tokens hit) has no complete {...} to find."""
    assert extract_json('{"found": true, "notes": "this response was cut of') is None


def test_plain_prose_with_no_json_at_all_returns_none():
    assert extract_json("I could not find any release notes for that image.") is None


def test_empty_string_returns_none():
    assert extract_json("") is None


def test_whitespace_only_returns_none():
    assert extract_json("   \n\n  ") is None
