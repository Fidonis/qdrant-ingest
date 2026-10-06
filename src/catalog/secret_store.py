"""The encrypted secret store, and the environment that consults it.

A source credential used to have exactly one home: a ``QI_SECRET_<NAME>`` variable in the
container environment. The environment is read when the container is created and the bundle
``.env`` is read-only from inside, so adding a credential meant a shell on the host and a
restart.

``secrets.yaml`` is the second home. It sits next to ``jobs.yaml`` in the catalog directory,
which is the only part of the bundle that is writable, and holds the values encrypted the way
``connections.yaml`` holds its api-keys (Fernet, the key derived from
``QI_CONNECTIONS_SECRET``; see ``connections.crypto``)::

    version: 1
    secrets:
      - name: QI_SECRET_S3_KEY
        value: enc:1:gAAAA...

The file is read when a secret is needed, not when the process starts, and a change of the file
is picked up by the same poll as a change of ``jobs.yaml``. The reference in ``jobs.yaml`` does
not change: ``${env:QI_SECRET_S3_KEY}`` is answered from the process environment first and from
this store second, so existing files keep working and nothing about the syntax is new.

What this protects. The key sits in the same ``.env`` as ``QI_API_TOKEN`` and a backup carries
both files, so the store keeps a credential out of casual sight (a copy of the file, a diff, a
screenshot, a log) and not out of reach of someone who can read both. That is the same level as
the api-keys in ``connections.yaml``.

Rotating ``QI_CONNECTIONS_SECRET`` makes every stored value unreadable, exactly as it does for the
connection keys. The store reports that as such instead of as "not set".
"""

import logging
import os
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from connections.crypto import ENC_PREFIX, ConnectionSecretError, decrypt

log = logging.getLogger("catalog.secrets")

NAME_PATTERN = r"^QI_SECRET_[A-Z0-9_]+$"
_NAME_RE = re.compile(NAME_PATTERN)


class StoredSecret(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    value: str

    @field_validator("value")
    @classmethod
    def _encrypted(cls, value: str) -> str:
        # A plaintext value in this file would be a leak waiting to be committed.
        if not value.startswith(ENC_PREFIX):
            raise ValueError(f"secret values are stored encrypted ({ENC_PREFIX}...)")
        return value


class SecretsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    secrets: list[StoredSecret] = Field(default_factory=list)

    @field_validator("secrets")
    @classmethod
    def _unique(cls, secrets: list[StoredSecret]) -> list[StoredSecret]:
        seen: set[str] = set()
        for entry in secrets:
            if entry.name in seen:
                raise ValueError(f"duplicate secret name {entry.name!r}")
            seen.add(entry.name)
        return secrets


@dataclass(frozen=True)
class _State:
    values: Mapping[str, str] = field(default_factory=dict, repr=False)
    # Stored names that have no usable value, with the reason for each.
    unreadable: Mapping[str, str] = field(default_factory=dict)
    # Why the store is not fully usable, if it is not.
    error: str | None = None
    # True when the file as a whole cannot be used (not YAML, wrong shape): then every
    # name is affected, not just the ones that are stored.
    broken: bool = False


_EMPTY = _State()


class SecretStore:
    """Reads ``secrets.yaml`` on demand and keeps the result until the file changes."""

    def __init__(self, path: str | Path, key: str) -> None:
        self._path = Path(path)
        self._key = key
        self._lock = threading.Lock()
        self._signature: tuple[int, int, int] | None = None
        self._cached: _State = _EMPTY
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            stat = self._path.stat()
        except OSError:
            return None
        # The inode is part of the signature because a writer replaces the file by
        # renaming a new one over it, which a coarse mtime could not tell apart.
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def _state(self) -> _State:
        signature = self._stat()
        with self._lock:
            if self._loaded and signature == self._signature:
                return self._cached
            state = self._read() if signature is not None else _EMPTY
            self._cached, self._signature, self._loaded = state, signature, True
            return state

    def _read(self) -> _State:
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            return _State(
                error=f"{self._path.name} cannot be read: {exc.strerror or exc}", broken=True
            )
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            return _State(
                error=f"{self._path.name} is not valid YAML: {_first_line(exc)}", broken=True
            )
        if loaded is None:
            return _EMPTY
        try:
            document = SecretsDocument.model_validate(loaded)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "<root>"
            return _State(error=f"{self._path.name}: {where}: {first['msg']}", broken=True)
        if not document.secrets:
            return _EMPTY
        if not self._key:
            reason = "cannot be read: QI_CONNECTIONS_SECRET is not set"
            return _State(
                unreadable={entry.name: reason for entry in document.secrets},
                error="QI_CONNECTIONS_SECRET is not set, so the stored secrets cannot be read",
            )
        values: dict[str, str] = {}
        unreadable: dict[str, str] = {}
        for entry in document.secrets:
            try:
                values[entry.name] = decrypt(entry.value, self._key)
            except ConnectionSecretError:
                unreadable[entry.name] = (
                    "cannot be decrypted; QI_CONNECTIONS_SECRET may have changed since "
                    "it was stored"
                )
        error = None
        if unreadable:
            error = (
                f"{len(unreadable)} stored secret(s) cannot be decrypted; "
                "QI_CONNECTIONS_SECRET may have changed"
            )
        return _State(values=values, unreadable=unreadable, error=error)

    # -- reading ---------------------------------------------------------------

    def get(self, name: str) -> str | None:
        return self._state().values.get(name)

    def names(self) -> frozenset[str]:
        """The names that have a usable value."""
        return frozenset(self._state().values)

    def problem(self) -> str | None:
        """Why the store as a whole is not fully usable, or None."""
        return self._state().error

    def why_unavailable(self, name: str) -> str | None:
        """What is wrong with a name that has no usable value, or None if nothing is known."""
        state = self._state()
        if name in state.unreadable:
            return f"the stored secret '{name}' {state.unreadable[name]}"
        if state.broken and name not in state.values and _NAME_RE.match(name):
            return f"'{name}' is not set, and the secret store is unusable: {state.error}"
        return None


class LayeredEnviron(Mapping[str, str]):
    """The process environment first, the secret store second.

    It is a read-only ``Mapping`` so everything that takes an ``environ`` (the loader, the
    rclone configuration, the web interface's list of names) takes it unchanged. Values are
    looked up on use and never copied, so a secret added to the store is visible to the next
    lookup without a restart.
    """

    def __init__(self, base: Mapping[str, str], store: SecretStore) -> None:
        self._base = base
        self._store = store

    def __getitem__(self, key: str) -> str:
        value = self._base.get(key)
        if value:
            return value
        stored = self._store.get(key)
        if stored is not None:
            return stored
        if value is not None:
            return value  # set, but empty: the caller decides what an empty value means
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield from self._base
        for name in sorted(self._store.names()):
            if name not in self._base:
                yield name

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __repr__(self) -> str:
        return "LayeredEnviron(<environment + secret store>)"

    def why_unavailable(self, name: str) -> str | None:
        return self._store.why_unavailable(name)


def default_environ(store: SecretStore) -> LayeredEnviron:
    """The environment a running ingester resolves ``${env:QI_SECRET_...}`` against."""
    return LayeredEnviron(os.environ, store)


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else "invalid YAML"
