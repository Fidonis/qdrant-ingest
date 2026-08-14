"""Overlap protection: RW lock semantics and full-vs-upsert serialisation."""

import threading

from engine import LockingRunner, RunRejectedError
from engine.locks import _ReadWriteLock
from state import RunRow

from conftest import EngineHarness


def test_shared_holders_coexist() -> None:
    lock = _ReadWriteLock()
    assert lock.acquire(exclusive=False, timeout=0.1)
    assert lock.acquire(exclusive=False, timeout=0.1)
    lock.release(exclusive=False)
    lock.release(exclusive=False)


def test_exclusive_excludes_shared_and_times_out() -> None:
    lock = _ReadWriteLock()
    assert lock.acquire(exclusive=True, timeout=0.1)
    assert not lock.acquire(exclusive=False, timeout=0.05)
    assert not lock.acquire(exclusive=True, timeout=0.05)
    lock.release(exclusive=True)
    assert lock.acquire(exclusive=False, timeout=0.1)
    lock.release(exclusive=False)


def test_shared_blocks_exclusive_until_released() -> None:
    lock = _ReadWriteLock()
    assert lock.acquire(exclusive=False, timeout=0.1)
    assert not lock.acquire(exclusive=True, timeout=0.05)
    lock.release(exclusive=False)
    assert lock.acquire(exclusive=True, timeout=0.1)
    lock.release(exclusive=True)


class _Gate:
    """Blocks the embedder mid-run so a lock is provably held."""

    def __init__(self, harness: EngineHarness) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._inner = harness.embeddings.embed_all
        harness.embeddings.embed_all = self._gated  # type: ignore[method-assign]

    def _gated(self, texts: list[str], batch_size: int) -> list[list[float]]:
        self.entered.set()
        assert self.release.wait(timeout=10)
        return self._inner(texts, batch_size)


def _start_run(
    locking: LockingRunner, harness: EngineHarness, job_kwargs: dict[str, object]
) -> tuple[threading.Thread, list[RunRow]]:
    results: list[RunRow] = []
    job = harness.local_job(**job_kwargs)  # type: ignore[arg-type]

    def target() -> None:
        results.append(locking.run(job, "manual_rest"))

    thread = threading.Thread(target=target)
    thread.start()
    return thread, results


def test_full_and_upsert_on_one_collection_serialise(engine: EngineHarness) -> None:
    """A full reindex against a concurrent upsert on the same collection:
    nothing freshly written may be swept."""
    doc_full = engine.write_doc("full-doc.md", "# Full\n\nOwned by the full job.")
    engine.write_doc("upsert-doc.md", "# Upsert\n\nOwned by the upsert job.")
    locking = LockingRunner(engine.runner, engine.state, lock_timeout=10.0)

    full_job_kwargs: dict[str, object] = {
        "id": "job-full",
        "mode": "full",
        "source": {"type": "local", "label": "full-src", "path": str(engine.docs_dir)},
        "filters": {"include": ["full-doc.md"]},
    }
    upsert_job_kwargs: dict[str, object] = {
        "id": "job-upsert",
        "mode": "upsert",
        "source": {"type": "local", "label": "upsert-src", "path": str(engine.docs_dir)},
        "filters": {"include": ["upsert-doc.md"]},
    }

    # Seed both jobs once, without gating.
    locking.run(engine.local_job(**full_job_kwargs), "manual_rest")  # type: ignore[arg-type]
    locking.run(engine.local_job(**upsert_job_kwargs), "manual_rest")  # type: ignore[arg-type]

    # Change both documents, then let the full run block mid-embed while
    # holding the exclusive collection lock.
    doc_full.write_text("# Full\n\nChanged full content.", encoding="utf-8")
    engine.write_doc("upsert-doc.md", "# Upsert\n\nChanged upsert content.")
    gate = _Gate(engine)
    full_thread, full_results = _start_run(locking, engine, full_job_kwargs)
    assert gate.entered.wait(timeout=10)

    # The upsert must wait for the exclusive lock, run only after the sweep,
    # and therefore keep its fresh points.
    upsert_thread, upsert_results = _start_run(locking, engine, upsert_job_kwargs)
    gate.release.set()
    full_thread.join(timeout=20)
    upsert_thread.join(timeout=20)

    assert full_results and full_results[0].status == "success"
    assert upsert_results and upsert_results[0].status == "success"
    texts = {p["source"]: p["text"] for p in engine.payloads()}
    # Both fresh generations survive: the sweep ran strictly before the
    # upsert wrote its points.
    assert "Changed full content" in texts["local://full-src/full-doc.md"]
    assert "Changed upsert content" in texts["local://upsert-src/upsert-doc.md"]


def test_upsert_aborts_with_lock_status_when_full_holds_too_long(
    engine: EngineHarness,
) -> None:
    engine.write_doc("full-doc.md", "# Full\n\nBody.")
    engine.write_doc("upsert-doc.md", "# Upsert\n\nBody.")
    locking = LockingRunner(engine.runner, engine.state, lock_timeout=0.2)

    full_kwargs: dict[str, object] = {
        "id": "job-full",
        "mode": "full",
        "source": {"type": "local", "label": "full-src", "path": str(engine.docs_dir)},
        "filters": {"include": ["full-doc.md"]},
    }
    upsert_kwargs: dict[str, object] = {
        "id": "job-upsert",
        "mode": "upsert",
        "source": {"type": "local", "label": "upsert-src", "path": str(engine.docs_dir)},
        "filters": {"include": ["upsert-doc.md"]},
    }

    gate = _Gate(engine)
    full_thread, full_results = _start_run(locking, engine, full_kwargs)
    assert gate.entered.wait(timeout=10)

    rejected = locking.run(engine.local_job(**upsert_kwargs), "manual_rest")  # type: ignore[arg-type]
    assert rejected.status == "aborted_lock"
    assert rejected.error is not None and "locked" in rejected.error

    gate.release.set()
    full_thread.join(timeout=20)
    assert full_results and full_results[0].status == "success"
    # The rejected run is persisted for the history.
    stored = engine.state.get_run(rejected.run_id)
    assert stored is not None and stored.status == "aborted_lock"


def test_same_job_concurrently_is_rejected(engine: EngineHarness) -> None:
    engine.write_doc("a.md", "# A\n\nBody.")
    locking = LockingRunner(engine.runner, engine.state, lock_timeout=5.0)
    job_kwargs: dict[str, object] = {"mode": "upsert"}

    gate = _Gate(engine)
    thread, results = _start_run(locking, engine, job_kwargs)
    assert gate.entered.wait(timeout=10)

    try:
        locking.run(engine.local_job(**job_kwargs), "manual_rest")  # type: ignore[arg-type]
        raise AssertionError("expected RunRejectedError")
    except RunRejectedError as exc:
        assert exc.job_id == "job-a"
        assert exc.active_run_id is not None  # the running row is discoverable
    finally:
        gate.release.set()
        thread.join(timeout=20)
    assert results and results[0].status == "success"
