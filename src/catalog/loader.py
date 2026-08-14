"""Load, merge, and validate the job catalog.

The loader is transactional from the caller's perspective: it never raises on
bad input, it returns every problem as a named :class:`CatalogIssue`. The
engine decides what to do with a partially valid result — on a reload it keeps
the previous registry whenever ``errors`` is non-empty; at startup (when there
is no previous registry) it registers the valid jobs and surfaces the issues
under ``GET /v1/config``.
"""

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from apscheduler.triggers.cron import CronTrigger
from pydantic import ValidationError

from catalog.schema import JobConfig, LocalSource
from config import Settings

# Sections of `defaults:` that are mixed under every job (job keys win).
_DEFAULTS_SECTIONS = frozenset({"embedding", "chunking", "filters", "schedule", "safety"})


@dataclass(frozen=True)
class CatalogIssue:
    """One named validation problem, attributable to a job and field."""

    job_id: str | None
    field: str
    message: str


@dataclass
class LoadResult:
    """Outcome of one catalog load attempt."""

    path: str
    jobs: list[JobConfig] = field(default_factory=list)
    errors: list[CatalogIssue] = field(default_factory=list)
    checksum: str | None = None
    loaded_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def config_error(self) -> str | None:
        if not self.errors:
            return None
        first = self.errors[0]
        suffix = f" (+{len(self.errors) - 1} more)" if len(self.errors) > 1 else ""
        scope = f"job '{first.job_id}': " if first.job_id else ""
        return f"{scope}{first.field}: {first.message}{suffix}"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; override wins on every non-dict leaf."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _issue_from_validation_error(job_id: str | None, exc: ValidationError) -> list[CatalogIssue]:
    issues = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"]) or "<root>"
        issues.append(CatalogIssue(job_id=job_id, field=loc, message=error["msg"]))
    return issues


def _validate_schedule(job: JobConfig) -> list[CatalogIssue]:
    if job.schedule.cron is None:
        return []
    try:
        CronTrigger.from_crontab(job.schedule.cron)
    except ValueError as exc:
        return [CatalogIssue(job.id, "schedule.cron", f"invalid cron expression: {exc}")]
    return []


def _validate_secrets(job: JobConfig, environ: Mapping[str, str]) -> list[CatalogIssue]:
    issues = []
    for field_name, env_name in job.source.secret_env_names().items():
        if not environ.get(env_name):
            issues.append(
                CatalogIssue(
                    job.id,
                    f"source.{field_name}",
                    f"referenced environment variable '{env_name}' is not set",
                )
            )
    return issues


def _validate_local_path(job: JobConfig, settings: Settings) -> list[CatalogIssue]:
    if not isinstance(job.source, LocalSource):
        return []
    mount = os.path.normpath(settings.local_dir).replace("\\", "/").rstrip("/")
    normalized = os.path.normpath(job.source.path).replace("\\", "/")
    if normalized != mount and not normalized.startswith(mount + "/"):
        return [
            CatalogIssue(
                job.id,
                "source.path",
                f"local source paths must live under '{mount}' (got '{job.source.path}')",
            )
        ]
    return []


def _cross_job_issues(jobs: list[JobConfig], settings: Settings) -> list[CatalogIssue]:
    issues: list[CatalogIssue] = []

    seen_ids: set[str] = set()
    for job in jobs:
        if job.id in seen_ids:
            issues.append(CatalogIssue(job.id, "id", "duplicate job id"))
        seen_ids.add(job.id)

    system_collections = {settings.embed_meta_collection, settings.rbac_acl_collection}
    for job in jobs:
        if job.target.collection in system_collections:
            issues.append(
                CatalogIssue(
                    job.id,
                    "target.collection",
                    f"'{job.target.collection}' is a system collection and cannot be a target",
                )
            )

    # The remaining rules apply to enabled jobs only — a disabled job cannot
    # collide with anything at runtime.
    enabled = [job for job in jobs if job.enabled]

    labels_by_collection: dict[str, dict[str, str]] = {}
    for job in enabled:
        labels = labels_by_collection.setdefault(job.target.collection, {})
        other = labels.get(job.source.label)
        if other is not None:
            issues.append(
                CatalogIssue(
                    job.id,
                    "source.label",
                    f"label '{job.source.label}' already serves collection "
                    f"'{job.target.collection}' via job '{other}'; labels must be "
                    "unique per collection so `source` URIs stay disjoint",
                )
            )
        else:
            labels[job.source.label] = job.id

    model_by_collection: dict[str, tuple[str, str]] = {}
    for job in enabled:
        model = job.embedding.model or settings.embedding_model
        previous = model_by_collection.get(job.target.collection)
        if previous is not None and previous[0] != model:
            issues.append(
                CatalogIssue(
                    job.id,
                    "embedding.model",
                    f"collection '{job.target.collection}' is already served with model "
                    f"'{previous[0]}' by job '{previous[1]}'; one embedding model per "
                    "collection",
                )
            )
        elif previous is None:
            model_by_collection[job.target.collection] = (model, job.id)

    return issues


