"""Shared fixtures for the test suite."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from state import StateStore


@pytest.fixture
def state_store(tmp_path: Path) -> Iterator[StateStore]:
    store = StateStore(tmp_path / "state" / "ingest.db")
    yield store
    store.close()
