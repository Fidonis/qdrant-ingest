"""Parallel writers: WAL plus BEGIN IMMEDIATE must serialize cleanly."""

import threading

from state import StateStore

from support import make_document, make_run


def test_parallel_document_writers(state_store: StateStore) -> None:
    threads = 4
    writes_per_thread = 50
    errors: list[Exception] = []

    def writer(thread_index: int) -> None:
        try:
            for i in range(writes_per_thread):
                state_store.upsert_document(
                    make_document(
                        job_id=f"job-{thread_index}",
                        source=f"local://job-{thread_index}/{i}.md",
                    )
                )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    workers = [threading.Thread(target=writer, args=(t,)) for t in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert errors == []
    total = sum(state_store.count_documents(f"job-{t}") for t in range(threads))
    assert total == threads * writes_per_thread


def test_parallel_event_writers_get_distinct_sequences(state_store: StateStore) -> None:
    state_store.create_run(make_run(run_id="r1"))
    events_per_thread = 25
    threads = 4
    errors: list[Exception] = []

    def writer(thread_index: int) -> None:
        try:
            for i in range(events_per_thread):
                state_store.add_event("r1", "info", f"t{thread_index}-{i}")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    workers = [threading.Thread(target=writer, args=(t,)) for t in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert errors == []
    events = state_store.list_events("r1")
    assert len(events) == threads * events_per_thread
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
