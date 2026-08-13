"""Loader-level cross-job validation and defaults merging."""

from pathlib import Path

from catalog import load_catalog
from config import Settings

from support import make_job, write_catalog


def test_missing_file(tmp_path: Path) -> None:
    result = load_catalog(tmp_path / "jobs.yaml", Settings())
    assert not result.ok
    assert result.errors[0].field == "jobs_file"
    assert "not found" in result.errors[0].message
    assert result.jobs == []
    assert result.checksum is None


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "jobs.yaml"
    path.write_text("version: 1\njobs: [unclosed", encoding="utf-8")
    result = load_catalog(path, Settings())
    assert not result.ok
    assert "invalid YAML" in result.errors[0].message


def test_wrong_version(tmp_path: Path) -> None:
    path = write_catalog(tmp_path, make_job(), version=2)
    result = load_catalog(path, Settings())
    assert not result.ok
    assert result.errors[0].field == "version"


def test_duplicate_job_id(tmp_path: Path) -> None:
    job_b = make_job(source={"type": "local", "label": "other", "path": "/data/local/b"})
    path = write_catalog(tmp_path, make_job(), job_b)
    result = load_catalog(path, Settings())
    assert any(issue.field == "id" and "duplicate" in issue.message for issue in result.errors)


def test_label_collision_within_collection(tmp_path: Path) -> None:
    job_a = make_job(id="a")
    job_b = make_job(id="b")  # same label, same collection
    path = write_catalog(tmp_path, job_a, job_b)
    result = load_catalog(path, Settings())
    assert any(issue.field == "source.label" for issue in result.errors)


def test_same_label_across_collections_is_fine(tmp_path: Path) -> None:
    job_a = make_job(id="a")
    job_b = make_job(id="b", target={"collection": "col-b"})
    path = write_catalog(tmp_path, job_a, job_b)
    result = load_catalog(path, Settings())
    assert result.ok, result.errors


def test_disabled_job_does_not_collide(tmp_path: Path) -> None:
    job_a = make_job(id="a")
    job_b = make_job(id="b", enabled=False)
    path = write_catalog(tmp_path, job_a, job_b)
    result = load_catalog(path, Settings())
    assert result.ok, result.errors


def test_underscore_collections_rejected_by_pattern(tmp_path: Path) -> None:
    # The default system collections start with "_", which the collection name
    # pattern already forbids — no job can target them even before the
    # dedicated system-collection check runs.
    for name in ("_collection_meta", "_rbac_acl"):
        path = write_catalog(tmp_path, make_job(target={"collection": name}))
        result = load_catalog(path, Settings())
        assert any(issue.field == "target.collection" for issue in result.errors), name


def test_system_collection_rejected_when_renamed(tmp_path: Path) -> None:
    # With a custom (pattern-legal) meta collection name the explicit
    # system-collection check must still refuse the target.
    settings = Settings(embed_meta_collection="meta-coll")
    path = write_catalog(tmp_path, make_job(target={"collection": "meta-coll"}))
    result = load_catalog(path, settings)
    assert any(
        issue.field == "target.collection" and "system collection" in issue.message
        for issue in result.errors
    )


def test_one_embedding_model_per_collection(tmp_path: Path) -> None:
    job_a = make_job(id="a", embedding={"model": "model-one"})
    job_b = make_job(
        id="b",
        source={"type": "local", "label": "other", "path": "/data/local/b"},
        embedding={"model": "model-two"},
    )
    path = write_catalog(tmp_path, job_a, job_b)
    result = load_catalog(path, Settings())
    assert any(issue.field == "embedding.model" for issue in result.errors)


def test_default_model_counts_for_collection_conflict(tmp_path: Path) -> None:
    job_a = make_job(id="a")  # falls back to settings.embedding_model
    job_b = make_job(
        id="b",
        source={"type": "local", "label": "other", "path": "/data/local/b"},
        embedding={"model": "different"},
    )
    path = write_catalog(tmp_path, job_a, job_b)
    result = load_catalog(path, Settings())
    assert any(issue.field == "embedding.model" for issue in result.errors)


def test_local_path_outside_mount_rejected(tmp_path: Path) -> None:
    job = make_job(source={"type": "local", "label": "x", "path": "/etc/passwd"})
    path = write_catalog(tmp_path, job)
    result = load_catalog(path, Settings())
    assert any(issue.field == "source.path" for issue in result.errors)


def test_local_path_traversal_rejected(tmp_path: Path) -> None:
    job = make_job(source={"type": "local", "label": "x", "path": "/data/local/../../etc"})
    path = write_catalog(tmp_path, job)
    result = load_catalog(path, Settings())
    assert any(issue.field == "source.path" for issue in result.errors)


def test_defaults_are_merged_and_job_wins(tmp_path: Path) -> None:
    defaults = {
        "chunking": {"words": 512, "overlap": 64},
        "embedding": {"model": "default-model"},
        "safety": {"max_delete_ratio": 0.5},
    }
    job_a = make_job(id="a")
    job_b = make_job(
        id="b",
        source={"type": "local", "label": "other", "path": "/data/local/b"},
        target={"collection": "col-b"},
        chunking={"words": 128, "overlap": 16},
    )
    path = write_catalog(tmp_path, job_a, job_b, defaults=defaults)
    result = load_catalog(path, Settings())
    assert result.ok, result.errors
    by_id = {job.id: job for job in result.jobs}
    assert by_id["a"].chunking.words == 512
    assert by_id["a"].embedding.model == "default-model"
    assert by_id["a"].safety.max_delete_ratio == 0.5
    assert by_id["b"].chunking.words == 128
    assert by_id["b"].chunking.overlap == 16
    assert by_id["b"].embedding.model == "default-model"


def test_defaults_unknown_section_rejected(tmp_path: Path) -> None:
    path = write_catalog(tmp_path, make_job(), defaults={"target": {"collection": "x"}})
    result = load_catalog(path, Settings())
    assert any(issue.field == "defaults" for issue in result.errors)


def test_invalid_cron_flagged(tmp_path: Path) -> None:
    job = make_job(schedule={"cron": "totally wrong"})
    path = write_catalog(tmp_path, job)
    result = load_catalog(path, Settings())
    assert any(issue.field == "schedule.cron" for issue in result.errors)


def test_valid_jobs_survive_when_others_fail(tmp_path: Path) -> None:
    good = make_job(id="good")
    bad = make_job(
        id="bad",
        source={"type": "local", "label": "b", "path": "/data/local/b"},
        target={"collection": "col-b"},
        schedule={"cron": "not a cron"},
    )
    path = write_catalog(tmp_path, good, bad)
    result = load_catalog(path, Settings())
    assert not result.ok
    assert [job.id for job in result.jobs] == ["good"]


def test_checksum_and_config_error(tmp_path: Path) -> None:
    path = write_catalog(tmp_path, make_job())
    result = load_catalog(path, Settings())
    assert result.ok
    assert result.checksum is not None and len(result.checksum) == 64
    assert result.config_error is None

    missing = load_catalog(tmp_path / "nope.yaml", Settings())
    assert missing.config_error is not None
    assert "not found" in missing.config_error
