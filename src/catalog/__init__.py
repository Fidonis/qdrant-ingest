"""Job catalog: schema, secret references, and the transactional loader."""

from catalog.loader import CatalogIssue, LoadResult, load_catalog, load_catalog_bytes
from catalog.location import CatalogLocation, resolve_location
from catalog.schema import (
    ChunkingConfig,
    EmbeddingConfig,
    FiltersConfig,
    JobConfig,
    SafetyConfig,
    ScheduleConfig,
    TargetConfig,
)
from catalog.secrets import SecretResolutionError, resolve_secret

__all__ = [
    "CatalogIssue",
    "CatalogLocation",
    "ChunkingConfig",
    "EmbeddingConfig",
    "FiltersConfig",
    "JobConfig",
    "LoadResult",
    "SafetyConfig",
    "ScheduleConfig",
    "SecretResolutionError",
    "TargetConfig",
    "load_catalog",
    "load_catalog_bytes",
    "resolve_location",
    "resolve_secret",
]
