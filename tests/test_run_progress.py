"""What a run reports while it works: phase, files done, the file in hand, dry run."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from engine import JobRunner
from engine.progress import ProgressFlusher
from extract import TikaClient
from sources.rclone import SyncResult
from state import RunRow, StateStore

from conftest import EngineHarness
from support import make_run


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _sync_ok(_job: Any, _settings: Any) -> SyncResult:
    return SyncResult(ok=True, returncode=0, stderr_tail="", duration_seconds=0.0)


def test_a_phase_is_written_at_once_and_a_tick_only_after_the_interval(
    state_store: StateStore,
) -> None:
    clock = _Clock()
    run = make_run(phase="scanning")
    state_store.create_run(run)
    flusher = ProgressFlusher(state_store, run, interval=2.0, clock=clock)

    flusher.phase("embedding")
    stored = state_store.get_run("run-1")
    assert stored is not None and stored.phase == "embedding"

    run.files_done = 5
    flusher.tick("a.md")
    stored = state_store.get_run("run-1")
    assert stored is not None and stored.files_done == 0  # interval not over

    clock.now += 2.5
    flusher.tick("b.md")
    stored = state_store.get_run("run-1")
    assert stored is not None
    assert (stored.files_done, stored.current) == (5, "b.md")


def test_a_failing_flush_never_fails_the_run() -> None:
    class Broken:
        def update_run(self, run: RunRow) -> None:
            raise RuntimeError("database is locked")

    flusher = ProgressFlusher(Broken(), make_run())  # type: ignore[arg-type]
    flusher.phase("embedding")  # must not raise
    flusher.tick("a.md")


@pytest.fixture
def snapshots(engine: EngineHarness, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every write of a run row, as the reader of the database would have seen it."""
    seen: list[dict[str, Any]] = []
    original = engine.state.update_run

    def record(run: RunRow) -> None:
        original(run)
        seen.append(
            {
                "phase": run.phase,
                "files_seen": run.files_seen,
                "files_done": run.files_done,
                "current": run.current,
                "status": run.status,
            }
        )

    monkeypatch.setattr(engine.state, "update_run", record)
    return seen


def _runner(
    engine: EngineHarness, *, sync: Callable[[Any, Any], SyncResult] = _sync_ok
) -> JobRunner:
    """A runner that writes progress on every file, built from the harness' parts."""
    tika = TikaClient(
        "http://tika.test", transport=engine.tika.transport(), sleep=lambda _delay: None
    )
    return JobRunner(
        engine.settings,
        engine.state,
        lambda _connection: engine.writer,
        tika,
        embedder_factory=lambda _model: engine.embeddings,
        sync_fn=sync,
        progress_interval=0.0,
    )


def test_a_local_run_reports_every_file_and_ends_clean(
    engine: EngineHarness, snapshots: list[dict[str, Any]]
) -> None:
    for index in range(3):
        engine.write_doc(f"d{index}.md", f"# D{index}\n\nBody {index}.")
    job = engine.local_job(mode="upsert")

    run = _runner(engine).run_job(job, "manual_rest")

    assert run.status == "success"
    working = [row for row in snapshots if row["status"] == "running"]
    assert working[0]["phase"] == "embedding"
    assert {row["files_seen"] for row in working} == {3}
    embedding = [row for row in working if row["phase"] == "embedding"]
    # One write when the phase starts, then one per file; the file counts as done
    # only once the loop has moved past it.
    assert [row["files_done"] for row in embedding] == [0, 0, 1, 2]
    assert [row["current"] for row in embedding][1:] == ["d0.md", "d1.md", "d2.md"]
    stored = engine.state.get_run(run.run_id)
    assert stored is not None
    assert (stored.phase, stored.current) == (None, None)
    assert (stored.files_done, stored.files_seen, stored.dry_run) == (3, 3, False)


def test_a_remote_run_starts_syncing_and_then_scans(
    engine: EngineHarness, snapshots: list[dict[str, Any]]
) -> None:
    cache = Path(engine.settings.cache_dir) / "dav"
    cache.mkdir(parents=True)
    (cache / "a.md").write_text("# A\n\nBody.", encoding="utf-8")
    phase_during_sync: list[str | None] = []

    def sync(_job: Any, _settings: Any) -> SyncResult:
        # The row exists before the sync starts and already says what is going on.
        phase_during_sync.extend(row.phase for row in engine.state.list_runs(status="running"))
        return _sync_ok(_job, _settings)

    job = engine.local_job(
        source={
            "type": "webdav",
            "label": "dav",
            "url": "http://dav.test",
            "pass": "${env:QI_SECRET_X}",
        },
    )

    run = _runner(engine, sync=sync).run_job(job, "manual_rest")

    assert run.status == "success"
    assert phase_during_sync == ["syncing"]
    assert [row["phase"] for row in snapshots][:2] == ["scanning", "embedding"]


def test_a_dry_run_is_marked(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# A\n\nBody.")

    run = _runner(engine).run_job(engine.local_job(mode="upsert"), "manual_rest", dry_run=True)

    stored = engine.state.get_run(run.run_id)
    assert stored is not None and stored.dry_run is True


def test_cleaning_up_is_a_phase_only_when_something_is_cleaned(
    engine: EngineHarness, snapshots: list[dict[str, Any]]
) -> None:
    engine.write_doc("a.md", "# A\n\nBody.")
    runner = _runner(engine)

    runner.run_job(engine.local_job(mode="append"), "manual_rest")
    assert "cleaning up" not in {row["phase"] for row in snapshots}

    runner.run_job(engine.local_job(mode="upsert"), "manual_rest", dry_run=True)
    assert "cleaning up" not in {row["phase"] for row in snapshots}

    runner.run_job(engine.local_job(mode="upsert"), "manual_rest")
    assert "cleaning up" in {row["phase"] for row in snapshots}


def test_an_aborted_run_keeps_what_it_had_done(engine: EngineHarness) -> None:
    for index in range(4):
        engine.write_doc(f"d{index}.md", f"# D{index}\n\nBody {index}.")
    calls = {"n": 0}

    def should_abort() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # two files go through, the third is refused

    run = _runner(engine).run_job(
        engine.local_job(mode="upsert"), "manual_rest", should_abort=should_abort
    )

    assert run.status == "interrupted"
    stored = engine.state.get_run(run.run_id)
    assert stored is not None
    assert (stored.files_seen, stored.files_done) == (4, 2)
