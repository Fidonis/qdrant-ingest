"""Shared fixtures for the test suite."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from catalog.schema import JobConfig
from config import Settings
from engine import JobRunner
from extract import TikaClient
from sources.rclone import SyncResult
from state import StateStore
from store import QdrantWriter

from fakes.embeddings import FakeEmbeddings
from fakes.qdrant import FakeQdrant
from fakes.tika import FakeTika
from support import make_job


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


@dataclass
class EngineHarness:
    """Everything one engine test needs, wired against the fakes."""

    settings: Settings
    state: StateStore
    qdrant: FakeQdrant
    writer: QdrantWriter
    embeddings: FakeEmbeddings
    tika: FakeTika
    runner: JobRunner
    docs_dir: Path

    def write_doc(self, rel: str, content: str) -> Path:
        path = self.docs_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def local_job(self, **overrides: Any) -> JobConfig:
        base = make_job(
            source={"type": "local", "label": "docs", "path": str(self.docs_dir)},
            target={
                "collection": "col-a",
                "acl_tags": ["team:qa"],
                "extra_payload": {"origin": "test"},
            },
        )
        base.update(overrides)
        return JobConfig.model_validate(base)

    def payloads(self, collection: str = "col-a") -> list[dict[str, Any]]:
        return self.qdrant.payloads(collection)

    def sources_in_qdrant(self, collection: str = "col-a") -> set[str]:
        return {payload["source"] for payload in self.payloads(collection)}


@pytest.fixture
def engine(tmp_path: Path, fake_tika: FakeTika) -> Iterator[EngineHarness]:
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        cache_dir=str(tmp_path / "cache"),
        local_dir=str(tmp_path / "local"),
    )
    state = StateStore(Path(settings.state_dir) / "ingest.db")
    fake_qdrant = FakeQdrant()
    writer = QdrantWriter(
        client=fake_qdrant,  # type: ignore[arg-type]
        meta_collection=settings.embed_meta_collection,
    )
    embeddings = FakeEmbeddings()
    tika_client = TikaClient(
        "http://tika.test", transport=fake_tika.transport(), sleep=lambda _delay: None
    )
    runner = JobRunner(
        settings,
        state,
        writer,
        tika_client,
        embedder_factory=lambda _model: embeddings,
        sync_fn=lambda _job, _settings: SyncResult(
            ok=True, returncode=0, stderr_tail="", duration_seconds=0.0
        ),
    )
    docs_dir = tmp_path / "local" / "docs"
    docs_dir.mkdir(parents=True)
    harness = EngineHarness(
        settings=settings,
        state=state,
        qdrant=fake_qdrant,
        writer=writer,
        embeddings=embeddings,
        tika=fake_tika,
        runner=runner,
        docs_dir=docs_dir,
    )
    yield harness
    state.close()
    tika_client.close()
