"""Named Qdrant connections: schema, at-rest encryption, loader, writer, registry."""

from connections.crypto import ConnectionSecretError, decrypt, encrypt, is_encrypted
from connections.loader import (
    ConnectionIssue,
    ConnectionsLoadResult,
    ResolvedConnection,
    load_connections,
)
from connections.registry import ConnectionRegistry, UnknownConnectionError, probe
from connections.schema import ConnectionConfig
from connections.writer import (
    ConnectionsLocation,
    ConnectionsWriteError,
    dump_document,
    find_connection,
    load_document,
    read_raw,
    remove_connection,
    resolve_connections_location,
    upsert_connection,
    write_raw,
)

__all__ = [
    "ConnectionConfig",
    "ConnectionIssue",
    "ConnectionRegistry",
    "ConnectionSecretError",
    "ConnectionsLoadResult",
    "ConnectionsLocation",
    "ConnectionsWriteError",
    "ResolvedConnection",
    "UnknownConnectionError",
    "decrypt",
    "dump_document",
    "encrypt",
    "find_connection",
    "is_encrypted",
    "load_connections",
    "load_document",
    "probe",
    "read_raw",
    "remove_connection",
    "resolve_connections_location",
    "upsert_connection",
    "write_raw",
]
