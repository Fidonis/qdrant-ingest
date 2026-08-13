"""Shared fixtures for the test suite."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from extract import TikaClient
from state import StateStore

from fakes.tika import FakeTika


@pytest.fixture
def state_store(tmp_path: Path) -> Iterator[StateStore]:
    store = StateStore(tmp_path / "state" / "ingest.db")
    yield store
    store.close()


@pytest.fixture
def fake_tika() -> FakeTika:
    return FakeTika()


@pytest.fixture
def tika_client(fake_tika: FakeTika) -> Iterator[TikaClient]:
    client = TikaClient(
        "http://tika.test", transport=fake_tika.transport(), sleep=lambda _delay: None
    )
    yield client
    client.close()
