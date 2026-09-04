"""Shared helpers for the test suite."""

import json
from pathlib import Path
from typing import Any

import yaml

from state import DocumentRow, RunRow, now_iso

GOLDEN_DIR = Path(__file__).parent / "data" / "golden"


def golden_input(name: str, filename: str) -> Path:
    return GOLDEN_DIR / name / filename


def golden_chunks(name: str) -> list[str]:
    loaded = json.loads((GOLDEN_DIR / name / "expected.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, list)
    return loaded

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


TEST_CONNECTION = "db-a"


def make_job(**overrides: Any) -> dict[str, Any]:
    """A minimal valid local-source job definition, override any key.

    A ``target`` override that omits ``connection`` still gets the default one
    merged in, so the many tests that pass ``target={"collection": ...}`` keep
    producing a schema-valid job.
    """
    job: dict[str, Any] = {
        "id": "job-a",
        "source": {"type": "local", "label": "job-a", "path": "/data/local/a"},
        "target": {"collection": "col-a", "connection": TEST_CONNECTION},
        "mode": "upsert",
    }
    target_override = overrides.pop("target", None)
    job.update(overrides)
    if target_override is not None:
        job["target"] = {"connection": TEST_CONNECTION, **target_override}
    return job


def write_catalog(
    tmp_path: Path,
    *jobs: dict[str, Any],
    defaults: dict[str, Any] | None = None,
    version: int = 1,
) -> Path:
    """Serialize a jobs.yaml document into tmp_path and return its path.

    When no ``defaults`` is given, a default embedding model is supplied so the
    loader's "an enabled job needs a model" rule is satisfied without every
    test spelling it out.
    """
    if defaults is None:
        defaults = {"embedding": {"model": "test-model"}}
    doc: dict[str, Any] = {"version": version, "defaults": defaults, "jobs": list(jobs)}
    path = tmp_path / "jobs.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def write_connections(
    tmp_path: Path,
    *connections: dict[str, Any],
    version: int = 1,
    name: str = "connections.yaml",
) -> Path:
    """Serialize a connections.yaml document into tmp_path and return its path.

    With no connections given, one named ``db-a`` (matching :func:`make_job`) is
    written so a catalog validates against it.
    """
    entries = list(connections) or [{"name": TEST_CONNECTION, "url": "http://qdrant.test:6333"}]
    doc = {"version": version, "connections": entries}
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path
