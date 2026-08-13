"""Forward-only schema migrations.

Applied at store construction inside a single explicit transaction; the
current version lives in ``schema_meta.schema_version``. A database written
by a newer release refuses to open instead of guessing.

Statements are stored individually (not as one script) because
``executescript`` would issue an implicit COMMIT and break transactionality.
"""

import sqlite3

SCHEMA_VERSION_KEY = "schema_version"

_V1: tuple[str, ...] = (
    """
    CREATE TABLE documents (
      job_id       TEXT    NOT NULL,
      collection   TEXT    NOT NULL,
      source       TEXT    NOT NULL,
      rel_path     TEXT    NOT NULL,
      size         INTEGER NOT NULL,
      mtime_ns     INTEGER NOT NULL,
      content_sha  TEXT    NOT NULL,
      text_sha     TEXT,
      params_sha   TEXT    NOT NULL,
      media_type   TEXT,
      chunk_count  INTEGER NOT NULL DEFAULT 0,
      status       TEXT    NOT NULL,
      last_error   TEXT,
      last_run_id  TEXT    NOT NULL,
      indexed_at   TEXT    NOT NULL,
      PRIMARY KEY (job_id, source)
    )
    """,
    "CREATE INDEX idx_documents_collection ON documents(collection, source)",
    """
    CREATE TABLE runs (
      run_id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL,
      mode TEXT NOT NULL,
      full_scope TEXT,
      trigger TEXT NOT NULL,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      status TEXT NOT NULL,
      sync_status TEXT,
      sync_stderr_tail TEXT,
      files_seen INTEGER DEFAULT 0,
      docs_indexed INTEGER DEFAULT 0,
      docs_unchanged INTEGER DEFAULT 0,
      docs_skipped_changed INTEGER DEFAULT 0,
      docs_failed INTEGER DEFAULT 0,
      docs_deleted INTEGER DEFAULT 0,
      chunks_upserted INTEGER DEFAULT 0,
      bytes_read INTEGER DEFAULT 0,
      embed_calls INTEGER DEFAULT 0,
      error TEXT
    )
    """,
    "CREATE INDEX idx_runs_job ON runs(job_id, started_at DESC)",
    """
    CREATE TABLE run_events (
      run_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      ts TEXT NOT NULL,
      level TEXT NOT NULL,
      source TEXT,
      message TEXT NOT NULL,
      PRIMARY KEY (run_id, seq)
    )
    """,
    "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((1, _V1),)

LATEST_VERSION = MIGRATIONS[-1][0]


class SchemaVersionError(Exception):
    """The database was written by a newer release."""


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
    ).fetchone()
    if row is None:
        return 0
    value = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (SCHEMA_VERSION_KEY,)
    ).fetchone()
    return int(value[0]) if value else 0


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring the schema up to LATEST_VERSION. Requires autocommit mode."""
    version = current_version(conn)
    if version > LATEST_VERSION:
        raise SchemaVersionError(
            f"state database schema is version {version}, "
            f"but this release supports at most {LATEST_VERSION}"
        )
    pending = [(target, statements) for target, statements in MIGRATIONS if target > version]
    if not pending:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for target, statements in pending:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                (SCHEMA_VERSION_KEY, str(target)),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