def load_catalog(
    path: str | Path,
    settings: Settings,
    environ: Mapping[str, str] | None = None,
) -> LoadResult:
    """Parse ``jobs.yaml`` and return jobs plus every validation problem."""
    env = os.environ if environ is None else environ
    file_path = Path(path)
    result = LoadResult(path=str(file_path))

    if not file_path.is_file():
        result.errors.append(CatalogIssue(None, "jobs_file", "jobs.yaml not found"))
        return result

    try:
        raw_bytes = file_path.read_bytes()
    except OSError as exc:
        result.errors.append(CatalogIssue(None, "jobs_file", f"unreadable: {exc}"))
        return result

    result.checksum = hashlib.sha256(raw_bytes).hexdigest()

    try:
        document = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        result.errors.append(CatalogIssue(None, "jobs_file", f"invalid YAML: {exc}"))
        return result

    if document is None:
        document = {}
    if not isinstance(document, dict):
        result.errors.append(CatalogIssue(None, "jobs_file", "top level must be a mapping"))
        return result

    version = document.get("version")
    if version != 1:
        result.errors.append(
            CatalogIssue(None, "version", f"unsupported catalog version {version!r}; expected 1")
        )
        return result

    defaults = document.get("defaults") or {}
    if not isinstance(defaults, dict):
        result.errors.append(CatalogIssue(None, "defaults", "defaults must be a mapping"))
        return result
    unknown_sections = set(defaults) - _DEFAULTS_SECTIONS
    if unknown_sections:
        result.errors.append(
            CatalogIssue(
                None,
                "defaults",
                "unsupported defaults sections: " + ", ".join(sorted(unknown_sections)),
            )
        )

    raw_jobs = document.get("jobs") or []
    if not isinstance(raw_jobs, list):
        result.errors.append(CatalogIssue(None, "jobs", "jobs must be a list"))
        return result

    for index, raw_job in enumerate(raw_jobs):
        if not isinstance(raw_job, dict):
            result.errors.append(CatalogIssue(None, f"jobs[{index}]", "job must be a mapping"))
            continue
        job_id = raw_job.get("id") if isinstance(raw_job.get("id"), str) else None
        known_defaults = {key: defaults[key] for key in defaults if key in _DEFAULTS_SECTIONS}
        merged = _deep_merge(known_defaults, raw_job)
        try:
            job = JobConfig.model_validate(merged)
        except ValidationError as exc:
            result.errors.extend(_issue_from_validation_error(job_id or f"jobs[{index}]", exc))
            continue
        result.errors.extend(_validate_schedule(job))
        result.errors.extend(_validate_secrets(job, env))
        result.errors.extend(_validate_local_path(job, settings))
        result.jobs.append(job)

    result.errors.extend(_cross_job_issues(result.jobs, settings))

    if result.errors:
        # Transactional contract: a catalog with errors registers nothing by
        # itself; the caller decides whether to keep a previous registry or
        # (at startup) to accept the subset that validated cleanly.
        failed_ids = {issue.job_id for issue in result.errors}
        result.jobs = [job for job in result.jobs if job.id not in failed_ids]

    return result
