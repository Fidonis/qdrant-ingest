"""Shared helpers for the test suite."""

from pathlib import Path
from typing import Any

import yaml


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
