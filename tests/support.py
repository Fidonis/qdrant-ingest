"""Shared helpers for the test suite."""

from pathlib import Path
from typing import Any

import yaml

from state import DocumentRow, RunRow, now_iso


def make_document(**overrides: Any) -> DocumentRow:
    """A minimal valid document row, override any field."""
    values: dict[str, Any] = {
        "job_id": "job-a",
        "collection": "col-a",
        "source": "local://job-a/doc.md",
        "rel_path": "doc.md",
        "size": 123,
        "mtime_ns": 1_700_000_000_000_000_000,
        "content_sha": "c" * 64,
        "params_sha": "p" * 64,
        "status": "indexed",
        "last_run_id": "run-1",
        "indexed_at": now_iso(),
    }
    values.update(overrides)
    return DocumentRow(**values)


def make_run(**overrides: Any) -> RunRow:
    """A minimal valid run row, override any field."""
    values: dict[str, Any] = {
        "run_id": "run-1",
        "job_id": "job-a",
        "mode": "upsert",
        "trigger": "manual_rest",
        "started_at": now_iso(),
        "status": "running",
    }
    values.update(overrides)
    return RunRow(**values)


def make_job(**overrides: Any) -> dict[str, Any]:
    """A minimal valid local-source job definition, override any key."""
    job: dict[str, Any] = {
        "id": "job-a",
        "source": {"type": "local", "label": "job-a", "path": "/data/local/a"},
        "target": {"collection": "col-a"},
        "mode": "upsert",
    }
    job.update(overrides)
    return job


def write_catalog(
    tmp_path: Path,
    *jobs: dict[str, Any],
    defaults: dict[str, Any] | None = None,
    version: int = 1,
) -> Path:
    """Serialize a jobs.yaml document into tmp_path and return its path."""
    doc: dict[str, Any] = {"version": version, "jobs": list(jobs)}
    if defaults is not None:
        doc["defaults"] = defaults
    path = tmp_path / "jobs.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path
