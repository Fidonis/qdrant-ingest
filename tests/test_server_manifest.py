"""The MCP registry manifest and the image label that authenticates it."""

import json
import re
from pathlib import Path

from config import APP_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))
DOCKERFILE = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")


def test_branded_namespace() -> None:
    assert MANIFEST["name"] == "de.fidonis/qdrant-ingest"


def test_image_label_matches_manifest_name() -> None:
    # The registry reads this label off the pushed image to prove ownership;
    # a mismatch rejects the publish.
    match = re.search(
        r'LABEL io\.modelcontextprotocol\.server\.name="([^"]+)"', DOCKERFILE
    )
    assert match is not None, "Dockerfile is missing the MCP ownership label"
    assert match.group(1) == MANIFEST["name"]


def test_version_tracks_the_application() -> None:
    assert MANIFEST["version"] == APP_VERSION
    package = MANIFEST["packages"][0]
    assert package["identifier"] == f"ghcr.io/fidonis/qdrant-ingest:{APP_VERSION}"
    # The publish workflow deletes this key; it must not be committed.
    assert "version" not in package


def test_transport_matches_the_served_endpoint() -> None:
    transport = MANIFEST["packages"][0]["transport"]
    assert transport["type"] == "streamable-http"
    assert transport["url"].endswith(":8300/mcp")
    assert "EXPOSE 8300" in DOCKERFILE


def test_required_environment_is_complete() -> None:
    required = {
        entry["name"]
        for entry in MANIFEST["packages"][0]["environmentVariables"]
        if entry.get("isRequired")
    }
    # Pulling the image from the registry without these does nothing useful.
    assert required == {
        "QI_QDRANT_URL",
        "QI_QDRANT_API_KEY",
        "QI_EMBEDDING_API_URL",
        "QI_EMBEDDING_API_KEY",
        "QI_TIKA_URL",
        "QI_JOBS_FILE",
    }


def test_secrets_are_marked() -> None:
    secrets = {
        entry["name"]
        for entry in MANIFEST["packages"][0]["environmentVariables"]
        if entry.get("isSecret")
    }
    assert secrets == {"QI_QDRANT_API_KEY", "QI_EMBEDDING_API_KEY", "QI_API_TOKEN"}
