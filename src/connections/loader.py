"""Load, validate, and decrypt ``connections.yaml``.

Like :mod:`catalog.loader` this is transactional from the caller's
perspective: it never raises on bad input, it returns every problem as a named
:class:`ConnectionIssue`. The :class:`~connections.registry.ConnectionRegistry`
keeps the previous set whenever a reload produces errors.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from config import Settings
from connections.crypto import ConnectionSecretError, decrypt
from connections.schema import ConnectionConfig


@dataclass(frozen=True)
class ConnectionIssue:
    """One named validation problem, attributable to a connection and field."""

    name: str | None
    field: str
    message: str


@dataclass(frozen=True)
class ResolvedConnection:
    """A connection with its api-key decrypted, ready to build a client from."""

    name: str
    url: str
    api_key: str | None


@dataclass
class ConnectionsLoadResult:
    """Outcome of one ``connections.yaml`` load attempt."""

    path: str
    connections: list[ResolvedConnection] = field(default_factory=list)
    errors: list[ConnectionIssue] = field(default_factory=list)
    checksum: str | None = None
    loaded_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def error_summary(self) -> str | None:
        if not self.errors:
            return None
        first = self.errors[0]
        suffix = f" (+{len(self.errors) - 1} more)" if len(self.errors) > 1 else ""
        scope = f"connection '{first.name}': " if first.name else ""
        return f"{scope}{first.field}: {first.message}{suffix}"

    def names(self) -> set[str]:
        return {connection.name for connection in self.connections}


def _issue_from_validation_error(
    name: str | None, exc: ValidationError
) -> list[ConnectionIssue]:
    issues = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"]) or "<root>"
        issues.append(ConnectionIssue(name=name, field=loc, message=error["msg"]))
    return issues


def load_connections(
    path: str | Path,
    settings: Settings,
    environ: Mapping[str, str] | None = None,
) -> ConnectionsLoadResult:
    """Parse ``connections.yaml`` and return connections plus every problem."""
    file_path = Path(path)
    result = ConnectionsLoadResult(path=str(file_path))

    if not file_path.is_file():
        result.errors.append(
            ConnectionIssue(None, "connections_file", "connections.yaml not found")
        )
        return result

    try:
        raw_bytes = file_path.read_bytes()
    except OSError as exc:
        result.errors.append(
            ConnectionIssue(None, "connections_file", f"unreadable: {exc}")
        )
        return result

    result.checksum = hashlib.sha256(raw_bytes).hexdigest()

    try:
        document = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        result.errors.append(
            ConnectionIssue(None, "connections_file", f"invalid YAML: {exc}")
        )
        return result

    if document is None:
        document = {}
    if not isinstance(document, dict):
        result.errors.append(
            ConnectionIssue(None, "connections_file", "top level must be a mapping")
        )
        return result

    version = document.get("version")
    if version != 1:
        result.errors.append(
            ConnectionIssue(
                None, "version", f"unsupported connections version {version!r}; expected 1"
            )
        )
        return result

    raw_connections = document.get("connections") or []
    if not isinstance(raw_connections, list):
        result.errors.append(
            ConnectionIssue(None, "connections", "connections must be a list")
        )
        return result

    secret = settings.connections_secret
    seen: set[str] = set()
    for index, raw in enumerate(raw_connections):
        if not isinstance(raw, dict):
            result.errors.append(
                ConnectionIssue(None, f"connections[{index}]", "connection must be a mapping")
            )
            continue
        name = raw.get("name") if isinstance(raw.get("name"), str) else None
        try:
            connection = ConnectionConfig.model_validate(raw)
        except ValidationError as exc:
            result.errors.extend(
                _issue_from_validation_error(name or f"connections[{index}]", exc)
            )
            continue

        if connection.name in seen:
            result.errors.append(
                ConnectionIssue(connection.name, "name", "duplicate connection name")
            )
        seen.add(connection.name)

        api_key: str | None = None
        if connection.api_key is not None:
            try:
                api_key = decrypt(connection.api_key, secret)
            except ConnectionSecretError as exc:
                result.errors.append(ConnectionIssue(connection.name, "api_key", str(exc)))
                continue

        result.connections.append(
            ResolvedConnection(name=connection.name, url=connection.url, api_key=api_key)
        )

    if result.errors:
        # Same contract as the job catalog: a connection named in an error is
        # dropped, the rest still resolve.
        failed = {issue.name for issue in result.errors}
        result.connections = [c for c in result.connections if c.name not in failed]

    return result
