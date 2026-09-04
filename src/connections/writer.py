"""Where the connection list lives, and how it is changed without losing it.

``connections.yaml`` sits next to ``jobs.yaml`` in the one writable
subdirectory of the config bundle. Every write goes through :func:`write_raw`,
which validates the candidate with the same :func:`connections.loader.
load_connections` the reload path uses -- a candidate that does not load never
reaches the file.

There is no legacy-location fallback: the file is new in this release.
"""

import logging
import os
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from config import Settings
from connections.loader import ConnectionIssue, ConnectionsLoadResult, load_connections

log = logging.getLogger("connections.writer")

_TMP_NAME = ".connections.yaml.tmp"

# Serialised process-wide, for the same reason the job catalog is: the engine
# is threaded and the interface runs in the same process.
_write_lock = threading.Lock()

STARTER_DOCUMENT = """version: 1

connections: []
"""


class ConnectionsWriteError(Exception):
    """The candidate did not validate, or the file is not writable."""

    def __init__(self, issues: list[ConnectionIssue]) -> None:
        self.issues = issues
        detail = "; ".join(f"{issue.field}: {issue.message}" for issue in issues)
        super().__init__(detail or "write refused")


@dataclass(frozen=True)
class ConnectionsLocation:
    """The connections file, and whether it may be changed."""

    path: Path
    writable: bool

    @property
    def exists(self) -> bool:
        return self.path.is_file()


def _dir_writable(path: Path) -> bool:
    directory = path.parent
    return directory.is_dir() and os.access(directory, os.W_OK)


def resolve_connections_location(settings: Settings) -> ConnectionsLocation:
    path = Path(settings.connections_file)
    return ConnectionsLocation(path=path, writable=_dir_writable(path))


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def read_raw(location: ConnectionsLocation) -> str:
    """Return the file verbatim, or the starter document when absent."""
    if not location.exists:
        return STARTER_DOCUMENT
    return location.path.read_text(encoding="utf-8")


def write_raw(
    location: ConnectionsLocation,
    raw: str,
    settings: Settings,
    environ: Mapping[str, str] | None = None,
) -> ConnectionsLoadResult:
    """Validate ``raw``, then replace the file with it atomically.

    Raises :class:`ConnectionsWriteError` -- leaving the file untouched -- when
    the location is read-only or the candidate does not load. On success the
    previous contents are kept next to the file as ``connections.yaml.bak``.
    """
    if not location.writable:
        raise ConnectionsWriteError(
            [
                ConnectionIssue(
                    None,
                    "connections_file",
                    f"{location.path.parent} is not writable; connections are read-only here",
                )
            ]
        )

    tmp_path = location.path.parent / _TMP_NAME

    with _write_lock:
        try:
            tmp_path.write_text(raw, encoding="utf-8")
        except OSError as exc:
            raise ConnectionsWriteError(
                [ConnectionIssue(None, "connections_file", f"could not stage the change: {exc}")]
            ) from exc

        try:
            candidate = load_connections(tmp_path, settings, environ)
            if not candidate.ok:
                raise ConnectionsWriteError(candidate.errors)

            if location.path.is_file():
                shutil.copy2(location.path, backup_path(location.path))
            os.replace(tmp_path, location.path)
        finally:
            tmp_path.unlink(missing_ok=True)

    log.info("connections written: %d entr(y/ies) at %s", len(candidate.connections), location.path)
    return load_connections(location.path, settings, environ)


# -- document surgery -----------------------------------------------------------


def load_document(raw: str) -> dict[str, Any]:
    """Parse the file into a plain document, tolerating an empty file."""
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConnectionsWriteError(
            [ConnectionIssue(None, "connections_file", f"invalid YAML: {exc}")]
        ) from exc
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ConnectionsWriteError(
            [ConnectionIssue(None, "connections_file", "top level must be a mapping")]
        )
    document.setdefault("version", 1)
    if not isinstance(document.get("connections"), list):
        document["connections"] = []
    return document


def dump_document(document: Mapping[str, Any]) -> str:
    return yaml.safe_dump(
        dict(document), default_flow_style=False, sort_keys=False, allow_unicode=True
    )


def upsert_connection(
    document: dict[str, Any], connection: Mapping[str, Any], original_name: str | None = None
) -> dict[str, Any]:
    """Insert or replace one connection, keeping its position in the list."""
    connections: list[Any] = list(document.get("connections") or [])
    target_name = original_name or connection.get("name")
    for index, existing in enumerate(connections):
        if isinstance(existing, dict) and existing.get("name") == target_name:
            connections[index] = dict(connection)
            break
    else:
        connections.append(dict(connection))
    document["connections"] = connections
    return document


def remove_connection(document: dict[str, Any], name: str) -> dict[str, Any]:
    """Drop one connection by name. Removing an absent one is not an error."""
    document["connections"] = [
        connection
        for connection in (document.get("connections") or [])
        if not (isinstance(connection, dict) and connection.get("name") == name)
    ]
    return document


def find_connection(document: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """Return one connection's raw mapping as authored, or None."""
    for connection in document.get("connections") or []:
        if isinstance(connection, dict) and connection.get("name") == name:
            return connection
    return None
