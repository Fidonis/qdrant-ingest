"""Runtime view of ``connections.yaml``: resolved connections plus one
:class:`~store.QdrantWriter` per connection.

Mirrors the per-model ``EmbeddingClient`` cache in ``main.build_engine``: a
writer is built the first time a connection is used and kept until
``connections.yaml`` changes, at which point the whole cache is dropped.
"""

import logging
import threading
from collections.abc import Iterable, Mapping

from qdrant_client import QdrantClient

from config import Settings
from connections.loader import (
    ConnectionsLoadResult,
    ResolvedConnection,
    load_connections,
)
from store import QdrantWriter

log = logging.getLogger("connections.registry")


class UnknownConnectionError(Exception):
    """A job named a connection that is not in the resolved set."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown connection '{name}'")
        self.name = name


class ConnectionRegistry:
    def __init__(self, settings: Settings, environ: Mapping[str, str] | None = None) -> None:
        self._settings = settings
        self._environ = environ
        self._lock = threading.Lock()
        self._connections: dict[str, ResolvedConnection] = {}
        self._writers: dict[tuple[str, str, str | None], QdrantWriter] = {}
        self._last_load: ConnectionsLoadResult | None = None

    # ── configuration ────────────────────────────────────────────────────────

    def reload(self, *, initial: bool = False) -> ConnectionsLoadResult:
        """Re-read the file. On a validation error keep the previous set,
        except at the first load where the valid subset is accepted."""
        result = load_connections(
            self._settings.connections_file, self._settings, self._environ
        )
        with self._lock:
            apply = result.ok or initial or not self._connections
            if apply:
                self._connections = {c.name: c for c in result.connections}
                self._writers.clear()
            self._last_load = result
        return result

    @property
    def last_load(self) -> ConnectionsLoadResult | None:
        return self._last_load

    def error(self) -> str | None:
        return self._last_load.error_summary if self._last_load else None

    def config_info(self) -> dict[str, object]:
        load = self._last_load
        return {
            "path": self._settings.connections_file,
            "checksum": load.checksum if load else None,
            "loaded_at": load.loaded_at.isoformat() if load else None,
            "valid": bool(load and load.ok),
            "count": len(self._connections),
            "errors": [
                {"name": issue.name, "field": issue.field, "message": issue.message}
                for issue in (load.errors if load else [])
            ],
        }

    # ── views ────────────────────────────────────────────────────────────────

    def names(self) -> set[str]:
        with self._lock:
            return set(self._connections)

    def get(self, name: str) -> ResolvedConnection | None:
        with self._lock:
            return self._connections.get(name)

    def all(self) -> list[ResolvedConnection]:
        with self._lock:
            return list(self._connections.values())

    # ── writers ──────────────────────────────────────────────────────────────

    def writer(self, name: str) -> QdrantWriter:
        with self._lock:
            connection = self._connections.get(name)
            if connection is None:
                raise UnknownConnectionError(name)
            key = (connection.name, connection.url, connection.api_key)
            writer = self._writers.get(key)
            if writer is None:
                client = QdrantClient(
                    url=connection.url, api_key=connection.api_key or None
                )
                writer = QdrantWriter(client, self._settings.embed_meta_collection)
                self._writers[key] = writer
            return writer

    # ── probes ───────────────────────────────────────────────────────────────

    def ping(self, name: str) -> bool:
        try:
            return self.writer(name).ping()
        except UnknownConnectionError:
            return False

    def ping_all(self, names: Iterable[str]) -> bool:
        checked = list(names)
        if not checked:
            return True
        return all(self.ping(name) for name in checked)
