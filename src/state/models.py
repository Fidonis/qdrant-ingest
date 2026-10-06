"""Row models for the SQLite state store."""

from dataclasses import asdict, dataclass
from typing import Any, Literal, get_args

DocumentStatus = Literal[
    "indexed",
    "skipped_no_text",
    "skipped_too_large",
    "skipped_unsupported",
    "failed_extract",
    "failed_embed",
]

DOCUMENT_STATUSES: tuple[str, ...] = get_args(DocumentStatus)

RunStatus = Literal["running", "success", "failed", "interrupted", "aborted_guard", "aborted_lock"]

RunTrigger = Literal["cron", "manual_rest", "manual_mcp", "manual_ui", "startup"]

# Where a running run is. `embedding` covers the per-file loop (extract, chunk, embed,
# write); `cleaning up` is the deletion or sweep after a clean scan.
RunPhase = Literal["syncing", "scanning", "embedding", "cleaning up"]


@dataclass
class DocumentRow:
    """One tracked document, keyed by (job_id, source).

    The primary key is deliberately not (collection, rel_path): the job owns
    its slice of a collection, and the same rel_path may legitimately exist
    under two different jobs.
    """

    job_id: str
    collection: str
    source: str
    rel_path: str
    size: int
    mtime_ns: int
    content_sha: str
    params_sha: str
    status: DocumentStatus
    last_run_id: str
    indexed_at: str
    text_sha: str | None = None
    media_type: str | None = None
    chunk_count: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunRow:
    """One ingestion run and its counters."""

    run_id: str
    job_id: str
    mode: str
    trigger: RunTrigger
    started_at: str
    status: RunStatus
    full_scope: str | None = None
    finished_at: str | None = None
    sync_status: str | None = None
    sync_stderr_tail: str | None = None
    files_seen: int = 0
    docs_indexed: int = 0
    docs_unchanged: int = 0
    docs_skipped_changed: int = 0
    docs_failed: int = 0
    docs_deleted: int = 0
    chunks_upserted: int = 0
    bytes_read: int = 0
    embed_calls: int = 0
    error: str | None = None
    # Progress, flushed while the run works (see engine.progress). `phase` is one of
    # PHASES, `current` the relative path being processed, `files_done` counts every
    # file of the scan loop, including the ones skipped as unchanged.
    dry_run: bool = False
    files_done: int = 0
    phase: str | None = None
    current: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunEvent:
    """One log line attached to a run."""

    run_id: str
    seq: int
    ts: str
    level: str
    source: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
