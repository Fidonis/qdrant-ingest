"""The connections write path: validate first, replace atomically, keep a backup."""

from pathlib import Path

import pytest

from config import Settings
from connections.writer import (
    ConnectionsWriteError,
    backup_path,
    dump_document,
    find_connection,
    load_document,
    read_raw,
    remove_connection,
    resolve_connections_location,
    upsert_connection,
    write_raw,
)

VALID = """version: 1
connections:
  - name: primary
    url: http://qdrant:6333
"""

INVALID = VALID.replace("http://qdrant:6333", "qdrant:6333")  # no scheme


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    catalog_dir = tmp_path / "config" / "catalog"
    catalog_dir.mkdir(parents=True)
    return Settings(connections_file=str(catalog_dir / "connections.yaml"))


def test_starter_document_when_absent(settings: Settings) -> None:
    location = resolve_connections_location(settings)
    assert "connections: []" in read_raw(location)


def test_valid_write_lands_and_reloads(settings: Settings) -> None:
    location = resolve_connections_location(settings)
    result = write_raw(location, VALID, settings)
    assert result.ok, result.errors
    assert Path(settings.connections_file).read_text(encoding="utf-8") == VALID


def test_invalid_write_leaves_the_file_untouched(settings: Settings) -> None:
    location = resolve_connections_location(settings)
    write_raw(location, VALID, settings)
    before = Path(settings.connections_file).read_text(encoding="utf-8")

    with pytest.raises(ConnectionsWriteError):
        write_raw(location, INVALID, settings)

    assert Path(settings.connections_file).read_text(encoding="utf-8") == before


def test_the_previous_version_is_kept_as_a_backup(settings: Settings) -> None:
    location = resolve_connections_location(settings)
    write_raw(location, VALID, settings)
    write_raw(location, VALID.replace("6333", "7333"), settings)
    assert backup_path(Path(settings.connections_file)).read_text(encoding="utf-8") == VALID


def test_a_read_only_location_refuses_the_write(tmp_path: Path) -> None:
    settings = Settings(connections_file=str(tmp_path / "missing" / "connections.yaml"))
    location = resolve_connections_location(settings)
    assert not location.writable
    with pytest.raises(ConnectionsWriteError):
        write_raw(location, VALID, settings)


def test_no_temporary_file_is_left_behind(settings: Settings) -> None:
    location = resolve_connections_location(settings)
    with pytest.raises(ConnectionsWriteError):
        write_raw(location, INVALID, settings)
    leftovers = list(Path(settings.connections_file).parent.glob(".connections.yaml*"))
    assert leftovers == []


def test_document_surgery() -> None:
    document = load_document(VALID)
    upsert_connection(document, {"name": "research", "url": "https://r:6333"})
    assert [c["name"] for c in document["connections"]] == ["primary", "research"]

    upsert_connection(document, {"name": "primary", "url": "http://new:6333"})
    assert document["connections"][0]["url"] == "http://new:6333"
    assert len(document["connections"]) == 2

    assert find_connection(document, "research")["url"] == "https://r:6333"

    remove_connection(document, "primary")
    assert [c["name"] for c in document["connections"]] == ["research"]

    # round-trips back to text
    assert "research" in dump_document(document)


def test_rename_keeps_the_position() -> None:
    document = load_document(
        "version: 1\nconnections:\n"
        "  - {name: a, url: 'http://a:6333'}\n"
        "  - {name: b, url: 'http://b:6333'}\n"
    )
    upsert_connection(document, {"name": "renamed", "url": "http://a:6333"}, original_name="a")
    assert [c["name"] for c in document["connections"]] == ["renamed", "b"]
