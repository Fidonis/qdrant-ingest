"""The catalog write path: validate first, replace atomically, keep a backup."""

from pathlib import Path

import pytest

from catalog.writer import (
    CatalogLocation,
    CatalogWriteError,
    backup_path,
    dump_document,
    find_job,
    load_document,
    migrate_legacy,
    read_raw,
    remove_job,
    resolve_location,
    upsert_job,
    write_raw,
)
from config import Settings

VALID = """version: 1
jobs:
  - id: docs
    source:
      type: local
      label: docs
      path: {path}
    target:
      collection: col-a
    mode: append
"""

# `mode: sideways` is not one of full/append/upsert, so the per-job schema
# rejects it -- a candidate that reaches the loader but never the file.
INVALID = VALID.replace("mode: append", "mode: sideways")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    local_dir = tmp_path / "local"
    (local_dir / "docs").mkdir(parents=True)
    catalog_dir = tmp_path / "config" / "catalog"
    catalog_dir.mkdir(parents=True)
    return Settings(
        state_dir=str(tmp_path / "state"),
        cache_dir=str(tmp_path / "cache"),
        local_dir=str(local_dir),
        jobs_file=str(catalog_dir / "jobs.yaml"),
        jobs_file_legacy=str(tmp_path / "config" / "jobs.yaml"),
    )


def valid_for(settings: Settings) -> str:
    return VALID.format(path=str(Path(settings.local_dir) / "docs"))


def invalid_for(settings: Settings) -> str:
    return INVALID.format(path=str(Path(settings.local_dir) / "docs"))


# -- location ---------------------------------------------------------------


def test_primary_location_is_used_when_it_exists(settings: Settings) -> None:
    Path(settings.jobs_file).write_text(valid_for(settings), encoding="utf-8")
    location = resolve_location(settings)
    assert location.path == Path(settings.jobs_file)
    assert location.legacy is False
    assert location.writable is True


def test_legacy_location_is_served_when_the_primary_is_absent(settings: Settings) -> None:
    Path(settings.jobs_file_legacy).write_text(valid_for(settings), encoding="utf-8")
    location = resolve_location(settings)
    assert location.path == Path(settings.jobs_file_legacy)
    assert location.legacy is True


def test_primary_wins_over_legacy_when_both_exist(settings: Settings) -> None:
    Path(settings.jobs_file).write_text(valid_for(settings), encoding="utf-8")
    Path(settings.jobs_file_legacy).write_text(valid_for(settings), encoding="utf-8")
    assert resolve_location(settings).legacy is False


def test_missing_directory_is_not_writable(settings: Settings) -> None:
    absent = settings.model_copy(
        update={"jobs_file": "/nonexistent-directory-for-tests/jobs.yaml"}
    )
    assert resolve_location(absent).writable is False


def test_read_raw_returns_the_starter_document_when_absent(settings: Settings) -> None:
    raw = read_raw(resolve_location(settings))
    assert "version: 1" in raw
    assert load_document(raw)["jobs"] == []


# -- writes -----------------------------------------------------------------


def test_valid_write_lands_and_reloads(settings: Settings) -> None:
    location = resolve_location(settings)
    result = write_raw(location, valid_for(settings), settings)

    assert result.ok
    assert [job.id for job in result.jobs] == ["docs"]
    assert "mode: append" in Path(settings.jobs_file).read_text(encoding="utf-8")


def test_invalid_write_leaves_the_file_untouched(settings: Settings) -> None:
    location = resolve_location(settings)
    write_raw(location, valid_for(settings), settings)
    before = Path(settings.jobs_file).read_text(encoding="utf-8")
    stat_before = Path(settings.jobs_file).stat().st_mtime_ns

    with pytest.raises(CatalogWriteError) as excinfo:
        write_raw(location, invalid_for(settings), settings)

    assert excinfo.value.issues
    assert Path(settings.jobs_file).read_text(encoding="utf-8") == before
    assert Path(settings.jobs_file).stat().st_mtime_ns == stat_before


def test_a_refused_write_leaves_no_temporary_file(settings: Settings) -> None:
    location = resolve_location(settings)
    with pytest.raises(CatalogWriteError):
        write_raw(location, invalid_for(settings), settings)

    leftovers = [p.name for p in Path(settings.jobs_file).parent.iterdir()]
    assert leftovers == []


