"""At-rest encryption for connection api-keys."""

import pytest

from connections.crypto import (
    ConnectionSecretError,
    decrypt,
    encrypt,
    is_encrypted,
)

SECRET = "a-sufficiently-random-secret"


def test_round_trip() -> None:
    token = encrypt("qdrant-key-123", SECRET)
    assert is_encrypted(token)
    assert token != "qdrant-key-123"
    assert decrypt(token, SECRET) == "qdrant-key-123"


def test_a_different_secret_cannot_read_it() -> None:
    token = encrypt("k", SECRET)
    with pytest.raises(ConnectionSecretError):
        decrypt(token, "another-secret")


def test_an_empty_secret_is_refused_both_ways() -> None:
    with pytest.raises(ConnectionSecretError):
        encrypt("k", "")
    with pytest.raises(ConnectionSecretError):
        decrypt(encrypt("k", SECRET), "")


def test_a_tampered_token_is_rejected() -> None:
    token = encrypt("k", SECRET)
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    with pytest.raises(ConnectionSecretError):
        decrypt(tampered, SECRET)


def test_a_non_token_value_is_rejected() -> None:
    with pytest.raises(ConnectionSecretError):
        decrypt("plain-text", SECRET)


def test_derivation_is_stable_across_calls() -> None:
    # Two encryptions differ (fresh IV) but both decrypt with the same secret.
    a = encrypt("k", SECRET)
    b = encrypt("k", SECRET)
    assert a != b
    assert decrypt(a, SECRET) == decrypt(b, SECRET) == "k"
