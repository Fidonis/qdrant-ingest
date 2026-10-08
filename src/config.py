"""Runtime configuration.

Every tunable is an environment variable with the ``QI_`` prefix so the
container contract stays greppable in one place. ``OIDC_ISSUER`` is the one
exception: it is shared verbatim with the surrounding stack and keeps its
unprefixed name.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "qdrant-ingest"
APP_VERSION = "1.0.0"


class Settings(BaseSettings):
    """Environment-backed service configuration."""

    model_config = SettingsConfigDict(env_prefix="QI_", case_sensitive=False, extra="ignore")

    # Embeddings
    #
    # The model is not configured here any more: it lives in jobs.yaml
    # (defaults.embedding.model, or per job) so several models can be in use at
    # once. Only the endpoint stays global -- it is one shared bottleneck.
    embedding_api_url: str = "http://litellm:4000/v1"
    embedding_api_key: str = ""
    embed_meta_collection: str = "_collection_meta"
    rbac_acl_collection: str = "_rbac_acl"
    embed_batch_size: int = 32
    embed_retries: int = 3
    embed_concurrency: int = 2
    embed_rps: float = 0.0

    # Qdrant connections
    #
    # The Qdrant instances jobs may write to are declared in connections.yaml
    # next to the job catalog. Each api-key is stored encrypted;
    # connections_secret is the cleartext key the encryption is derived from.
    # See connections.crypto.
    connections_file: str = "/config/catalog/connections.yaml"
    connections_secret: str = ""

    # Source credentials
    #
    # `${env:QI_SECRET_<NAME>}` is answered from the process environment first and from
    # this encrypted store second. The values are encrypted with a key derived from
    # connections_secret, like the connection api-keys. See catalog.secret_store.
    secrets_file: str = "/config/catalog/secrets.yaml"

    # Job catalog
    #
    # The catalog lives in its own subdirectory of the config bundle, next to
    # connections.yaml and secrets.yaml; the bundle root holds the .env with the
    # REST token and every source credential. `jobs_file_legacy` is where the
    # catalog lived before that subdirectory existed; a deployment still
    # carrying it there keeps working. See catalog.location.
    jobs_file: str = "/config/catalog/jobs.yaml"
    jobs_file_legacy: str = "/config/jobs.yaml"
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
    http_host: str = "0.0.0.0"
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