def test_the_previous_version_is_kept_as_a_backup(settings: Settings) -> None:
    location = resolve_location(settings)
    write_raw(location, valid_for(settings), settings)
    first = Path(settings.jobs_file).read_text(encoding="utf-8")

    write_raw(location, valid_for(settings).replace("col-a", "col-b"), settings)

    assert backup_path(Path(settings.jobs_file)).read_text(encoding="utf-8") == first
    assert "col-b" in Path(settings.jobs_file).read_text(encoding="utf-8")


def test_a_read_only_location_refuses_the_write(settings: Settings) -> None:
    location = CatalogLocation(path=Path(settings.jobs_file), legacy=False, writable=False)
    with pytest.raises(CatalogWriteError) as excinfo:
        write_raw(location, valid_for(settings), settings)
    assert "not writable" in str(excinfo.value)


def test_broken_yaml_is_reported_rather_than_written(settings: Settings) -> None:
    location = resolve_location(settings)
    with pytest.raises(CatalogWriteError) as excinfo:
        write_raw(location, "version: 1\njobs: [oops\n", settings)
    assert any("YAML" in issue.message for issue in excinfo.value.issues)
    assert not Path(settings.jobs_file).exists()


def test_a_literal_credential_is_refused(settings: Settings) -> None:
    """jobs.yaml stays commit-safe by construction, including from the form."""
    location = resolve_location(settings)
    raw = valid_for(settings).replace(
        "      type: local\n      label: docs\n      path: "
        + str(Path(settings.local_dir) / "docs"),
        "      type: webdav\n      label: docs\n      url: https://dav.test\n"
        "      pass: hunter2",
    )
    with pytest.raises(CatalogWriteError):
        write_raw(location, raw, settings)


# -- migration --------------------------------------------------------------


def test_migrate_moves_the_catalog_to_the_writable_location(settings: Settings) -> None:
    Path(settings.jobs_file_legacy).write_text(valid_for(settings), encoding="utf-8")
    assert resolve_location(settings).legacy is True

    result = migrate_legacy(settings)

    assert result.ok
    assert Path(settings.jobs_file).exists()
    assert resolve_location(settings).legacy is False
    # The old file is deliberately left in place; nothing reads it any more.
    assert Path(settings.jobs_file_legacy).exists()


def test_migrate_refuses_when_the_catalog_is_already_current(settings: Settings) -> None:
    Path(settings.jobs_file).write_text(valid_for(settings), encoding="utf-8")
    with pytest.raises(CatalogWriteError):
        migrate_legacy(settings)


# -- document surgery -------------------------------------------------------


def test_upsert_replaces_in_place_and_appends_new_jobs() -> None:
    document = {"version": 1, "jobs": [{"id": "a"}, {"id": "b"}]}

    upsert_job(document, {"id": "a", "mode": "upsert"})
    assert document["jobs"][0] == {"id": "a", "mode": "upsert"}

    upsert_job(document, {"id": "c"})
    assert [job["id"] for job in document["jobs"]] == ["a", "b", "c"]


def test_upsert_under_a_new_id_keeps_the_position() -> None:
    document = {"version": 1, "jobs": [{"id": "a"}, {"id": "b"}]}
    upsert_job(document, {"id": "renamed"}, original_id="a")
    assert [job["id"] for job in document["jobs"]] == ["renamed", "b"]


def test_remove_and_find() -> None:
    document = {"version": 1, "jobs": [{"id": "a"}, {"id": "b"}]}
    assert find_job(document, "b") == {"id": "b"}
    remove_job(document, "b")
    assert find_job(document, "b") is None
    # Removing something that is not there is not an error.
    remove_job(document, "b")


def test_load_document_tolerates_an_empty_file() -> None:
    assert load_document("")["jobs"] == []


def test_load_document_rejects_a_non_mapping() -> None:
    with pytest.raises(CatalogWriteError):
        load_document("- just\n- a list\n")


def test_dump_keeps_the_authored_key_order() -> None:
    text = dump_document({"version": 1, "defaults": {}, "jobs": []})
    assert text.index("version") < text.index("defaults") < text.index("jobs")
