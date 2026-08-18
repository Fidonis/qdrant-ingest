"""Where the job catalog lives, and how it is changed without losing it.

The bundle directory is mounted read-only on purpose -- it holds the .env with
the Qdrant api-key, the REST token and every source credential. Only the
``catalog/`` subdirectory below it is writable, which is why the catalog moved
there. A deployment that still keeps ``jobs.yaml`` at the old path keeps
working; the catalog is then served read-only and the interface offers to
migrate it once.

Every write goes through :func:`write_raw`, which validates the candidate with
the same :func:`catalog.loader.load_catalog` the reload path uses. A candidate
that does not load never reaches the file, so the promise the reload path
already makes -- a broken catalog never replaces a working one -- holds for
writes as well.
"""

import logging
import os
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from catalog.loader import CatalogIssue, LoadResult, load_catalog
from config import Settings

log = logging.getLogger("catalog.writer")

# Hidden, and in the target directory so the replace below is a rename within
# one filesystem. The reload poll stats jobs.yaml only, so a half-written
# candidate is never visible to it.
_TMP_NAME = ".jobs.yaml.tmp"

# Writes are serialized process-wide. The engine is threaded and the interface
# is served from the same process, so two operators saving at once would
# otherwise interleave read-modify-write on one document.
_write_lock = threading.Lock()

STARTER_DOCUMENT = """version: 1

jobs: []
"""


class CatalogWriteError(Exception):
    """The candidate did not validate, or the catalog is not writable.

    Carries the issues so the caller can render them per field instead of
    reporting that something, somewhere, was wrong.
    """

    def __init__(self, issues: list[CatalogIssue]) -> None:
        self.issues = issues
        detail = "; ".join(f"{issue.field}: {issue.message}" for issue in issues)
        super().__init__(detail or "write refused")


@dataclass(frozen=True)
class CatalogLocation:
    """Which file is serving as the catalog, and whether it may be changed."""

    path: Path
    legacy: bool
    writable: bool

    @property
    def exists(self) -> bool:
        return self.path.is_file()


def resolve_location(settings: Settings) -> CatalogLocation:
    """Pick the catalog file and probe whether its directory accepts writes.

    The configured path wins whenever it exists. Only when it does not, and
    the legacy path does, is the old location served -- an installation that
    predates the writable directory keeps running untouched.
    """
    primary = Path(settings.jobs_file)
    legacy_path = Path(settings.jobs_file_legacy)

    if not primary.is_file() and legacy_path != primary and legacy_path.is_file():
        return CatalogLocation(path=legacy_path, legacy=True, writable=_dir_writable(legacy_path))

    return CatalogLocation(path=primary, legacy=False, writable=_dir_writable(primary))


def _dir_writable(path: Path) -> bool:
    """True when a new file could be created next to `path`.

    Probes the directory rather than the file: the write is a rename into it,
    and on a fresh install the file does not exist yet.
    """
    directory = path.parent
    return directory.is_dir() and os.access(directory, os.W_OK)


def backup_path(path: Path) -> Path:
    """The location the previous contents are kept at after a write."""
    return path.with_name(path.name + ".bak")


def read_raw(location: CatalogLocation) -> str:
    """Return the catalog file verbatim, or the starter document when absent."""
    if not location.exists:
        return STARTER_DOCUMENT
    return location.path.read_text(encoding="utf-8")


def write_raw(
    location: CatalogLocation,
    raw: str,
    settings: Settings,
    environ: Mapping[str, str] | None = None,
) -> LoadResult:
    """Validate `raw`, then replace the catalog with it atomically.

    Raises :class:`CatalogWriteError` -- leaving the file untouched -- when the
    location is read-only or the candidate does not load. On success the
    previous contents are kept next to the file as ``jobs.yaml.bak``.
    """
    if not location.writable:
        raise CatalogWriteError(
            [
                CatalogIssue(
                    None,
                    "jobs_file",
                    f"{location.path.parent} is not writable; the catalog is read-only here",
                )
            ]
        )

    tmp_path = location.path.parent / _TMP_NAME

    with _write_lock:
        try:
            tmp_path.write_text(raw, encoding="utf-8")
        except OSError as exc:
            raise CatalogWriteError(
                [CatalogIssue(None, "jobs_file", f"could not stage the change: {exc}")]
            ) from exc

        try:
            # Validated as a file, through the very same loader the reload path
            # uses -- the YAML parse, the per-job schema, the secret references
            # and the cross-job checks all run against the candidate.
            candidate = load_catalog(tmp_path, settings, environ)
            if not candidate.ok:
                raise CatalogWriteError(candidate.errors)

            if location.path.is_file():
                shutil.copy2(location.path, backup_path(location.path))
            os.replace(tmp_path, location.path)
        finally:
            tmp_path.unlink(missing_ok=True)

    log.info("catalog written: %d job(s) at %s", len(candidate.jobs), location.path)
    # Re-read from the real path so the result carries it rather than the
    # temporary name the caller never saw.
    return load_catalog(location.path, settings, environ)


def migrate_legacy(settings: Settings, environ: Mapping[str, str] | None = None) -> LoadResult:
    """Copy a legacy catalog to the writable location, validating it first."""
    location = resolve_location(settings)
    if not location.legacy:
        raise CatalogWriteError(
            [CatalogIssue(None, "jobs_file", "the catalog is already at the writable location")]
        )

    target = Path(settings.jobs_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    destination = CatalogLocation(path=target, legacy=False, writable=_dir_writable(target))
    return write_raw(destination, read_raw(location), settings, environ)


# -- document surgery -------------------------------------------------------
#
# Form edits round-trip the document through yaml.safe_load/safe_dump, which
# drops comments and blank lines. That is the documented cost of editing a job
# through the form; the raw editor writes exactly the bytes it was given.


def load_document(raw: str) -> dict[str, Any]:
    """Parse the catalog into a plain document, tolerating an empty file."""
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CatalogWriteError(
            [CatalogIssue(None, "jobs_file", f"invalid YAML: {exc}")]
        ) from exc
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise CatalogWriteError([CatalogIssue(None, "jobs_file", "top level must be a mapping")])
    document.setdefault("version", 1)
    if not isinstance(document.get("jobs"), list):
        document["jobs"] = []
    return document


def dump_document(document: Mapping[str, Any]) -> str:
    """Serialize a document back to YAML, keeping the authored key order."""
    return yaml.safe_dump(
        dict(document), default_flow_style=False, sort_keys=False, allow_unicode=True
    )


def upsert_job(
    document: dict[str, Any], job: Mapping[str, Any], original_id: str | None = None
) -> dict[str, Any]:
    """Insert or replace one job, keeping its position in the list."""
    jobs: list[Any] = list(document.get("jobs") or [])
    target_id = original_id or job.get("id")
    for index, existing in enumerate(jobs):
        if isinstance(existing, dict) and existing.get("id") == target_id:
            jobs[index] = dict(job)
            break
    else:
        jobs.append(dict(job))
    document["jobs"] = jobs
    return document


def remove_job(document: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Drop one job by id. Removing an absent job is not an error."""
    document["jobs"] = [
        job
        for job in (document.get("jobs") or [])
        if not (isinstance(job, dict) and job.get("id") == job_id)
    ]
    return document


def find_job(document: Mapping[str, Any], job_id: str) -> dict[str, Any] | None:
    """Return one job's raw mapping as authored, or None."""
    for job in document.get("jobs") or []:
        if isinstance(job, dict) and job.get("id") == job_id:
            return job
    return None
