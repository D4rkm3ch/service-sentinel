"""Security hardening: AI provider API keys, the Apprise notification URL, the GitHub token, and
the auth gate's own shared secret were stored as plain TEXT in the app_settings SQLite table with
no encryption at rest (security_hardening_plan.md finding #7) -- anyone with read access to the
SQLite file, or a backup of it, had every configured secret in clear text.

Fixed with app.secrets_crypto: optional (SECRETS_ENCRYPTION_KEY unset means plain passthrough,
matching every existing deployment's current behavior exactly) and backward-compatible (an
"enc:v1:" prefix marks an encrypted value, so a value written before a key was ever configured
still reads back correctly as plain text -- no migration script, no forced re-save).

Tests patch app.secrets_crypto.settings.secrets_encryption_key directly rather than the process
env var, since Settings (app/config.py) is read once at import time -- this is the same pattern
already used for other config-derived test scenarios in this codebase."""

from unittest.mock import patch

import pytest

from app import db, secrets_crypto

db.init_db()

_TEST_KEY = "a-test-passphrase-not-a-real-secret"


# ---------------------------------------------------------------------------
# encrypt/decrypt round-trip, key on
# ---------------------------------------------------------------------------

def test_encrypt_then_decrypt_round_trips_with_a_key_configured():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        encrypted = secrets_crypto.encrypt("sk-ant-real-secret-value")
        assert encrypted != "sk-ant-real-secret-value"
        assert encrypted.startswith("enc:v1:")
        assert secrets_crypto.decrypt(encrypted) == "sk-ant-real-secret-value"


def test_empty_value_is_never_encrypted():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        assert secrets_crypto.encrypt("") == ""


def test_encrypted_value_is_not_the_plaintext_anywhere_in_the_stored_string():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        encrypted = secrets_crypto.encrypt("super-secret-api-key-value")
        assert "super-secret-api-key-value" not in encrypted


# ---------------------------------------------------------------------------
# No key configured -- plain passthrough, matching today's existing behavior
# ---------------------------------------------------------------------------

def test_encrypt_is_a_passthrough_when_no_key_is_configured():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", ""):
        assert secrets_crypto.encrypt("plain-value") == "plain-value"


def test_decrypt_is_a_passthrough_for_unprefixed_values_regardless_of_key():
    """A value saved before a key was ever configured (or from before this feature existed)
    must still read back correctly -- no forced migration."""
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        assert secrets_crypto.decrypt("plain-value-never-encrypted") == "plain-value-never-encrypted"


# ---------------------------------------------------------------------------
# Key removed/changed after a value was already encrypted
# ---------------------------------------------------------------------------

def test_decrypting_an_encrypted_value_with_no_key_returns_empty_not_a_crash():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        encrypted = secrets_crypto.encrypt("real-secret")
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", ""):
        assert secrets_crypto.decrypt(encrypted) == ""


def test_decrypting_with_the_wrong_key_returns_empty_not_a_crash():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        encrypted = secrets_crypto.encrypt("real-secret")
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", "a-completely-different-key"):
        assert secrets_crypto.decrypt(encrypted) == ""


def test_a_corrupted_encrypted_value_returns_empty_not_a_crash():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        assert secrets_crypto.decrypt("enc:v1:this-is-not-valid-fernet-ciphertext") == ""


# ---------------------------------------------------------------------------
# Wired into db.py's actual secret getters/setters
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_secrets():
    db.set_anthropic_api_key("")
    db.set_gemini_api_key("")
    db.set_github_token("")
    db.set_apprise_urls("")
    db.clear_auth_secret()
    yield
    db.set_anthropic_api_key("")
    db.set_gemini_api_key("")
    db.set_github_token("")
    db.set_apprise_urls("")
    db.clear_auth_secret()


def test_anthropic_key_is_encrypted_at_rest_and_reads_back_correctly():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        db.set_anthropic_api_key("sk-ant-real-value")
        raw_stored = db._get_setting("anthropic_api_key", "")
        assert raw_stored.startswith("enc:v1:")
        assert "sk-ant-real-value" not in raw_stored
        assert db.get_anthropic_api_key() == "sk-ant-real-value"


def test_gemini_key_is_encrypted_at_rest_and_reads_back_correctly():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        db.set_gemini_api_key("AIza-real-value")
        raw_stored = db._get_setting("gemini_api_key", "")
        assert raw_stored.startswith("enc:v1:")
        assert db.get_gemini_api_key() == "AIza-real-value"


def test_github_token_is_encrypted_at_rest_and_reads_back_correctly():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        db.set_github_token("ghp_real_value")
        raw_stored = db._get_setting("github_token", "")
        assert raw_stored.startswith("enc:v1:")
        assert db.get_github_token() == "ghp_real_value"


def test_apprise_urls_are_encrypted_at_rest_and_read_back_correctly():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        db.set_apprise_urls("discord://id/token,slack://token/channel")
        raw_stored = db._get_setting("notify_apprise_urls", "")
        assert raw_stored.startswith("enc:v1:")
        assert db.get_apprise_urls() == ["discord://id/token", "slack://token/channel"]


def test_auth_secret_is_encrypted_at_rest_and_reads_back_correctly():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        db.set_auth_secret("a-shared-password")
        raw_stored = db._get_setting("auth_secret", "")
        assert raw_stored.startswith("enc:v1:")
        assert db.get_auth_secret() == "a-shared-password"


def test_without_a_key_configured_secrets_are_stored_exactly_as_before():
    """No SECRETS_ENCRYPTION_KEY set -- matches every existing deployment's current, unchanged
    behavior: plain text in, plain text out."""
    db.set_anthropic_api_key("sk-ant-plain-value")
    assert db._get_setting("anthropic_api_key", "") == "sk-ant-plain-value"
    assert db.get_anthropic_api_key() == "sk-ant-plain-value"


def test_a_value_saved_before_a_key_was_configured_still_reads_back_after_one_is_set():
    """No forced migration -- an already-plaintext value keeps working once encryption is
    turned on, until it's next re-saved through the UI."""
    db.set_github_token("ghp_saved_while_unencrypted")
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        assert db.get_github_token() == "ghp_saved_while_unencrypted"
