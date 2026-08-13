"""Deletion guards: unit semantics and engine-level enforcement."""

from catalog.schema import SafetyConfig
from engine import check_vanished_deletion

from conftest import EngineHarness


def test_no_vanished_is_always_allowed() -> None:
    decision = check_vanished_deletion({"a"}, {"a"}, SafetyConfig(), force=False)
    assert decision.allowed


def test_empty_source_guard_blocks_total_loss() -> None:
    decision = check_vanished_deletion(set(), {"a", "b"}, SafetyConfig(), force=False)
    assert not decision.allowed
    assert decision.reason is not None and "empty" in decision.reason


def test_empty_source_guard_can_be_disabled() -> None:
    safety = SafetyConfig(empty_source_guard=False, max_delete_ratio=1.0)
    decision = check_vanished_deletion(set(), {"a", "b"}, safety, force=False)
    assert decision.allowed


def test_ratio_guard_blocks_and_force_overrides() -> None:
    seen = {"a"}
    state = {"a", "b", "c", "d"}  # 3 of 4 vanished = 75%
    blocked = check_vanished_deletion(seen, state, SafetyConfig(), force=False)
    assert not blocked.allowed
    assert blocked.reason is not None and "max_delete_ratio" in blocked.reason
    forced = check_vanished_deletion(seen, state, SafetyConfig(), force=True)
    assert forced.allowed


def test_ratio_at_the_limit_is_allowed() -> None:
    seen = {"a", "b", "c"}
    state = {"a", "b", "c", "d"}  # 25% == default limit, not above it
    assert check_vanished_deletion(seen, state, SafetyConfig(), force=False).allowed


def test_engine_empty_tree_aborts_with_guard(engine: EngineHarness) -> None:
    paths = [engine.write_doc(f"d{i}.md", f"# D{i}\n\nBody.") for i in range(3)]
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")

    for path in paths:
        path.unlink()
    run = engine.runner.run_job(job, "manual_rest")

    assert run.status == "aborted_guard"
    assert len(engine.sources_in_qdrant()) == 3  # nothing deleted
    assert len(engine.state.list_sources("job-a")) == 3


def test_engine_ratio_guard_and_force(engine: EngineHarness) -> None:
    paths = [engine.write_doc(f"d{i}.md", f"# D{i}\n\nBody.") for i in range(4)]
    job = engine.local_job(mode="upsert")
    engine.runner.run_job(job, "manual_rest")

    for path in paths[:3]:  # 75% > 25%
        path.unlink()
    blocked = engine.runner.run_job(job, "manual_rest")
    assert blocked.status == "aborted_guard"
    assert len(engine.sources_in_qdrant()) == 4

    forced = engine.runner.run_job(job, "manual_rest", force=True)
    assert forced.status == "success"
    assert forced.docs_deleted == 3
    assert len(engine.sources_in_qdrant()) == 1


def test_engine_respects_configured_ratio(engine: EngineHarness) -> None:
    paths = [engine.write_doc(f"d{i}.md", f"# D{i}\n\nBody.") for i in range(4)]
    job = engine.local_job(mode="upsert", safety={"max_delete_ratio": 0.9})
    engine.runner.run_job(job, "manual_rest")

    for path in paths[:3]:  # 75% < 90%
        path.unlink()
    run = engine.runner.run_job(job, "manual_rest")
    assert run.status == "success"
    assert run.docs_deleted == 3
