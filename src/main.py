"""Service entry point: wire the settings into the engine and serve the app."""

import functools
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from api.metrics import Metrics
from api.rest import create_app as create_rest_app
from catalog.location import resolve_location
from catalog.secret_store import SecretStore, default_environ
from config import Settings
from connections.registry import ConnectionRegistry
from embed import EmbeddingClient, EmbeddingLimiter, LimitedEmbedder
from engine import JobRunner, LockingRunner
from engine.service import JobEngine
from extract import TikaClient
from mcp_app import OIDCValidator, build_mcp_app
from sources import sync_job
from state import StateStore

log = logging.getLogger("main")


def resolve_catalog_setting(settings: Settings) -> Settings:
    """Point `jobs_file` at whichever catalog this deployment actually has.

    The configured path wins. Only when it is absent and the legacy one is
    present does the old location take over, so an installation predating the
    catalog directory keeps running.
    """
    location = resolve_location(settings)
    if str(location.path) != settings.jobs_file:
        log.warning(
            "serving the job catalog from its legacy path %s; move it to %s",
            location.path,
            settings.jobs_file,
        )
        return settings.model_copy(update={"jobs_file": str(location.path)})
    return settings


def build_engine(settings: Settings, metrics: Metrics) -> JobEngine:
    state = StateStore(Path(settings.state_dir) / "ingest.db")
    # The Qdrant instances jobs write to are declared in connections.yaml; the
    # registry resolves them and hands out one writer per connection.
    registry = ConnectionRegistry(settings)
    # Credentials resolve against the process environment and, behind it, the encrypted
    # store. The same mapping goes to the catalog loader (does the secret exist?) and to the
    # sync (what is it?): a stored value that only the loader could see would validate and
    # then fail at the first run.
    secret_store = SecretStore(settings.secrets_file, settings.connections_secret)
    environ = default_environ(secret_store)
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

    runner = JobRunner(
        settings,
        state,
        registry.writer,
        tika,
        embedder_factory=embedder_for,
        sync_fn=functools.partial(sync_job, environ=environ),
    )
    locking = LockingRunner(runner, state, settings.lock_timeout)
    return JobEngine(
        settings,
        state,
        registry,
        locking,
        environ=environ,
        secret_store=secret_store,
        dep_probes={
            # ping() only hits GET /models, so any model string works here; the
            # per-job models are what the runs use.
            "embeddings": lambda: raw_client("_probe").ping(),
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
    settings = resolve_catalog_setting(Settings())
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
