"""The shipped example files must load against the real validators."""

from pathlib import Path

from catalog import load_catalog
from config import Settings
from connections.loader import load_connections

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "docs" / "jobs.example.yaml"
CONNECTIONS_EXAMPLE = REPO_ROOT / "docs" / "connections.example.yaml"


def test_example_connections_are_valid() -> None:
    result = load_connections(CONNECTIONS_EXAMPLE, Settings(), environ={})
    assert result.ok, [
        f"{issue.name}.{issue.field}: {issue.message}" for issue in result.errors
    ]
    assert result.names() == {"primary"}


def test_example_catalog_is_valid() -> None:
    connections = load_connections(CONNECTIONS_EXAMPLE, Settings(), environ={})
    result = load_catalog(
        EXAMPLE, Settings(), environ={}, known_connections=connections.names()
    )
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


def test_example_connections_have_no_literal_api_key() -> None:
    for line in CONNECTIONS_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().lstrip("# ")
        if stripped.startswith("api_key:"):
            value = stripped.split(":", 1)[1].strip()
            assert value == "" or value.startswith("enc:1:"), line
