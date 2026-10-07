"""Where the job catalog lives.

The catalog sits in its own subdirectory of the config bundle, next to
``connections.yaml`` and ``secrets.yaml``. A deployment that still keeps
``jobs.yaml`` at the old path, in the bundle root, keeps working: the legacy
location is served for as long as the configured one does not exist.
"""

from dataclasses import dataclass
from pathlib import Path

from config import Settings


@dataclass(frozen=True)
class CatalogLocation:
    """Which file is serving as the catalog."""

    path: Path
    legacy: bool


def resolve_location(settings: Settings) -> CatalogLocation:
    """Pick the catalog file.

    The configured path wins whenever it exists. Only when it does not, and
    the legacy path does, is the old location served -- an installation that
    predates the catalog directory keeps running untouched.
    """
    primary = Path(settings.jobs_file)
    legacy_path = Path(settings.jobs_file_legacy)

    if not primary.is_file() and legacy_path != primary and legacy_path.is_file():
        return CatalogLocation(path=legacy_path, legacy=True)

    return CatalogLocation(path=primary, legacy=False)
