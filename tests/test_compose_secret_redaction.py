"""A real-world audit found no test anywhere exercised the secret-redaction path
(SECRET_KEY_PATTERN / _redact_env / redact_compose_file_text in compose_lookup.py) -- the only
thing standing between a compose file's real secrets and an outbound AI provider call. Originally
locked in two known gaps (security_hardening_plan.md finding #4); both are now fixed:

  1. Was: only the environment block was inspected -- labels, command args, and the top-level
     secrets: block were sent to the AI provider unredacted regardless of content. Now: labels:
     gets the same key-name check as environment: (via _redact_env), and command: plus the
     top-level secrets: block get value-shape detection (via _redact_value_shapes_recursive).
  2. Was: the key-name regex only ever looked at the key, never the value -- a value like
     DATABASE_URL=postgres://user:hunter2@host/db kept its real password because "DATABASE_URL"
     doesn't match SECRET_KEY_PATTERN. Now: every value (regardless of key name) also goes
     through value-shape detection -- a connection-string password, or a long bearer/webhook-
     token-shaped run of characters -- via _redact_value_shaped_secrets.

One narrower gap remains, deliberately out of scope for finding #4's fix (see the plan): the
key-name regex is still a word-boundary heuristic, so an abbreviated key like PASSWD (as opposed
to PASSWORD) still isn't matched by name -- though a realistic PASSWD value would usually still
get caught by value-shape detection now if it's long enough to look token-shaped."""

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


def test_redact_env_catches_a_connection_string_password_under_an_innocuous_key_name():
    """Finding #4 fix: a key with an innocuous name (e.g. DB_CONNECTION_STRING, which doesn't
    match SECRET_KEY_PATTERN) whose *value* embeds a real password is now redacted via
    value-shape detection -- only the password segment, keeping the scheme/user/host intact so
    an AI compose review can still tell what the value points at."""
    redacted = _redact_env({
        "DB_CONNECTION_STRING": "postgres://user:hunter2@db:5432/app",
        "REDIS_URL": "redis://:hunter2@cache:6379",
    })
    assert redacted["DB_CONNECTION_STRING"] == "postgres://user:[REDACTED]@db:5432/app"
    assert redacted["REDIS_URL"] == "redis://:[REDACTED]@cache:6379"
    assert "hunter2" not in redacted["DB_CONNECTION_STRING"]
    assert "hunter2" not in redacted["REDIS_URL"]


def test_redact_env_catches_a_long_bearer_token_shaped_value_under_an_innocuous_key_name():
    """Finding #4 fix: a webhook/API-key-shaped value with no user:password@ shape at all --
    just a long, opaque, mixed alphanumeric run -- gets redacted too, even under a key name
    like WEBHOOK_URL that SECRET_KEY_PATTERN doesn't match."""
    token = "aBc123XyZ789qRs456TuV012"  # 24 chars, mixed letters+digits -- realistically token-shaped
    redacted = _redact_env({"WEBHOOK_URL": f"https://hooks.example.com/services/{token}"})
    assert token not in redacted["WEBHOOK_URL"]
    assert redacted["WEBHOOK_URL"] == "https://hooks.example.com/services/[REDACTED]"


def test_redact_env_value_shape_detection_leaves_ordinary_short_values_alone():
    """The value-shape heuristic requires 20+ characters -- short, ordinary-looking values under
    an innocuous key name are left untouched rather than over-redacted."""
    redacted = _redact_env({"QUEUE_NAME": "jobs", "LOG_LEVEL": "info", "PORT": "8080"})
    assert redacted == {"QUEUE_NAME": "jobs", "LOG_LEVEL": "info", "PORT": "8080"}


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


def test_redact_compose_file_text_redacts_a_secret_looking_label_key_by_name():
    """labels: now gets the same key-name check as environment: (via _redact_env)."""
    path = _write_compose_file(
        "redact-label-key-name.yml",
        "services:\n"
        "  app:\n"
        "    image: owner/app\n"
        "    labels:\n"
        "      - \"servicesentinel.auth_password=hunter2\"\n",
    )
    try:
        result = redact_compose_file_text(path)
        assert "hunter2" not in result
        assert "[REDACTED]" in result
    finally:
        path.unlink()


def test_redact_compose_file_text_redacts_a_long_token_in_a_label_value():
    """Finding #4 fix: a label value with no secret-looking key name at all, but a long
    bearer/webhook-token-shaped value, is now caught by value-shape detection."""
    token = "aBc123XyZ789qRs456TuV012"
    path = _write_compose_file(
        "redact-label-value-shape.yml",
        "services:\n"
        "  app:\n"
        "    image: owner/app\n"
        "    labels:\n"
        f"      - \"servicesentinel.webhook=https://hooks.example.com/services/{token}\"\n",
    )
    try:
        result = redact_compose_file_text(path)
        assert token not in result
        assert "[REDACTED]" in result
        assert "hooks.example.com/services" in result  # structure preserved, only the token redacted
    finally:
        path.unlink()


def test_redact_compose_file_text_redacts_a_long_token_in_a_command_arg():
    """Finding #4 fix: command: previously passed through completely raw."""
    token = "fakeTok3nVal99qRsTuVwXyZ0123AbCdEf"  # deliberately not shaped like any real vendor's key prefix
    path = _write_compose_file(
        "redact-command.yml",
        "services:\n"
        "  app:\n"
        "    image: owner/app\n"
        f"    command: [\"--api-token={token}\"]\n",
    )
    try:
        result = redact_compose_file_text(path)
        assert token not in result
        assert "[REDACTED]" in result
    finally:
        path.unlink()


def test_redact_compose_file_text_redacts_a_long_token_in_the_top_level_secrets_block():
    """Finding #4 fix: the top-level secrets: block previously passed through completely raw."""
    token = "aBc123XyZ789qRs456TuV012defGHI"
    path = _write_compose_file(
        "redact-top-level-secrets.yml",
        "services:\n"
        "  app:\n"
        "    image: owner/app\n"
        "secrets:\n"
        "  api_key:\n"
        f"    environment: {token}\n",
    )
    try:
        result = redact_compose_file_text(path)
        assert token not in result
        assert "[REDACTED]" in result
    finally:
        path.unlink()


def test_redact_compose_file_text_leaves_ordinary_command_and_labels_untouched():
    """Short, ordinary command args and label values (no secret-looking key name, no
    token-shaped value) are left exactly as written -- the point is closing the real gap, not
    over-redacting everything in these sections."""
    path = _write_compose_file(
        "redact-ordinary-command-labels.yml",
        "services:\n"
        "  app:\n"
        "    image: owner/app\n"
        "    command: [\"--verbose\", \"--port=8080\"]\n"
        "    labels:\n"
        "      - \"traefik.enable=true\"\n",
    )
    try:
        result = redact_compose_file_text(path)
        assert "--verbose" in result
        assert "8080" in result
        assert "traefik.enable" in result
        assert "true" in result
    finally:
        path.unlink()
