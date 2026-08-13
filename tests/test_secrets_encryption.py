"""Secrets at rest are ALWAYS encrypted -- AI provider API keys, the Apprise notification URL,
the GitHub token, and the access-control password, all stored in the app_settings SQLite table.
Encryption used to be opt-in (SECRETS_ENCRYPTION_KEY set = encrypted, unset = plain text); a
real-world review call rejected that: if it's secret, treat it as secret, never plain text.

The key resolves in order (see app/secrets_crypto.py's own docstring): the operator-supplied
SECRETS_ENCRYPTION_KEY passphrase if set (strongest -- lives outside the data volume), otherwise
an auto-generated random key persisted at DATA_DIR/secrets.key with 0600 permissions. Values a
pre-always-on version stored as plain text are re-encrypted in place on startup
(db.ensure_secrets_encrypted(), called from init_db()).

Tests patch app.secrets_crypto.settings.secrets_encryption_key directly rather than the process
env var, since Settings (app/config.py) is read once at import time -- this is the same pattern
already used for other config-derived test scenarios in this codebase."""

import os
import stat
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app import db, secrets_crypto

db.init_db()

_TEST_KEY = "a-test-passphrase-not-a-real-secret"


# ---------------------------------------------------------------------------
# encrypt/decrypt round-trip under an operator passphrase
# ---------------------------------------------------------------------------

def test_encrypt_then_decrypt_round_trips_with_a_passphrase_configured():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        encrypted = secrets_crypto.encrypt("sk-ant-real-secret-value")
        assert encrypted != "sk-ant-real-secret-value"
        assert encrypted.startswith("enc:v1:")
        assert secrets_crypto.decrypt(encrypted) == "sk-ant-real-secret-value"


def test_empty_value_is_never_encrypted():
    assert secrets_crypto.encrypt("") == ""


def test_encrypted_value_is_not_the_plaintext_anywhere_in_the_stored_string():
    encrypted = secrets_crypto.encrypt("super-secret-api-key-value")
    assert "super-secret-api-key-value" not in encrypted


# ---------------------------------------------------------------------------
# No passphrase configured -- the auto-generated key file takes over
# ---------------------------------------------------------------------------

def test_encrypt_is_never_a_passthrough_even_with_no_passphrase_configured():
    """The old behavior (no key = plain text out) is exactly what this feature removes."""
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", ""):
        encrypted = secrets_crypto.encrypt("plain-value")
        assert encrypted != "plain-value"
        assert encrypted.startswith("enc:v1:")
        assert secrets_crypto.decrypt(encrypted) == "plain-value"


def test_auto_generated_key_file_is_created_under_data_dir_with_owner_only_permissions():
    # DATA_DIR is a fixed path (see conftest.py), not a fresh tmpdir per run, so a key file left
    # behind by an earlier test run/session sticks around on disk indefinitely -- without
    # clearing it (and the module's own cached Fernet instance, which would otherwise keep
    # serving the stale file's key from memory even after it's deleted) first, this test always
    # took the "already exists" fast path in _load_or_create_key_file and never actually
    # exercised key *creation* -- silently degrading to just "a key file happens to exist with
    # the right permissions", which would stay green even if the creation code itself broke.
    path = secrets_crypto.key_file_path()
    path.unlink(missing_ok=True)
    secrets_crypto._cache_token = None
    secrets_crypto._cached_fernet = None
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", ""):
        secrets_crypto.encrypt("anything")
        assert path.exists()
        assert path.parent == secrets_crypto.settings.data_dir
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_concurrent_key_file_creation_loses_the_race_cleanly_instead_of_crashing():
    """The O_EXCL open is there specifically so two processes racing to create the key file on
    first use don't both "win" -- one raises FileExistsError and falls back to reading whatever
    the other one just wrote, rather than crashing or silently overwriting it. The initial
    path.exists() check (line 57) can't itself observe that race (nothing's there yet when both
    processes check), so this simulates it the same way it actually happens: the file genuinely
    exists on disk when os.open(O_EXCL) runs, but Path.exists() is patched to still say False for
    the earlier check, standing in for the other process's create winning in between."""
    path = secrets_crypto.key_file_path()
    path.unlink(missing_ok=True)
    secrets_crypto._cache_token = None
    secrets_crypto._cached_fernet = None
    path.parent.mkdir(parents=True, exist_ok=True)
    winner_key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(winner_key)
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", ""), \
         patch("pathlib.Path.exists", return_value=False):
        assert secrets_crypto._load_or_create_key_file() == winner_key


