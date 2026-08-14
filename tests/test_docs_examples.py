"""The shipped example catalog must load against the real validator."""

from pathlib import Path

from catalog import load_catalog
from config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "docs" / "jobs.example.yaml"


def test_example_catalog_is_valid() -> None:
    result = load_catalog(EXAMPLE, Settings(), environ={})
    assert result.ok, [
        f"{issue.job_id}.{issue.field}: {issue.message}" for issue in result.errors
    ]
    assert [job.id for job in result.jobs] == ["local-docs"]


def test_example_catalog_has_no_literal_secrets() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    for marker in ("password:", "secret_access_key:", "access_key_id:", "pass:"):
        for line in text.splitlines():
            stripped = line.strip().lstrip("# ")
            if stripped.startswith(marker):
                value = stripped.split(":", 1)[1].strip()
                assert value.startswith("${env:QI_SECRET_"), line
