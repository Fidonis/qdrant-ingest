"""Job catalog: schema, secret references, and the transactional loader."""

from catalog.loader import CatalogIssue, LoadResult, load_catalog
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
from catalog.writer import (
    CatalogLocation,
    CatalogWriteError,
    dump_document,
    find_job,
    load_document,
    migrate_legacy,
    read_raw,
    remove_job,
    resolve_location,
    upsert_job,
    write_raw,
)

__all__ = [
    "CatalogIssue",
    "CatalogLocation",
    "CatalogWriteError",
    "ChunkingConfig",
    "EmbeddingConfig",
    "FiltersConfig",
    "JobConfig",
    "LoadResult",
    "SafetyConfig",
    "ScheduleConfig",
    "SecretResolutionError",
    "TargetConfig",
    "dump_document",
    "find_job",
    "load_catalog",
    "load_document",
    "migrate_legacy",
    "read_raw",
    "remove_job",
    "resolve_location",
    "resolve_secret",
    "upsert_job",
    "write_raw",
]
