"""Named Qdrant connections: schema, at-rest encryption, loader, registry."""

from connections.crypto import ConnectionSecretError, decrypt, encrypt, is_encrypted
from connections.loader import (
    ConnectionIssue,
    ConnectionsLoadResult,
    ResolvedConnection,
    load_connections,
)
from connections.registry import ConnectionRegistry, UnknownConnectionError
from connections.schema import ConnectionConfig

__all__ = [
    "ConnectionConfig",
    "ConnectionIssue",
    "ConnectionRegistry",
    "ConnectionSecretError",
    "ConnectionsLoadResult",
    "ResolvedConnection",
    "UnknownConnectionError",
    "decrypt",
    "encrypt",
    "is_encrypted",
    "load_connections",
]
