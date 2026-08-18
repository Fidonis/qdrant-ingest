"""Service entry point: wire the settings into the engine and serve the app."""

import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from qdrant_client import QdrantClient

from api.metrics import Metrics
from api.rest import create_app as create_rest_app
from config import Settings
from embed import EmbeddingClient, EmbeddingLimiter, LimitedEmbedder
from engine import JobRunner, LockingRunner
from engine.service import JobEngine
from extract import TikaClient
from mcp_app import OIDCValidator, build_mcp_app
from state import StateStore
from store import QdrantWriter

log = logging.getLogger("main")


def build_engine(settings: Settings, metrics: Metrics) -> JobEngine:
    state = StateStore(Path(settings.state_dir) / "ingest.db")
    qdrant_client = QdrantClient(
        url=settings.qdrant_url, api_key=settings.qdrant_api_key or None
    )
    writer = QdrantWriter(qdrant_client, settings.embed_meta_collection)
    tika = TikaClient(
        settings.tika_url,
        timeout=settings.tika_timeout,
        ocr_language=settings.tika_ocr_language,
        pdf_ocr_strategy=settings.tika_pdf_ocr_strategy,
    )

    # One shared limiter: the embeddings endpoint is a global bottleneck no
    # matter how many jobs run concurrently.
    limiter = EmbeddingLimiter(settings.embed_concurrency, settings.embed_rps)
    clients: dict[str, EmbeddingClient] = {}

    def raw_client(model: str) -> EmbeddingClient:
        if model not in clients:
            clients[model] = EmbeddingClient(
                settings.embedding_api_url,
                settings.embedding_api_key,
                model,
                retries=settings.embed_retries,
            )
        return clients[model]

    def embedder_for(model: str) -> LimitedEmbedder:
        return LimitedEmbedder(raw_client(model), limiter)

    runner = JobRunner(settings, state, writer, tika, embedder_factory=embedder_for)
    locking = LockingRunner(runner, state, settings.lock_timeout)
    return JobEngine(
        settings,
        state,
        writer,
        locking,
        dep_probes={
            "qdrant": writer.ping,
            "embeddings": lambda: raw_client(settings.embedding_model).ping(),
            "tika": tika.ping,
        },
        metrics_hook=metrics.record_run,
    )


def create_app(settings: Settings, engine: JobEngine, metrics: Metrics) -> FastAPI:
    mcp_app = None
    if settings.oidc_issuer:
        validator = OIDCValidator(
            settings.oidc_issuer,
            settings.oidc_audience,
            jwks_cache_ttl=settings.oidc_jwks_cache_ttl,
        )
        # The app carries its own path: create_app registers it as an exact
        # route rather than mounting it, so the scope reaches it unchanged.
        mcp_app = build_mcp_app(
            engine, validator, settings.oidc_operator_role, path=settings.mcp_path
        )
    else:
        # Without an issuer there is nothing to validate tokens against, and
        # an unauthenticated MCP endpoint on this bridge would be a hole.
        log.warning("OIDC_ISSUER is unset; the MCP endpoint stays disabled")
    return create_rest_app(settings, engine, metrics, mcp_app)


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    metrics = Metrics()
    engine = build_engine(settings, metrics)
    app = create_app(settings, engine, metrics)
    engine.startup()
    try:
        uvicorn.run(
            app,
            host=settings.http_host,
            port=settings.http_port,
            log_level=settings.log_level.lower(),
        )
    finally:
        log.info("shutting down; waiting up to %.0fs for running jobs", settings.shutdown_grace)
        engine.shutdown()
        engine.wait_for_runs(settings.shutdown_grace)


if __name__ == "__main__":
    main()
