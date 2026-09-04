"""Encryption for the api-keys stored in ``connections.yaml``.

The keys have to live in the file: connections are created through the web
interface, and the bundle ``.env`` is mounted read-only from inside the
container, so there is nowhere else to put them. They are therefore encrypted
at rest with Fernet (AES-128-CBC + HMAC).

``QI_CONNECTIONS_SECRET`` is the only cleartext input. The Fernet key is
*derived* from it (SHA-256 → urlsafe-base64) so any sufficiently random string
works no matter what shape the deployment tooling generates it in.

Rotating ``QI_CONNECTIONS_SECRET`` invalidates every stored key -- the same
trade-off as rotating ``QI_UI_SESSION_SECRET``. Multi-key rotation
(``MultiFernet``) is a later concern.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

# Stored prefix of an encrypted value. The scheme version lets a future change
# to the cipher stay distinguishable from what is already on disk.
ENC_PREFIX = "enc:1:"


class ConnectionSecretError(Exception):
    """``QI_CONNECTIONS_SECRET`` is missing, or a stored key cannot be read with it."""


def _fernet(secret: str) -> Fernet:
    if not secret:
        raise ConnectionSecretError("QI_CONNECTIONS_SECRET is not set")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def is_encrypted(value: str) -> bool:
    return value.startswith(ENC_PREFIX)


def encrypt(plaintext: str, secret: str) -> str:
    """Return the ``enc:1:`` token for a plaintext api-key."""
    token = _fernet(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")
    return ENC_PREFIX + token


def decrypt(stored: str, secret: str) -> str:
    """Return the plaintext for an ``enc:1:`` token.

    Raises :class:`ConnectionSecretError` when the secret is unset, the value is
    not a token, or the token does not decrypt with this secret.
    """
    if not is_encrypted(stored):
        raise ConnectionSecretError("value is not an enc:1: token")
    token = stored[len(ENC_PREFIX) :]
    try:
        return _fernet(secret).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise ConnectionSecretError(
            "api-key could not be decrypted; QI_CONNECTIONS_SECRET may have changed"
        ) from exc
