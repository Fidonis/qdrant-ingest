"""Configuration defaults and environment overrides."""

import pytest

from config import APP_VERSION, Settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.http_port == 8300
    assert settings.http_host == "0.0.0.0"
    assert settings.embed_batch_size == 32
    assert settings.embed_concurrency == 2
    assert settings.max_concurrent_jobs == 2
    assert settings.timezone == "UTC"
    assert settings.embed_meta_collection == "_collection_meta"
    assert settings.rbac_acl_collection == "_rbac_acl"
    assert settings.jobs_file == "/config/jobs.yaml"
    assert settings.jobs_reload_interval == 30
    assert settings.rest_auth == "token"
    assert settings.oidc_audience == "mcp-qdrant-ingest"
    assert settings.oidc_operator_role == "qdrant-ingest-operator"
    assert settings.max_file_bytes == 209_715_200
    assert settings.min_chars_per_page == 100
    assert settings.sheet_rows == 40
    assert settings.run_history_limit == 200


def test_version_is_semver() -> None:
    parts = APP_VERSION.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QI_HTTP_PORT", "9000")
    monkeypatch.setenv("QI_EMBEDDING_MODEL", "other-model")
    settings = Settings()
    assert settings.http_port == 9000
    assert settings.embedding_model == "other-model"


def test_oidc_issuer_is_unprefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example.com/realms/papaia")
    settings = Settings()
    assert settings.oidc_issuer == "https://idp.example.com/realms/papaia"
