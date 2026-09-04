"""A ConnectionRegistry that hands out a fake-backed writer for every name.

The real registry resolves connections.yaml and builds a live QdrantClient per
connection. Tests keep the real resolve/reload/validate path but swap the
writer for the shared :class:`FakeQdrant`-backed one, so runs still exercise
real payload-filter semantics.
"""

from config import Settings
from connections.registry import ConnectionRegistry, UnknownConnectionError
from store import QdrantWriter


class FakeConnectionRegistry(ConnectionRegistry):
    def __init__(self, settings: Settings, writer: QdrantWriter) -> None:
        super().__init__(settings)
        self._fake_writer = writer

    def writer(self, name: str) -> QdrantWriter:
        if name not in self.names():
            raise UnknownConnectionError(name)
        return self._fake_writer
