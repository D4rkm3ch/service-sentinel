"""Encrypts secret values at rest -- AI provider API keys, the Apprise notification URL, the
GitHub token, and the access-control password, all stored in the app_settings SQLite table. The
UI already does the right thing on the way out (masked input, never re-serializes the real value
back to the browser), so the exposure this closes is specifically: anyone with read access to
the SQLite file, or a backup of it, could read every configured secret.

Always on. Encryption used to be opt-in (SECRETS_ENCRYPTION_KEY set = encrypted, unset = plain
text); a secret is never written as plain text anymore. The key resolves in order:

1. SECRETS_ENCRYPTION_KEY, if set -- an operator-supplied passphrase that lives OUTSIDE the
   data volume (compose env), so a stolen copy of DATA_DIR alone can't decrypt anything.
   Derived into a Fernet key via SHA-256 exactly as before, so values encrypted under a
   passphrase before this change still decrypt unchanged.
2. Otherwise, an auto-generated random key stored at DATA_DIR/secrets.key, created with 0600
   permissions on first use. Honest scope: this key sits in the same volume as the database,
   so it protects against database-file-only exposure (a copied/synced .db file, an SQL-level
   read, a tool that only grabs *.db) -- not against an attacker holding the entire volume.
   It's the never-plaintext floor; setting SECRETS_ENCRYPTION_KEY is still the stronger option.

Values stored as plain text by an older version are re-encrypted in place on startup -- see
db.ensure_secrets_encrypted(), called from init_db(). decrypt() keeps its plain-passthrough
branch for exactly that window (a value read after upgrade but before the migration ran).

Fernet (symmetric, authenticated encryption) rather than something home-rolled: it's the
standard, well-reviewed choice in the `cryptography` package for exactly this "encrypt a small
value at rest under a single key" case, and its built-in authentication tag means a corrupted or
tampered stored value is detected and rejected on read, not silently decrypted into garbage."""

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger("service_sentinel.secrets_crypto")

_PREFIX = "enc:v1:"
_KEY_FILE_NAME = "secrets.key"

# The auth gate middleware decrypts the stored password on every single request, so the Fernet
# instance is cached rather than re-derived (env path) or re-read from disk (file path) each
# call. Keyed on the inputs it was built from so tests that monkeypatch
# settings.secrets_encryption_key or settings.data_dir get a fresh instance automatically.
_cache_token: tuple[str, str] | None = None
_cached_fernet: Fernet | None = None


def key_file_path():
    return settings.data_dir / _KEY_FILE_NAME


def _load_or_create_key_file() -> bytes:
    path = key_file_path()
    if path.exists():
        return path.read_bytes().strip()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # O_EXCL + mode on the open() itself: the file is 0600 from its first byte (never a window
    # where it exists world-readable before a chmod), and a concurrent creator loses the race
    # cleanly instead of both writing different keys.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    logger.info(
        "Generated a new secrets encryption key at %s -- back this file up alongside the "
        "database; encrypted settings are unrecoverable without it.", path,
    )
    return key


def _fernet() -> Fernet:
    global _cache_token, _cached_fernet
    token = (settings.secrets_encryption_key, str(settings.data_dir))
    if _cached_fernet is not None and _cache_token == token:
        return _cached_fernet
    if settings.secrets_encryption_key:
        # Fernet requires a 32-byte, base64-urlsafe-encoded key -- derived from whatever
        # arbitrary string the operator set, so they can pick any passphrase rather than
        # needing to generate a Fernet-shaped key themselves.
        digest = hashlib.sha256(settings.secrets_encryption_key.encode("utf-8")).digest()
        fernet = Fernet(base64.urlsafe_b64encode(digest))
    else:
        fernet = Fernet(_load_or_create_key_file())
    _cache_token, _cached_fernet = token, fernet
    return fernet


def is_encrypted(stored: str) -> bool:
    return stored.startswith(_PREFIX)


def encrypt(value: str) -> str:
    """Always encrypts a non-empty value -- there is no plaintext-passthrough mode anymore."""
    if not value:
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(stored: str) -> str:
    """Values without the enc:v1: prefix are returned exactly as stored -- either empty, or a
    plain-text value written by an older version that startup migration hasn't re-encrypted
    yet. Only prefixed values attempt decryption at all."""
    if not stored or not stored.startswith(_PREFIX):
        return stored
    token = stored[len(_PREFIX):]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        # Wrong key (SECRETS_ENCRYPTION_KEY changed/removed after values were encrypted under
        # it, or a replaced secrets.key file), or a corrupted value. Logged rather than raised:
        # an undecryptable secret should read as "not configured" (the existing,
        # already-well-handled empty-string case throughout this app), not crash whichever
        # route happened to read it.
        logger.warning("Encrypted setting could not be decrypted (wrong key, or a corrupted value) -- treating as unconfigured.")
        return ""
