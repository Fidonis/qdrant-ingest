"""ConnectionRegistry: resolve, cache one writer per connection, reload."""

from pathlib import Path

import pytest
import yaml

from config import Settings
from connections.registry import ConnectionRegistry, UnknownConnectionError


def _write(path: Path, *connections: dict[str, str]) -> None:
    path.write_text(
        yaml.safe_dump({"version": 1, "connections": list(connections)}), encoding="utf-8"
    )


@pytest.fixture
def registry(tmp_path: Path) -> ConnectionRegistry:
    path = tmp_path / "connections.yaml"
    _write(path, {"name": "primary", "url": "http://qdrant:6333"})
    reg = ConnectionRegistry(Settings(connections_file=str(path)))
    reg.reload(initial=True)
    return reg


def test_names_and_get(registry: ConnectionRegistry) -> None:
    assert registry.names() == {"primary"}
    assert registry.get("primary").url == "http://qdrant:6333"
    assert registry.get("missing") is None


def test_writer_is_cached_per_connection(registry: ConnectionRegistry) -> None:
    first = registry.writer("primary")
    assert registry.writer("primary") is first


def test_unknown_connection_raises(registry: ConnectionRegistry) -> None:
    with pytest.raises(UnknownConnectionError):
        registry.writer("nope")


def test_reload_picks_up_a_new_connection_and_drops_the_cache(
    registry: ConnectionRegistry, tmp_path: Path
) -> None:
    first = registry.writer("primary")
    _write(
        tmp_path / "connections.yaml",
        {"name": "primary", "url": "http://qdrant:6333"},
        {"name": "research", "url": "http://research:6333"},
    )
    result = registry.reload()
    assert result.ok
    assert registry.names() == {"primary", "research"}
    assert registry.writer("primary") is not first  # cache was dropped


def test_a_broken_reload_keeps_the_previous_set(
    registry: ConnectionRegistry, tmp_path: Path
) -> None:
    (tmp_path / "connections.yaml").write_text("version: 1\nconnections: [\n", encoding="utf-8")
    result = registry.reload()
    assert not result.ok
    assert registry.names() == {"primary"}  # unchanged
    assert registry.error() is not None


def test_ping_all_is_true_for_an_empty_set(registry: ConnectionRegistry) -> None:
    assert registry.ping_all([]) is True


def test_config_info_shape(registry: ConnectionRegistry) -> None:
    info = registry.config_info()
    assert info["valid"] is True
    assert info["count"] == 1
    assert info["errors"] == []
