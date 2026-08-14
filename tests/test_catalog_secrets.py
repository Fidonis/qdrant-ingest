"""Env-only secret references: literals are rejected, resolution is scoped."""

import pytest
from pydantic import ValidationError

from catalog import load_catalog
from catalog.schema import JobConfig
from catalog.secrets import SecretResolutionError, resolve_secret
from config import Settings

from support import make_job, write_catalog


def _s3_job(**source_overrides: object) -> dict[str, object]:
    source: dict[str, object] = {
        "type": "s3",
        "label": "s3-src",
        "bucket": "reports",
        "access_key_id": "${env:QI_SECRET_S3_ACCESS_KEY}",
        "secret_access_key": "${env:QI_SECRET_S3_SECRET_KEY}",
    }
    source.update(source_overrides)
    return make_job(id="s3-job", source=source)


def test_literal_credential_rejected() -> None:
    with pytest.raises(ValidationError, match="literal credential values"):
        JobConfig.model_validate(_s3_job(secret_access_key="hunter2"))


def test_non_qi_secret_name_rejected() -> None:
    with pytest.raises(ValidationError, match="QI_SECRET_"):
        JobConfig.model_validate(_s3_job(secret_access_key="${env:QI_QDRANT_API_KEY}"))


def test_valid_reference_holds_env_name() -> None:
    job = JobConfig.model_validate(_s3_job())
    assert job.source.secret_env_names() == {
        "access_key_id": "QI_SECRET_S3_ACCESS_KEY",
        "secret_access_key": "QI_SECRET_S3_SECRET_KEY",
    }


def test_resolve_secret_reads_environment() -> None:
    assert resolve_secret("QI_SECRET_X", {"QI_SECRET_X": "value"}) == "value"


def test_resolve_secret_missing_raises() -> None:
    with pytest.raises(SecretResolutionError, match="QI_SECRET_X"):
        resolve_secret("QI_SECRET_X", {})


def test_loader_names_job_and_field_for_literal(tmp_path: object) -> None:
    path = write_catalog(tmp_path, _s3_job(secret_access_key="hunter2"))  # type: ignore[arg-type]
    result = load_catalog(path, Settings(), environ={})
    assert not result.ok
    issue = result.errors[0]
    assert issue.job_id == "s3-job"
    assert "secret_access_key" in issue.field
    assert result.jobs == []


def test_loader_flags_missing_env_var(tmp_path: object) -> None:
    path = write_catalog(tmp_path, _s3_job())  # type: ignore[arg-type]
    result = load_catalog(path, Settings(), environ={"QI_SECRET_S3_ACCESS_KEY": "ak"})
    assert not result.ok
    assert any(
        issue.field == "source.secret_access_key"
        and "QI_SECRET_S3_SECRET_KEY" in issue.message
        for issue in result.errors
    )


def test_loader_accepts_present_env_vars(tmp_path: object) -> None:
    path = write_catalog(tmp_path, _s3_job())  # type: ignore[arg-type]
    env = {"QI_SECRET_S3_ACCESS_KEY": "ak", "QI_SECRET_S3_SECRET_KEY": "sk"}
    result = load_catalog(path, Settings(), environ=env)
    assert result.ok, result.errors
    assert [job.id for job in result.jobs] == ["s3-job"]
