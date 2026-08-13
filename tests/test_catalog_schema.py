"""Schema-level validation of single job definitions."""

import pytest
from pydantic import ValidationError

from catalog.schema import ChunkingConfig, JobConfig, ScheduleConfig

from support import make_job


def test_minimal_local_job_parses() -> None:
    job = JobConfig.model_validate(make_job())
    assert job.id == "job-a"
    assert job.enabled is True
    assert job.mode == "upsert"
    assert job.full_scope == "job"
    assert job.append_probe == "auto"
    assert job.chunking.words == 400
    assert job.safety.max_delete_ratio == 0.25
    assert job.schedule.is_manual_only


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError, match="typo_key"):
        JobConfig.model_validate(make_job(typo_key=True))


def test_unknown_source_key_rejected() -> None:
    source = {"type": "local", "label": "x", "path": "/data/local/x", "extra": 1}
    with pytest.raises(ValidationError, match="extra"):
        JobConfig.model_validate(make_job(source=source))


def test_s3_requires_bucket() -> None:
    source = {"type": "s3", "label": "s3-src"}
    with pytest.raises(ValidationError, match="bucket"):
        JobConfig.model_validate(make_job(source=source))


def test_unknown_source_type_rejected() -> None:
    source = {"type": "carrier-pigeon", "label": "x"}
    with pytest.raises(ValidationError):
        JobConfig.model_validate(make_job(source=source))


def test_webdav_pass_alias() -> None:
    source = {
        "type": "webdav",
        "label": "cloud",
        "url": "https://cloud.example.com/dav",
        "user": "svc",
        "pass": "${env:QI_SECRET_WEBDAV}",
    }
    job = JobConfig.model_validate(make_job(source=source))
    assert job.source.secret_env_names() == {"password": "QI_SECRET_WEBDAV"}


def test_chunk_overlap_must_be_smaller_than_words() -> None:
    with pytest.raises(ValidationError, match="overlap"):
        ChunkingConfig(words=100, overlap=100)


def test_schedule_cron_and_every_are_exclusive() -> None:
    with pytest.raises(ValidationError, match="not both"):
        ScheduleConfig(cron="0 2 * * *", every="15m")


def test_schedule_every_format() -> None:
    with pytest.raises(ValidationError, match="every"):
        ScheduleConfig(every="15 minutes")
    assert ScheduleConfig(every="15m").every_seconds == 900
    assert ScheduleConfig(every="2h").every_seconds == 7200
    assert ScheduleConfig(every="45s").every_seconds == 45
    assert ScheduleConfig(every="1d").every_seconds == 86400


def test_extra_payload_reserved_keys_rejected() -> None:
    target = {"collection": "c", "extra_payload": {"ingest_run": "boom"}}
    with pytest.raises(ValidationError, match="ingest_run"):
        JobConfig.model_validate(make_job(target=target))


def test_extra_payload_free_keys_allowed() -> None:
    target = {"collection": "c", "extra_payload": {"origin": "s3", "retention_class": "7y"}}
    job = JobConfig.model_validate(make_job(target=target))
    assert job.target.extra_payload["origin"] == "s3"


def test_source_template_requires_placeholders() -> None:
    with pytest.raises(ValidationError, match="rel_path"):
        JobConfig.model_validate(make_job(source_template="static://{label}"))


def test_source_uri_default_template() -> None:
    job = JobConfig.model_validate(make_job())
    assert job.source_uri("reports/q1.pdf") == "local://job-a/reports/q1.pdf"
    assert job.source_prefix() == "local://job-a/"


def test_source_uri_custom_template() -> None:
    job = JobConfig.model_validate(
        make_job(
            source={"type": "sftp", "label": "legal", "host": "sftp.example.com"},
            source_template="sftp://{label}/{rel_path}",
        )
    )
    assert job.source_uri("x.pdf") == "sftp://legal/x.pdf"


def test_job_id_pattern() -> None:
    with pytest.raises(ValidationError):
        JobConfig.model_validate(make_job(id="Not A Slug"))
