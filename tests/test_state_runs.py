"""Run history: lifecycle, filters, retention, restart reconciliation."""

from state import StateStore

from support import make_run


def test_create_and_get_roundtrip(state_store: StateStore) -> None:
    run = make_run()
    state_store.create_run(run)
    assert state_store.get_run("run-1") == run


def test_update_run_finishes(state_store: StateStore) -> None:
    run = make_run()
    state_store.create_run(run)
    run.status = "success"
    run.finished_at = "2026-08-13T02:05:00+00:00"
    run.docs_indexed = 12
    run.chunks_upserted = 340
    state_store.update_run(run)
    loaded = state_store.get_run("run-1")
    assert loaded is not None
    assert loaded.status == "success"
    assert loaded.docs_indexed == 12
    assert loaded.chunks_upserted == 340


def test_list_runs_filters(state_store: StateStore) -> None:
    state_store.create_run(
        make_run(run_id="r1", job_id="a", status="success", started_at="2026-08-01T00:00:00")
    )
    state_store.create_run(
        make_run(run_id="r2", job_id="a", status="failed", started_at="2026-08-02T00:00:00")
    )
    state_store.create_run(
        make_run(run_id="r3", job_id="b", status="success", started_at="2026-08-03T00:00:00")
    )
    assert [r.run_id for r in state_store.list_runs()] == ["r3", "r2", "r1"]
    assert [r.run_id for r in state_store.list_runs(job_id="a")] == ["r2", "r1"]
    assert [r.run_id for r in state_store.list_runs(status="success")] == ["r3", "r1"]
    assert [r.run_id for r in state_store.list_runs(limit=1)] == ["r3"]
    assert [r.run_id for r in state_store.list_runs(since="2026-08-02T00:00:00")] == ["r3", "r2"]


def test_last_successful_run(state_store: StateStore) -> None:
    assert state_store.last_successful_run("a") is None
    state_store.create_run(
        make_run(run_id="r1", job_id="a", status="success", started_at="2026-08-01T00:00:00")
    )
    state_store.create_run(
        make_run(run_id="r2", job_id="a", status="failed", started_at="2026-08-02T00:00:00")
    )
    last = state_store.last_successful_run("a")
    assert last is not None
    assert last.run_id == "r1"


def test_reconcile_interrupted_runs(state_store: StateStore) -> None:
    state_store.create_run(make_run(run_id="r1", status="running"))
    state_store.create_run(make_run(run_id="r2", status="success", finished_at="x"))
    assert state_store.reconcile_interrupted_runs() == 1
    reconciled = state_store.get_run("r1")
    assert reconciled is not None
    assert reconciled.status == "interrupted"
    assert reconciled.finished_at is not None
    untouched = state_store.get_run("r2")
    assert untouched is not None
    assert untouched.status == "success"


def test_prune_runs_keeps_newest_and_drops_events(state_store: StateStore) -> None:
    for i in range(5):
        run_id = f"r{i}"
        state_store.create_run(
            make_run(run_id=run_id, started_at=f"2026-08-0{i + 1}T00:00:00")
        )
        state_store.add_event(run_id, "info", f"event for {run_id}")
    assert state_store.prune_runs("job-a", keep=2) == 3
    remaining = [r.run_id for r in state_store.list_runs(job_id="job-a")]
    assert remaining == ["r4", "r3"]
    assert state_store.list_events("r0") == []
    assert len(state_store.list_events("r4")) == 1


def test_events_sequence_per_run(state_store: StateStore) -> None:
    state_store.create_run(make_run(run_id="r1"))
    state_store.add_event("r1", "info", "first")
    state_store.add_event("r1", "warning", "second", source="sync")
    events = state_store.list_events("r1")
    assert [event.seq for event in events] == [1, 2]
    assert events[0].message == "first"
    assert events[1].level == "warning"
    assert events[1].source == "sync"
