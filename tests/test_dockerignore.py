"""The build context excludes development state at every depth.

Docker matches a bare pattern against the context root only, while git matches
a slash-free pattern at any depth. The two ignore files look alike, so a
pattern copied from .gitignore silently stops working: everything the tooling
creates under src/ then lands in the build context and, through
``COPY src/ /app/``, in the image.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
DOCKERFILE = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

PATTERNS = [
    line.strip()
    for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.strip().startswith("#")
]

# Names the toolchain creates below src/, not only at the context root.
NESTED = [
    ".venv",
    "__pycache__",
    "*.pyc",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".env",
    ".env.*",
]


@pytest.mark.parametrize("name", NESTED)
def test_nested_artifacts_are_excluded_at_any_depth(name: str) -> None:
    assert f"**/{name}" in PATTERNS, (
        f"{name!r} must be ignored as '**/{name}' — a bare {name!r} only "
        "matches the context root, so the copy under src/ reaches the image"
    )
    assert name not in PATTERNS, (
        f"a bare {name!r} alongside '**/{name}' is redundant and invites the "
        "root-anchored spelling back"
    )


def test_the_dockerfile_still_copies_the_whole_source_tree() -> None:
    # The patterns above only matter while the build copies src/ wholesale.
    # If that ever narrows, this test has outlived its purpose.
    assert "COPY src/ /app/" in DOCKERFILE
