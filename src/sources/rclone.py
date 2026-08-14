"""Source synchronisation through the embedded rclone binary.

rclone runs as a subprocess *inside the causal chain* of an ingestion run: a
failed sync aborts the run before the scan phase, with the exit code and a
stderr tail attached to the run record. Credentials never appear on the
command line; they are written into a per-job ``rclone.conf`` (mode 0600)
inside the state volume, passwords obscured via ``rclone obscure``.
"""

import contextlib
import math
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from catalog.schema import (
    AzureBlobSource,
    FtpSource,
    GdriveSource,
    HttpSource,
    JobConfig,
    LocalSource,
    S3Source,
    SftpSource,
    SmbSource,
    SourceConfig,
    WebdavSource,
)
from catalog.secrets import resolve_secret
from config import Settings
from sources.filters import rclone_filter_args

_STDERR_TAIL_CHARS = 4000

ObscureFn = Callable[[str], str]


@dataclass
class SyncResult:
    """Outcome of the sync phase of one run."""

    ok: bool
    returncode: int
    stderr_tail: str
    duration_seconds: float
    skipped: bool = False  # local sources have no sync step


def obscure_with_binary(rclone_binary: str, value: str) -> str:
    """Obscure a password the way rclone.conf expects, via the binary itself."""
    proc = subprocess.run(
        [rclone_binary, "obscure", "-"],
        input=value,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _single_line_pem(pem: str) -> str:
    # rclone's key_pem option expects the PEM on one line with literal \n.
    return pem.strip().replace("\r\n", "\n").replace("\n", "\\n")


def build_remote_config(
    source: SourceConfig,
    environ: Mapping[str, str],
    obscure: ObscureFn,
) -> tuple[str, str]:
    """Return ``(rclone.conf text, remote spec)`` for a non-local source."""
    label = source.label
    lines = [f"[{label}]"]

    if isinstance(source, S3Source):
        lines.append("type = s3")
        lines.append(f"provider = {source.provider}")
        if source.region:
            lines.append(f"region = {source.region}")
        if source.endpoint:
            lines.append(f"endpoint = {source.endpoint}")
        if source.access_key_id or source.secret_access_key:
            lines.append("env_auth = false")
        if source.access_key_id:
            lines.append(f"access_key_id = {resolve_secret(source.access_key_id, environ)}")
        if source.secret_access_key:
            lines.append(
                f"secret_access_key = {resolve_secret(source.secret_access_key, environ)}"
            )
        prefix = source.prefix.strip("/")
        remote_path = source.bucket + (f"/{prefix}" if prefix else "")
    elif isinstance(source, WebdavSource):
        lines.append("type = webdav")
        lines.append(f"url = {source.url}")
        lines.append(f"vendor = {source.vendor}")
        if source.user:
            lines.append(f"user = {source.user}")
        if source.password:
            lines.append(f"pass = {obscure(resolve_secret(source.password, environ))}")
        remote_path = ""
    elif isinstance(source, SftpSource):
        lines.append("type = sftp")
        lines.append(f"host = {source.host}")
        lines.append(f"port = {source.port}")
        if source.user:
            lines.append(f"user = {source.user}")
        if source.password:
            lines.append(f"pass = {obscure(resolve_secret(source.password, environ))}")
        if source.key_file:
            lines.append(f"key_pem = {_single_line_pem(resolve_secret(source.key_file, environ))}")
        remote_path = source.path
    elif isinstance(source, SmbSource):
        lines.append("type = smb")
        lines.append(f"host = {source.host}")
        if source.user:
            lines.append(f"user = {source.user}")
        if source.password:
            lines.append(f"pass = {obscure(resolve_secret(source.password, environ))}")
        path = source.path.strip("/")
        remote_path = source.share + (f"/{path}" if path else "")
    elif isinstance(source, FtpSource):
        lines.append("type = ftp")
        lines.append(f"host = {source.host}")
        lines.append(f"port = {source.port}")
        if source.user:
            lines.append(f"user = {source.user}")
        if source.password:
            lines.append(f"pass = {obscure(resolve_secret(source.password, environ))}")
        if source.tls:
            lines.append("explicit_tls = true")
        remote_path = source.path.lstrip("/")
    elif isinstance(source, GdriveSource):
        lines.append("type = drive")
        lines.append("scope = drive.readonly")
        if source.service_account_json:
            credentials = resolve_secret(source.service_account_json, environ)
            lines.append(f"service_account_credentials = {' '.join(credentials.split())}")
        if source.token:
            lines.append(f"token = {resolve_secret(source.token, environ)}")
        if source.root_folder_id:
            lines.append(f"root_folder_id = {source.root_folder_id}")
        remote_path = ""
    elif isinstance(source, AzureBlobSource):
        lines.append("type = azureblob")
        lines.append(f"account = {source.account}")
        if source.key:
            lines.append(f"key = {resolve_secret(source.key, environ)}")
        if source.sas_url:
            lines.append(f"sas_url = {resolve_secret(source.sas_url, environ)}")
        prefix = source.prefix.strip("/")
        remote_path = source.container + (f"/{prefix}" if prefix else "")
    elif isinstance(source, HttpSource):
        lines.append("type = http")
        lines.append(f"url = {source.url}")
        remote_path = ""
    else:  # LocalSource — scanned directly, never synced
        raise ValueError("local sources have no rclone remote")

    return "\n".join(lines) + "\n", f"{label}:{remote_path}"


def count_files(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())


def build_sync_command(
    rclone_binary: str,
    conf_path: Path,
    remote: str,
    dest: Path,
    job: JobConfig,
    existing_files: int,
) -> list[str]:
    """The full rclone argv for one sync, --max-delete included.

    --max-delete is the first line of defense derived from max_delete_ratio;
    the ratio check inside the ingester is the second, because the cache can
    also be empty for reasons rclone never sees.
    """
    args = [
        rclone_binary,
        "--config",
        str(conf_path),
        "sync",
        remote,
        str(dest),
        "--transfers",
        "4",
        "--checkers",
        "4",
    ]
    args += rclone_filter_args(job.filters)
    if existing_files > 0:
        max_delete = max(1, math.ceil(job.safety.max_delete_ratio * existing_files))
        args += ["--max-delete", str(max_delete)]
    args += job.source.rclone_flags
    return args


def execute_sync(
    rclone_binary: str,
    conf_text: str,
    conf_path: Path,
    remote: str,
    dest: Path,
    job: JobConfig,
    timeout_seconds: float,
) -> SyncResult:
    """Write the config, run rclone, and capture the outcome."""
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(conf_text, encoding="utf-8")
    with contextlib.suppress(OSError):  # chmod is a no-op on some filesystems
        conf_path.chmod(0o600)
    dest.mkdir(parents=True, exist_ok=True)

    command = build_sync_command(
        rclone_binary, conf_path, remote, dest, job, count_files(dest)
    )
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_seconds
        )
    except FileNotFoundError:
        return SyncResult(
            ok=False,
            returncode=-1,
            stderr_tail=f"rclone binary not found: {rclone_binary}",
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired:
        return SyncResult(
            ok=False,
            returncode=-1,
            stderr_tail=f"rclone sync timed out after {timeout_seconds:.0f}s",
            duration_seconds=time.monotonic() - started,
        )
    return SyncResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stderr_tail=proc.stderr[-_STDERR_TAIL_CHARS:],
        duration_seconds=time.monotonic() - started,
    )


def sync_job(
    job: JobConfig,
    settings: Settings,
    environ: Mapping[str, str] | None = None,
    *,
    rclone_binary: str = "rclone",
    obscure: ObscureFn | None = None,
    timeout_seconds: float = 3600.0,
) -> SyncResult:
    """Synchronise a job's remote source into its cache landing zone."""
    if isinstance(job.source, LocalSource):
        return SyncResult(ok=True, returncode=0, stderr_tail="", duration_seconds=0.0, skipped=True)

    env: Mapping[str, str] = os.environ if environ is None else environ
    if obscure is None:

        def obscure(value: str) -> str:
            return obscure_with_binary(rclone_binary, value)

    conf_text, remote = build_remote_config(job.source, env, obscure)
    conf_path = Path(settings.state_dir) / f"rclone-{job.id}.conf"
    dest = Path(settings.cache_dir) / job.source.label
    return execute_sync(
        rclone_binary, conf_text, conf_path, remote, dest, job, timeout_seconds
    )
