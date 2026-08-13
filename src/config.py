"""Runtime configuration.

Every tunable is an environment variable with the ``QI_`` prefix so the
container contract stays greppable in one place. ``OIDC_ISSUER`` is the one
exception: it is shared verbatim with the surrounding stack and keeps its
unprefixed name.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "qdrant-ingest"
APP_VERSION = "0.1.0"


class Settings(BaseSettings):
    """Environment-backed service configuration."""

    model_config = SettingsConfigDict(env_prefix="QI_", case_sensitive=False, extra="ignore")

    # Qdrant
    qdrant_url: str = "http://host.docker.internal:6333"
    qdrant_api_key: str = ""

    # Embeddings
    embedding_api_url: str = "http://litellm:4000/v1"
    embedding_api_key: str = ""
    embedding_model: str = "nomic-embed-text"
    embed_meta_collection: str = "_collection_meta"
    rbac_acl_collection: str = "_rbac_acl"
    embed_batch_size: int = 32
    embed_retries: int = 3
    embed_concurrency: int = 2
    embed_rps: float = 0.0

    # Job catalog
    jobs_file: str = "/config/jobs.yaml"
    jobs_reload_interval: int = 30

    # Tika
    tika_url: str = "http://qdrant-ingest-tika:9998"
    tika_timeout: float = 300.0
    tika_ocr_language: str = "deu+eng"
    tika_pdf_ocr_strategy: str = "auto"
    tika_sniff_unknown: bool = False
    min_chars_per_page: int = 100

    # Extraction limits and chunking
    max_file_bytes: int = 209_715_200
    sheet_rows: int = 40
    chunk_words: int = 400
    chunk_overlap: int = 50

    # Scheduling
    max_concurrent_jobs: int = 2
    timezone: str = "UTC"
    misfire_grace: int = 300
    cron_jitter: int = 30
    lock_timeout: float = 60.0
    run_history_limit: int = 200
    shutdown_grace: float = 30.0

    # Control plane
    rest_auth: str = "token"
    api_token: str = ""
    http_host: str = "0.0.0.0"  # noqa: S104 — container-internal bind
    http_port: int = 8300
    mcp_path: str = "/mcp"
    metrics_enabled: bool = True
    metrics_auth: bool = True
    log_level: str = "INFO"

    # OIDC (MCP transport)
    oidc_issuer: str = Field(default="", validation_alias="OIDC_ISSUER")
    oidc_audience: str = "mcp-qdrant-ingest"
    oidc_operator_role: str = "qdrant-ingest-operator"
    oidc_jwks_cache_ttl: int = 3600

    # Data directories — fixed container paths, overridable for tests
    state_dir: str = "/data/state"
    cache_dir: str = "/data/cache"
    local_dir: str = "/data/local"