def test_auto_key_round_trips_across_fresh_reads_of_the_same_key_file():
    """The file's content, not some in-memory state, is the key -- a value encrypted before a
    restart must decrypt after one. Simulated by clearing the module's Fernet cache between
    encrypt and decrypt."""
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", ""):
        encrypted = secrets_crypto.encrypt("survives-restart")
        secrets_crypto._cache_token = None
        secrets_crypto._cached_fernet = None
        assert secrets_crypto.decrypt(encrypted) == "survives-restart"


def test_decrypt_is_a_passthrough_for_unprefixed_values():
    """A plain value written by an older version must still read back correctly during the
    window between upgrade and the startup migration re-encrypting it."""
    assert secrets_crypto.decrypt("plain-value-never-encrypted") == "plain-value-never-encrypted"


# ---------------------------------------------------------------------------
# Key removed/changed after a value was already encrypted
# ---------------------------------------------------------------------------

def test_decrypting_with_the_wrong_key_returns_empty_not_a_crash():
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", _TEST_KEY):
        encrypted = secrets_crypto.encrypt("real-secret")
    with patch.object(secrets_crypto.settings, "secrets_encryption_key", "a-completely-different-key"):
        assert secrets_crypto.decrypt(encrypted) == ""


def test_a_corrupted_encrypted_value_returns_empty_not_a_crash():
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


def test_every_secret_setter_stores_ciphertext_never_plaintext():
    """The core guarantee, checked at the storage layer for all five secrets: what actually
    lands in app_settings is enc:v1: ciphertext, with the plaintext nowhere in it -- no
    passphrase configured, purely on the auto-generated key."""
    db.set_anthropic_api_key("sk-ant-real-value")
    db.set_gemini_api_key("AIza-real-value")
    db.set_github_token("ghp_real_value")
    db.set_apprise_urls("discord://id/token")
    db.set_auth_secret("a-shared-password")

    for key, plain in (
        ("anthropic_api_key", "sk-ant-real-value"),
        ("gemini_api_key", "AIza-real-value"),
        ("github_token", "ghp_real_value"),
        ("notify_apprise_urls", "discord://id/token"),
        ("auth_secret", "a-shared-password"),
    ):
        raw_stored = db._get_setting(key, "")
        assert raw_stored.startswith("enc:v1:"), f"{key} stored without encryption"
        assert plain not in raw_stored, f"{key} plaintext leaked into storage"

    assert db.get_anthropic_api_key() == "sk-ant-real-value"
    assert db.get_gemini_api_key() == "AIza-real-value"
    assert db.get_github_token() == "ghp_real_value"
    assert db.get_apprise_urls() == ["discord://id/token"]
    assert db.get_auth_secret() == "a-shared-password"


def test_startup_migration_re_encrypts_a_plaintext_value_left_by_an_older_version():
    # Simulate the pre-always-on situation: a secret sitting in app_settings as plain text.
    db._set_setting("github_token", "ghp_saved_while_unencrypted")
    assert db._get_setting("github_token", "") == "ghp_saved_while_unencrypted"

    db.ensure_secrets_encrypted()

    raw_stored = db._get_setting("github_token", "")
    assert raw_stored.startswith("enc:v1:")
    assert "ghp_saved_while_unencrypted" not in raw_stored
    assert db.get_github_token() == "ghp_saved_while_unencrypted"


def test_startup_migration_is_idempotent_and_leaves_encrypted_values_untouched():
    db.set_github_token("ghp_already_encrypted")
    stored_before = db._get_setting("github_token", "")
    db.ensure_secrets_encrypted()
    # Byte-identical: an already-encrypted value must not be re-wrapped or re-randomized.
    assert db._get_setting("github_token", "") == stored_before


def test_init_db_runs_the_migration():
    db._set_setting("gemini_api_key", "AIza-plaintext-leftover")
    db.init_db()
    assert db._get_setting("gemini_api_key", "").startswith("enc:v1:")
    assert db.get_gemini_api_key() == "AIza-plaintext-leftover"
