"""The SQLite state store.

One connection per thread (WAL allows concurrent readers plus one writer),
every write inside an explicit ``BEGIN IMMEDIATE`` so lock acquisition fails
fast and deterministically instead of upgrading mid-transaction. Connections
run in autocommit (``isolation_level=None``); transaction boundaries are
always explicit.
"""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from state.migrations import apply_migrations
from state.models import DocumentRow, RunEvent, RunRow


def now_iso() -> str:
    """Current UTC time in the ISO8601 format used across the store."""
    return datetime.now(tz=UTC).isoformat()


_DOCUMENT_COLUMNS = (
    "job_id, collection, source, rel_path, size, mtime_ns, content_sha, text_sha, "
    "params_sha, media_type, chunk_count, status, last_error, last_run_id, indexed_at"
)

_RUN_COLUMNS = (
    "run_id, job_id, mode, full_scope, trigger, started_at, finished_at, status, "
    "sync_status, sync_stderr_tail, files_seen, docs_indexed, docs_unchanged, "
    "docs_skipped_changed, docs_failed, docs_deleted, chunks_upserted, bytes_read, "
    "embed_calls, error"
)


def _document_from_row(row: sqlite3.Row) -> DocumentRow:
    return DocumentRow(
        job_id=row["job_id"],
        collection=row["collection"],
        source=row["source"],
        rel_path=row["rel_path"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        content_sha=row["content_sha"],
        text_sha=row["text_sha"],
        params_sha=row["params_sha"],
        media_type=row["media_type"],
        chunk_count=row["chunk_count"],
        status=row["status"],
        last_error=row["last_error"],
        last_run_id=row["last_run_id"],
        indexed_at=row["indexed_at"],
    )


def _run_from_row(row: sqlite3.Row) -> RunRow:
    return RunRow(
        run_id=row["run_id"],
        job_id=row["job_id"],
        mode=row["mode"],
        full_scope=row["full_scope"],
        trigger=row["trigger"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        sync_status=row["sync_status"],
        sync_stderr_tail=row["sync_stderr_tail"],
        files_seen=row["files_seen"],
        docs_indexed=row["docs_indexed"],
        docs_unchanged=row["docs_unchanged"],
        docs_skipped_changed=row["docs_skipped_changed"],
        docs_failed=row["docs_failed"],
        docs_deleted=row["docs_deleted"],
        chunks_upserted=row["chunks_upserted"],
        bytes_read=row["bytes_read"],
        embed_calls=row["embed_calls"],
        error=row["error"],
    )


class StateStore:
    """Thread-safe access to the ingest state database."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Bootstrap connection applies migrations before anything else runs.
        apply_migrations(self._conn())

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── documents ────────────────────────────────────────────────────────────

    def get_document(self, job_id: str, source: str) -> DocumentRow | None:
        row = self._conn().execute(
            f"SELECT {_DOCUMENT_COLUMNS} FROM documents WHERE job_id = ? AND source = ?",
            (job_id, source),
        ).fetchone()
        return _document_from_row(row) if row else None

    def upsert_document(self, doc: DocumentRow) -> None:
        with self._write() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO documents ({_DOCUMENT_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.job_id,
                    doc.collection,
                    doc.source,
                    doc.rel_path,
                    doc.size,
                    doc.mtime_ns,
                    doc.content_sha,
                    doc.text_sha,
                    doc.params_sha,
                    doc.media_type,
                    doc.chunk_count,
                    doc.status,
                    doc.last_error,
                    doc.last_run_id,
                    doc.indexed_at,
                ),
            )

    def delete_document(self, job_id: str, source: str) -> None:
        with self._write() as conn:
            conn.execute(
                "DELETE FROM documents WHERE job_id = ? AND source = ?", (job_id, source)
            )

    def delete_documents_for_job(self, job_id: str) -> int:
        with self._write() as conn:
            cursor = conn.execute("DELETE FROM documents WHERE job_id = ?", (job_id,))
            return cursor.rowcount

    def delete_documents_for_collection(self, collection: str) -> int:
        with self._write() as conn:
            cursor = conn.execute("DELETE FROM documents WHERE collection = ?", (collection,))
            return cursor.rowcount

    def list_documents(self, job_id: str) -> list[DocumentRow]:
        rows = self._conn().execute(
            f"SELECT {_DOCUMENT_COLUMNS} FROM documents WHERE job_id = ? ORDER BY source",
            (job_id,),
        ).fetchall()
        return [_document_from_row(row) for row in rows]

    def list_sources(self, job_id: str) -> set[str]:
        rows = self._conn().execute(
            "SELECT source FROM documents WHERE job_id = ?", (job_id,)
        ).fetchall()
        return {row["source"] for row in rows}

    def count_documents(self, job_id: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM documents WHERE job_id = ?", (job_id,)
        ).fetchone()
        return int(row["n"])

    def orphan_summary(self, known_job_ids: set[str]) -> list[dict[str, Any]]:
        """State rows whose job no longer exists in the catalog."""
        rows = self._conn().execute(
            "SELECT job_id, collection, COUNT(*) AS state_rows FROM documents "
            "GROUP BY job_id, collection ORDER BY job_id"
        ).fetchall()
        return [
            {
                "job_id": row["job_id"],
                "collection": row["collection"],
                "state_rows": row["state_rows"],
            }
            for row in rows
            if row["job_id"] not in known_job_ids
        ]

    # ── runs ─────────────────────────────────────────────────────────────────

    def create_run(self, run: RunRow) -> None:
        with self._write() as conn:
            conn.execute(
                f"INSERT INTO runs ({_RUN_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.job_id,
                    run.mode,
                    run.full_scope,
                    run.trigger,
                    run.started_at,
                    run.finished_at,
                    run.status,
                    run.sync_status,
                    run.sync_stderr_tail,
                    run.files_seen,
                    run.docs_indexed,
                    run.docs_unchanged,
                    run.docs_skipped_changed,
                    run.docs_failed,
                    run.docs_deleted,
                    run.chunks_upserted,
                    run.bytes_read,
                    run.embed_calls,
                    run.error,
                ),
            )

    def update_run(self, run: RunRow) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE runs SET mode = ?, full_scope = ?, finished_at = ?, status = ?, "
                "sync_status = ?, sync_stderr_tail = ?, files_seen = ?, docs_indexed = ?, "
                "docs_unchanged = ?, docs_skipped_changed = ?, docs_failed = ?, "
                "docs_deleted = ?, chunks_upserted = ?, bytes_read = ?, embed_calls = ?, "
                "error = ? WHERE run_id = ?",
                (
                    run.mode,
                    run.full_scope,
                    run.finished_at,
                    run.status,
                    run.sync_status,
                    run.sync_stderr_tail,
                    run.files_seen,
                    run.docs_indexed,
                    run.docs_unchanged,
                    run.docs_skipped_changed,
                    run.docs_failed,
                    run.docs_deleted,
                    run.chunks_upserted,
                    run.bytes_read,
                    run.embed_calls,
                    run.error,
                    run.run_id,
                ),
            )

    def get_run(self, run_id: str) -> RunRow | None:
        row = self._conn().execute(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return _run_from_row(row) if row else None

    def list_runs(
        self,
        job_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        since: str | None = None,
    ) -> list[RunRow]:
        clauses = []
        params: list[Any] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT {_RUN_COLUMNS} FROM runs {where}ORDER BY started_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [_run_from_row(row) for row in rows]

    def last_successful_run(self, job_id: str) -> RunRow | None:
        row = self._conn().execute(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE job_id = ? AND status = 'success' "
            "ORDER BY started_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return _run_from_row(row) if row else None

    def reconcile_interrupted_runs(self) -> int:
        """Mark runs left ``running`` by a hard stop as interrupted."""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?, "
                "error = COALESCE(error, 'interrupted by restart') WHERE status = 'running'",
                (now_iso(),),
            )
            return cursor.rowcount

    def prune_runs(self, job_id: str, keep: int) -> int:
        """Keep the newest ``keep`` runs of a job; drop older runs and events."""
        with self._write() as conn:
            stale = [
                row["run_id"]
                for row in conn.execute(
                    "SELECT run_id FROM runs WHERE job_id = ? "
                    "ORDER BY started_at DESC LIMIT -1 OFFSET ?",
                    (job_id, keep),
                ).fetchall()
            ]
            for run_id in stale:
                conn.execute("DELETE FROM run_events WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            return len(stale)

    # ── run events ───────────────────────────────────────────────────────────

    def add_event(
        self, run_id: str, level: str, message: str, source: str | None = None
    ) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO run_events (run_id, seq, ts, level, source, message) "
                "VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events "
                "WHERE run_id = ?), ?, ?, ?, ?)",
                (run_id, run_id, now_iso(), level, source, message),
            )

    def list_events(self, run_id: str) -> list[RunEvent]:
        rows = self._conn().execute(
            "SELECT run_id, seq, ts, level, source, message FROM run_events "
            "WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [
            RunEvent(
                run_id=row["run_id"],
                seq=row["seq"],
                ts=row["ts"],
                level=row["level"],
                source=row["source"],
                message=row["message"],
            )
            for row in rows
        ]
