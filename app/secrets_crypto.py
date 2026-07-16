"""Encrypts secret values at rest (security_hardening_plan.md finding #7) -- AI provider API
keys, the Apprise notification URL, the GitHub token, and the optional auth gate's own shared
secret were previously stored as plain TEXT in the app_settings SQLite table. The UI already does
the right thing on the way out (masked input, never re-serializes the real value back to the
browser), so the actual exposure was specifically: anyone with read access to the SQLite file, or
a backup of it, had every configured secret in clear text.

Optional and backward-compatible: if SECRETS_ENCRYPTION_KEY isn't set, encrypt()/decrypt() are
plain passthroughs, matching today's existing behavior exactly -- an operator has to opt in, not
be forced into managing a key they never asked for. Encrypted values are stored with an
"enc:v1:" prefix, so a value written before a key was ever configured (or from before this
feature existed at all) still reads back correctly as plain text -- no migration script, no
forced re-save, both plain and encrypted values coexist in the same column indefinitely.

Fernet (symmetric, authenticated encryption) rather than something home-rolled: it's the
standard, well-reviewed choice in the `cryptography` package for exactly this "encrypt a small
value at rest under a single key" case, and its built-in authentication tag means a corrupted or
tampered stored value is detected and rejected on read, not silently decrypted into garbage."""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger("service_sentinel.secrets_crypto")

_PREFIX = "enc:v1:"


def _fernet() -> Fernet | None:
    key_material = settings.secrets_encryption_key
    if not key_material:
        return None
    # Fernet requires a 32-byte, base64-urlsafe-encoded key -- derived from whatever arbitrary
    # string the operator sets SECRETS_ENCRYPTION_KEY to, so they can pick any passphrase rather
    # than needing to generate a Fernet-shaped key themselves.
    digest = hashlib.sha256(key_material.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    """No-op (returns value unchanged) when no key is configured or value is already empty --
    callers never need to branch on whether encryption is actually active."""
    if not value:
        return value
    fernet = _fernet()
    if fernet is None:
        return value
    token = fernet.encrypt(value.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(stored: str) -> str:
    """Values without the enc:v1: prefix are returned exactly as stored -- either genuinely
    plain text (no key ever configured for this value) or empty. Only prefixed values attempt
    decryption at all."""
    if not stored or not stored.startswith(_PREFIX):
        return stored
    fernet = _fernet()
    if fernet is None:
        # Encrypted under a key that's since been removed or changed -- unrecoverable without
        # it. Logged rather than raised: a missing key should read as "not configured" (the
        # existing, already-well-handled empty-string case throughout this app), not crash
        # whichever route happened to read it.
        logger.warning("Encrypted setting found but SECRETS_ENCRYPTION_KEY is not set -- treating as unconfigured.")
        return ""
    token = stored[len(_PREFIX):]
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.warning("Encrypted setting could not be decrypted (wrong key, or a corrupted value) -- treating as unconfigured.")
        return ""
