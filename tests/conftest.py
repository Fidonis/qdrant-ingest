"""Shared fixtures for the test suite."""

import json
import time
from base64 import b64encode
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import itsdangerous
import pytest
import yaml
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from api.metrics import Metrics
from api.rest import create_app as create_rest_app
from catalog.schema import JobConfig
from config import Settings
from engine import JobRunner, LockingRunner
from engine.service import JobEngine
from extract import TikaClient
from mcp_app import build_mcp_app, build_mcp_server
from mcp_app.oidc import OIDCValidator
from sources.rclone import SyncResult
from state import RunRow, StateStore
from store import QdrantWriter
from ui import attach_ui
from ui.app import SESSION_COOKIE

from fakes.embeddings import FakeEmbeddings
from fakes.qdrant import FakeQdrant
from fakes.tika import FakeTika
from support import make_job

API_TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


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


@dataclass
class ApiHarness:
    """A TestClient over a fully wired JobEngine backed by the fakes."""

    client: TestClient
    engine: JobEngine
    env: EngineHarness
    settings: Settings
    jobs_path: Path
    metrics: Metrics

    def write_jobs_yaml(self, *jobs: dict[str, Any]) -> None:
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_path.write_text(
            yaml.safe_dump({"version": 1, "jobs": list(jobs)}), encoding="utf-8"
        )

    def default_job(self, **overrides: Any) -> dict[str, Any]:
        base = make_job(
            source={"type": "local", "label": "docs", "path": str(self.env.docs_dir)},
            target={"collection": "col-a", "acl_tags": ["team:qa"]},
        )
        base.update(overrides)
        return base

    def wait_run(self, run_id: str, timeout: float = 15.0) -> RunRow:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.env.state.get_run(run_id)
            if run is not None and run.status != "running":
                return run
            time.sleep(0.02)
        raise AssertionError(f"run {run_id} did not finish within {timeout}s")


@pytest.fixture
def api(engine: EngineHarness, tmp_path: Path) -> Iterator[ApiHarness]:
    jobs_path = tmp_path / "config" / "jobs.yaml"
    settings = engine.settings.model_copy(
        update={"jobs_file": str(jobs_path), "api_token": API_TOKEN}
    )
    locking = LockingRunner(engine.runner, engine.state, lock_timeout=5.0)
    metrics = Metrics()
    job_engine = JobEngine(
        settings,
        engine.state,
        engine.writer,
        locking,
        dep_probes={
            "qdrant": engine.writer.ping,
            "embeddings": lambda: True,
            "tika": lambda: True,
        },
        environ={"QI_SECRET_WEBDAV": "davpass"},
        metrics_hook=metrics.record_run,
    )
    app = create_rest_app(settings, job_engine, metrics)
    harness = ApiHarness(
        client=TestClient(app),
        engine=job_engine,
        env=engine,
        settings=settings,
        jobs_path=jobs_path,
        metrics=metrics,
    )
    yield harness
    job_engine.shutdown()


@pytest.fixture
def mcp_server(api: ApiHarness) -> FastMCP:
    """A FastMCP server bound to the same engine the API harness uses."""
    return build_mcp_server(api.engine)


@dataclass
class UiHarness:
    """A TestClient over the web interface, mounted on the real API app."""

    client: TestClient
    engine: JobEngine
    env: EngineHarness
    settings: Settings
    catalog_dir: Path
    jobs_path: Path

    def sign_session(self, payload: dict[str, Any]) -> str:
        """Mint the cookie SessionMiddleware would have written."""
        signer = itsdangerous.TimestampSigner(self.settings.ui_session_secret)
        data = b64encode(json.dumps(payload).encode("utf-8"))
        return signer.sign(data).decode("utf-8")

    def login(self, *, roles: tuple[str, ...] = ("qdrant-ingest-operator",)) -> str:
        """Put a valid session in the cookie jar and return its CSRF token."""
        csrf = "test-csrf-token"
        self.client.cookies.set(
            SESSION_COOKIE,
            self.sign_session(
                {
                    "user": {
                        "sub": "user-1",
                        "username": "tester",
                        "roles": list(roles),
                        "expires_at": int(time.time()) + 3600,
                    },
                    "csrf": csrf,
                }
            ),
        )
        return csrf

    def logout(self) -> None:
        self.client.cookies.clear()

    def write_catalog(self, text: str) -> None:
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_path.write_text(text, encoding="utf-8")


@pytest.fixture
def ui(engine: EngineHarness, tmp_path: Path) -> Iterator[UiHarness]:
    catalog_dir = tmp_path / "config" / "catalog"
    catalog_dir.mkdir(parents=True)
    jobs_path = catalog_dir / "jobs.yaml"

    settings = engine.settings.model_copy(
        update={
            "jobs_file": str(jobs_path),
            "jobs_file_legacy": str(tmp_path / "config" / "jobs.yaml"),
            "api_token": API_TOKEN,
            "oidc_issuer": "https://keycloak.test/realms/papaia",
            # http, so the session cookie is not marked Secure and the test
            # client (which speaks http) keeps sending it.
            "ui_public_url": "http://ui.test",
            "ui_client_secret": "ui-client-secret",
            "ui_session_secret": "ui-session-secret",
        }
    )
    locking = LockingRunner(engine.runner, engine.state, lock_timeout=5.0)
    metrics = Metrics()
    job_engine = JobEngine(
        settings,
        engine.state,
        engine.writer,
        locking,
        dep_probes={"qdrant": engine.writer.ping, "embeddings": lambda: True, "tika": lambda: True},
        environ={"QI_SECRET_WEBDAV": "davpass"},
        metrics_hook=metrics.record_run,
    )
    # The validators are never exercised unless a test drives the login routes
    # or presents a token, either of which would need a live identity provider.
    mcp_validator = OIDCValidator(settings.oidc_issuer, settings.oidc_audience)
    # MCP is mounted so that "a session is not a way into the other planes"
    # can be asserted against the real gate rather than against a 404.
    mcp_app = build_mcp_app(
        job_engine, mcp_validator, settings.oidc_operator_role, path=settings.mcp_path
    )
    app = create_rest_app(settings, job_engine, metrics, mcp_app)
    ui_validator = OIDCValidator(settings.oidc_issuer, settings.ui_client_id)
    attach_ui(app, settings, job_engine, ui_validator, environ={"QI_SECRET_WEBDAV": "davpass"})

    harness = UiHarness(
        client=TestClient(app),
        engine=job_engine,
        env=engine,
        settings=settings,
        catalog_dir=catalog_dir,
        jobs_path=jobs_path,
    )
    yield harness
    job_engine.shutdown()
