"""Service entry point.

Boots the FastAPI control plane with the auth-free ``/health`` endpoint.
The ingestion engine, scheduler, REST surface, and MCP server are wired in
here as they land in their own modules.
"""

import logging

import uvicorn
from fastapi import FastAPI

from config import APP_NAME, APP_VERSION, Settings


def create_app(settings: Settings) -> FastAPI:
    """Build the ASGI application for the given settings."""
    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": APP_VERSION,
            "jobs_loaded": 0,
            "config_error": None,
        }

    return app


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        create_app(settings),
        host=settings.http_host,
        port=settings.http_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
