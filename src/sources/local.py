"""Filter-aware scan of a directory tree (cache landing zone or local mount)."""

from dataclasses import dataclass
from pathlib import Path

from catalog.schema import FiltersConfig
from sources.filters import path_matches


@dataclass(frozen=True)
class ScannedFile:
    """One candidate file found below the scan root."""

    abs_path: Path
    rel_path: str  # posix, relative to the scan root
    size: int
    mtime_ns: int


def scan_tree(root: Path, filters: FiltersConfig) -> list[ScannedFile]:
    """Deterministically ordered candidate files below ``root``."""
    if not root.is_dir():
        return []
    result: list[ScannedFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if not path_matches(filters, rel):
            continue
        stat = path.stat()
        result.append(
            ScannedFile(abs_path=path, rel_path=rel, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        )
    return result
