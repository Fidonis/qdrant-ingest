"""Schema migrations: idempotent, forward-only, refuse newer databases."""

import sqlite3
from pathlib import Path

import pytest

from state import StateStore
from state.migrations import LATEST_VERSION, SchemaVersionError, current_version


def test_fresh_database_is_latest(tmp_path: Path) -> None:
    db_path = tmp_path / "ingest.db"
    store = StateStore(db_path)
    store.close()
    conn = sqlite3.connect(db_path)
    try:
        assert current_version(conn) == LATEST_VERSION
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"documents", "runs", "run_events", "schema_meta"} <= tables
    finally:
        conn.close()


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "ingest.db"
    StateStore(db_path).close()
    store = StateStore(db_path)  # second open must not re-run migrations
    store.close()
    conn = sqlite3.connect(db_path)
    try:
        assert current_version(conn) == LATEST_VERSION
    finally:
        conn.close()


def test_newer_schema_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "ingest.db"
    StateStore(db_path).close()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        (str(LATEST_VERSION + 1),),
    )
    conn.commit()
    conn.close()
    with pytest.raises(SchemaVersionError):
        StateStore(db_path)
