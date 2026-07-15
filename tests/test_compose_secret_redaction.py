"""A real-world audit found no test anywhere exercised the secret-redaction path
(SECRET_KEY_PATTERN / _redact_env / redact_compose_file_text in compose_lookup.py) -- the only
thing standing between a compose file's real secrets and an outbound AI provider call. Locks in
current behavior, including two known gaps already tracked in the security hardening plan (not
fixed here, that's a separate, not-yet-authorized pass):

  1. SECRET_KEY_PATTERN only inspects the environment block -- labels, command args, and
     anything else in a compose file is sent to the AI provider unredacted regardless of
     content.
  2. The regex itself is a word-boundary heuristic on the key name, not the value -- an
     abbreviated key like PASSWD (as opposed to PASSWORD) slips through untouched even though
     it's clearly a credential.

Both gap tests exist to document today's real behavior so a future redaction improvement has to
consciously update these tests rather than silently regress an already-narrow safety net."""

from pathlib import Path

from app.compose_lookup import SECRET_KEY_PATTERN, _redact_env, redact_compose_file_text
from app.config import settings


# ---------------------------------------------------------------------------
# SECRET_KEY_PATTERN / _redact_env
# ---------------------------------------------------------------------------

def test_redact_env_dict_redacts_matching_keys_case_insensitively():
    env = {
        "DB_PASSWORD": "hunter2", "API_TOKEN": "abc123", "SECRET_KEY": "s3cr3t",
        "MY_APIKEY": "xyz", "CREDENTIAL_FILE": "path", "db_password": "hunter2-lower",
        "PORT": "8080", "LOG_LEVEL": "info",
    }
    redacted = _redact_env(env)
    for key in ("DB_PASSWORD", "API_TOKEN", "SECRET_KEY", "MY_APIKEY", "CREDENTIAL_FILE", "db_password"):
        assert redacted[key] == "[REDACTED]"
    assert redacted["PORT"] == "8080"
    assert redacted["LOG_LEVEL"] == "info"


def test_redact_env_list_form_splits_key_value_and_redacts():
    env = ["DB_PASSWORD=hunter2", "PORT=8080", "TOKEN=abc123"]
    redacted = _redact_env(env)
    assert redacted == {"DB_PASSWORD": "[REDACTED]", "PORT": "8080", "TOKEN": "[REDACTED]"}


def test_redact_env_list_entry_without_equals_sign_has_none_value():
    """A bare 'KEY' entry (no '=value') in list form -- compose allows this to mean 'inherit
    from the host environment'. Not itself a secret leak (no value to leak), but the key name
    still goes through the same redaction check as any other."""
    redacted = _redact_env(["SOME_SECRET_TOKEN", "PORT=8080"])
    assert redacted["SOME_SECRET_TOKEN"] == "[REDACTED]"
    assert redacted["PORT"] == "8080"


def test_redact_env_non_dict_non_list_input_returns_empty_dict():
    assert _redact_env(None) == {}
    assert _redact_env("not a mapping") == {}
    assert _redact_env(42) == {}


def test_secret_key_pattern_matches_expected_key_shapes():
    for key in ("PASSWORD", "password", "DB_PASSWORD", "SECRET", "API_SECRET", "TOKEN",
                "AUTH_TOKEN", "KEY", "API_KEY", "APIKEY", "CREDENTIAL", "CREDENTIALS", "PASS"):
        assert SECRET_KEY_PATTERN.search(key), f"expected {key!r} to match SECRET_KEY_PATTERN"


def test_secret_key_pattern_known_gap_passwd_abbreviation_does_not_match():
    """Documents a known gap (see security_hardening_plan.md): the PASS alternative is
    word-boundary-anchored (PASS\\b), so an abbreviated key like PASSWD -- distinct from the
    literal word PASSWORD -- does not match and would NOT be redacted."""
    assert not SECRET_KEY_PATTERN.search("PASSWD")
    assert not SECRET_KEY_PATTERN.search("DB_PASSWD")


def test_secret_key_pattern_known_gap_value_content_is_never_inspected():
    """Documents a known gap: the pattern only ever inspects the key name. A key with an
    innocuous name (e.g. DB_CONNECTION_STRING) whose *value* embeds a real password is not
    redacted at all, since _redact_env never looks at value content."""
    redacted = _redact_env({"DB_CONNECTION_STRING": "postgres://user:hunter2@db:5432/app"})
    assert redacted["DB_CONNECTION_STRING"] == "postgres://user:hunter2@db:5432/app"


# ---------------------------------------------------------------------------
# redact_compose_file_text
# ---------------------------------------------------------------------------

def _write_compose_file(name: str, body: str) -> Path:
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def test_redact_compose_file_text_redacts_secret_env_values_across_every_service():
    path = _write_compose_file(
        "redact-multi-service.yml",
        "services:\n"
        "  app:\n"
        "    image: owner/app\n"
        "    environment:\n"
        "      DB_PASSWORD: hunter2\n"
        "      LOG_LEVEL: info\n"
        "  worker:\n"
        "    image: owner/worker\n"
        "    environment:\n"
        "      API_TOKEN: abc123\n"
        "      QUEUE_NAME: jobs\n",
    )
    try:
        result = redact_compose_file_text(path)
        assert "hunter2" not in result
        assert "abc123" not in result
        assert "[REDACTED]" in result
        assert "LOG_LEVEL: info" in result
        assert "QUEUE_NAME: jobs" in result
    finally:
        path.unlink()


def test_redact_compose_file_text_leaves_services_without_environment_untouched():
    path = _write_compose_file(
        "redact-no-env.yml",
        "services:\n  app:\n    image: owner/app\n    ports:\n      - \"8080:8080\"\n",
    )
    try:
        result = redact_compose_file_text(path)
        assert result is not None
        assert "8080" in result
    finally:
        path.unlink()


def test_redact_compose_file_text_returns_none_for_invalid_yaml():
    path = _write_compose_file("redact-invalid.yml", "services:\n  app:\n  - not: valid: yaml: [")
    try:
        assert redact_compose_file_text(path) is None
    finally:
        path.unlink()


def test_redact_compose_file_text_returns_none_when_top_level_is_not_a_services_mapping():
    path = _write_compose_file("redact-no-services-key.yml", "not_services:\n  app: {}\n")
    try:
        assert redact_compose_file_text(path) is None
    finally:
        path.unlink()

    path2 = _write_compose_file("redact-list-toplevel.yml", "- just\n- a\n- list\n")
    try:
        assert redact_compose_file_text(path2) is None
    finally:
        path2.unlink()


def test_redact_compose_file_text_returns_none_for_missing_file():
    assert redact_compose_file_text(Path(settings.compose_root) / "does-not-exist.yml") is None


def test_redact_compose_file_text_known_gap_secrets_outside_environment_block_are_not_redacted():
    """Documents a known gap (see security_hardening_plan.md): only the 'environment' block of
    each service is inspected. A secret embedded in labels, command args, or any other compose
    field is sent to the AI provider exactly as written."""
    path = _write_compose_file(
        "redact-outside-env.yml",
        "services:\n"
        "  app:\n"
        "    image: owner/app\n"
        "    command: [\"--api-token=abc123\"]\n"
        "    labels:\n"
        "      - \"traefik.http.middlewares.auth.basicauth.users=admin:hunter2\"\n",
    )
    try:
        result = redact_compose_file_text(path)
        assert "abc123" in result
        assert "hunter2" in result
    finally:
        path.unlink()
