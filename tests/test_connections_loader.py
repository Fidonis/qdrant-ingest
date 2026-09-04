"""Loader-level validation and decryption of connections.yaml."""

from pathlib import Path

import yaml

from config import Settings
from connections.crypto import encrypt
from connections.loader import load_connections

SECRET = "loader-test-secret"


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "connections.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_missing_file(tmp_path: Path) -> None:
    result = load_connections(tmp_path / "nope.yaml", Settings())
    assert not result.ok
    assert result.errors[0].field == "connections_file"
    assert result.connections == []


def test_wrong_version(tmp_path: Path) -> None:
    path = _write(tmp_path, {"version": 2, "connections": []})
    result = load_connections(path, Settings())
    assert not result.ok
    assert result.errors[0].field == "version"


def test_a_valid_file_resolves(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "version": 1,
            "connections": [
                {"name": "primary", "url": "http://qdrant:6333"},
                {"name": "research", "url": "https://q.research:6333"},
            ],
        },
    )
    result = load_connections(path, Settings())
    assert result.ok, result.errors
    assert result.names() == {"primary", "research"}
    assert result.checksum is not None and len(result.checksum) == 64


def test_duplicate_names_are_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "version": 1,
            "connections": [
                {"name": "primary", "url": "http://a:6333"},
                {"name": "primary", "url": "http://b:6333"},
            ],
        },
    )
    result = load_connections(path, Settings())
    assert not result.ok
    assert any(issue.field == "name" and "duplicate" in issue.message for issue in result.errors)
    assert result.names() == set()


def test_an_encrypted_key_is_decrypted(tmp_path: Path) -> None:
    token = encrypt("qdrant-secret", SECRET)
    path = _write(
        tmp_path,
        {"version": 1, "connections": [{"name": "p", "url": "http://q:6333", "api_key": token}]},
    )
    result = load_connections(path, Settings(connections_secret=SECRET))
    assert result.ok, result.errors
    assert result.connections[0].api_key == "qdrant-secret"


def test_an_undecryptable_key_is_an_issue(tmp_path: Path) -> None:
    token = encrypt("k", SECRET)
    path = _write(
        tmp_path,
        {"version": 1, "connections": [{"name": "p", "url": "http://q:6333", "api_key": token}]},
    )
    result = load_connections(path, Settings(connections_secret="wrong"))
    assert not result.ok
    assert any(issue.field == "api_key" for issue in result.errors)
    assert result.names() == set()


def test_missing_secret_with_an_encrypted_key_is_an_issue(tmp_path: Path) -> None:
    token = encrypt("k", SECRET)
    path = _write(
        tmp_path,
        {"version": 1, "connections": [{"name": "p", "url": "http://q:6333", "api_key": token}]},
    )
    result = load_connections(path, Settings(connections_secret=""))
    assert not result.ok
    assert any(issue.field == "api_key" for issue in result.errors)


def test_a_valid_entry_survives_when_another_fails(tmp_path: Path) -> None:
    token = encrypt("k", SECRET)
    path = _write(
        tmp_path,
        {
            "version": 1,
            "connections": [
                {"name": "good", "url": "http://good:6333"},
                {"name": "bad", "url": "http://bad:6333", "api_key": token},
            ],
        },
    )
    result = load_connections(path, Settings(connections_secret="wrong"))
    assert not result.ok
    assert result.names() == {"good"}
