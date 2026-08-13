"""rclone.conf generation and sync command construction (pure, no binary)."""

from pathlib import Path

import pytest

from catalog.schema import JobConfig
from sources import build_remote_config
from sources.rclone import build_sync_command

from support import make_job

_ENV = {
    "QI_SECRET_S3_ACCESS_KEY": "AKIA123",
    "QI_SECRET_S3_SECRET_KEY": "s3secret",
    "QI_SECRET_WEBDAV": "davpass",
    "QI_SECRET_SFTP_KEY": "-----BEGIN KEY-----\nabc\ndef\n-----END KEY-----",
}


def _fake_obscure(value: str) -> str:
    return f"OBSCURED({value})"


def _job(source: dict[str, object], **overrides: object) -> JobConfig:
    return JobConfig.model_validate(make_job(source=source, **overrides))


def test_s3_config_and_remote() -> None:
    job = _job(
        {
            "type": "s3",
            "label": "acme-reports",
            "bucket": "acme-corp-reports",
            "prefix": "published/",
            "region": "eu-central-1",
            "access_key_id": "${env:QI_SECRET_S3_ACCESS_KEY}",
            "secret_access_key": "${env:QI_SECRET_S3_SECRET_KEY}",
        }
    )
    conf, remote = build_remote_config(job.source, _ENV, _fake_obscure)
    assert "[acme-reports]" in conf
    assert "type = s3" in conf
    assert "provider = AWS" in conf
    assert "region = eu-central-1" in conf
    assert "env_auth = false" in conf
    assert "access_key_id = AKIA123" in conf
    assert "secret_access_key = s3secret" in conf
    assert remote == "acme-reports:acme-corp-reports/published"


def test_webdav_password_is_obscured() -> None:
    job = _job(
        {
            "type": "webdav",
            "label": "nextcloud-hr",
            "url": "https://cloud.example.com/remote.php/dav/files/svc/HR",
            "vendor": "nextcloud",
            "user": "svc-ingest",
            "pass": "${env:QI_SECRET_WEBDAV}",
        }
    )
    conf, remote = build_remote_config(job.source, _ENV, _fake_obscure)
    assert "pass = OBSCURED(davpass)" in conf
    assert "davpass" not in conf.replace("OBSCURED(davpass)", "")
    assert "vendor = nextcloud" in conf
    assert remote == "nextcloud-hr:"


def test_sftp_key_pem_single_line() -> None:
    job = _job(
        {
            "type": "sftp",
            "label": "legal",
            "host": "sftp.partner.example.com",
            "user": "fidonis",
            "key_file": "${env:QI_SECRET_SFTP_KEY}",
            "path": "/export/legal",
        }
    )
    conf, remote = build_remote_config(job.source, _ENV, _fake_obscure)
    assert "key_pem = -----BEGIN KEY-----\\nabc\\ndef\\n-----END KEY-----" in conf
    assert "\nabc" not in conf.split("key_pem")[1].split("\n")[0]
    assert remote == "legal:/export/legal"


def test_local_source_has_no_remote() -> None:
    job = JobConfig.model_validate(make_job())
    with pytest.raises(ValueError, match="local"):
        build_remote_config(job.source, _ENV, _fake_obscure)


def test_sync_command_includes_filters_and_flags(tmp_path: Path) -> None:
    job = _job(
        {
            "type": "s3",
            "label": "acme",
            "bucket": "b",
            "rclone_flags": ["--s3-no-check-bucket"],
        },
        filters={"include": ["**/*.pdf"], "exclude": ["**/drafts/**"]},
    )
    command = build_sync_command(
        "rclone", tmp_path / "conf", "acme:b", tmp_path / "dest", job, existing_files=0
    )
    assert command[0] == "rclone"
    assert "sync" in command
    assert "--filter" in command
    # Patterns arrive translated into rclone's dialect, not verbatim.
    assert "- drafts/**" in command
    assert "+ *.pdf" in command
    assert "+ */" in command
    assert "- **" in command
    assert command[-1] == "--s3-no-check-bucket"
    assert "--max-delete" not in command  # empty destination: first sync


def test_sync_command_max_delete_from_ratio(tmp_path: Path) -> None:
    job = _job(
        {"type": "s3", "label": "acme", "bucket": "b"},
        safety={"max_delete_ratio": 0.25},
    )
    command = build_sync_command(
        "rclone", tmp_path / "conf", "acme:b", tmp_path / "dest", job, existing_files=10
    )
    index = command.index("--max-delete")
    assert command[index + 1] == "3"  # ceil(0.25 * 10)


def test_sync_command_max_delete_floor_is_one(tmp_path: Path) -> None:
    job = _job(
        {"type": "s3", "label": "acme", "bucket": "b"},
        safety={"max_delete_ratio": 0.0},
    )
    command = build_sync_command(
        "rclone", tmp_path / "conf", "acme:b", tmp_path / "dest", job, existing_files=5
    )
    index = command.index("--max-delete")
    assert command[index + 1] == "1"
