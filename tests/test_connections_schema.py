"""Schema-level validation of a single connection definition."""

import pytest
from pydantic import ValidationError

from connections.crypto import encrypt
from connections.schema import ConnectionConfig


def test_minimal_connection_parses() -> None:
    connection = ConnectionConfig.model_validate(
        {"name": "primary", "url": "http://qdrant:6333"}
    )
    assert connection.name == "primary"
    assert connection.api_key is None


def test_name_must_be_a_slug() -> None:
    with pytest.raises(ValidationError):
        ConnectionConfig.model_validate({"name": "Not A Slug", "url": "http://q:6333"})


def test_url_needs_a_scheme() -> None:
    with pytest.raises(ValidationError, match="http"):
        ConnectionConfig.model_validate({"name": "p", "url": "qdrant:6333"})


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError, match="typo"):
        ConnectionConfig.model_validate(
            {"name": "p", "url": "http://q:6333", "typo": 1}
        )


def test_a_literal_api_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="enc:1:"):
        ConnectionConfig.model_validate(
            {"name": "p", "url": "http://q:6333", "api_key": "hunter2"}
        )


def test_an_encrypted_api_key_is_accepted() -> None:
    token = encrypt("secret", "s")
    connection = ConnectionConfig.model_validate(
        {"name": "p", "url": "http://q:6333", "api_key": token}
    )
    assert connection.api_key == token


def test_a_blank_api_key_becomes_none() -> None:
    connection = ConnectionConfig.model_validate(
        {"name": "p", "url": "http://q:6333", "api_key": ""}
    )
    assert connection.api_key is None
