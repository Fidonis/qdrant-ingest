"""Which file serves as the job catalog: the configured one, or the legacy one."""

from pathlib import Path

import pytest

from catalog.location import resolve_location
from config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    catalog_dir = tmp_path / "config" / "catalog"
    catalog_dir.mkdir(parents=True)
    return Settings(
        state_dir=str(tmp_path / "state"),
        cache_dir=str(tmp_path / "cache"),
        local_dir=str(tmp_path / "local"),
        jobs_file=str(catalog_dir / "jobs.yaml"),
        jobs_file_legacy=str(tmp_path / "config" / "jobs.yaml"),
    )


def test_primary_location_is_used_when_it_exists(settings: Settings) -> None:
    Path(settings.jobs_file).write_text("version: 1\njobs: []\n", encoding="utf-8")
    location = resolve_location(settings)
    assert location.path == Path(settings.jobs_file)
    assert location.legacy is False


def test_legacy_location_is_served_when_the_primary_is_absent(settings: Settings) -> None:
    Path(settings.jobs_file_legacy).write_text("version: 1\njobs: []\n", encoding="utf-8")
    location = resolve_location(settings)
    assert location.path == Path(settings.jobs_file_legacy)
    assert location.legacy is True


def test_primary_wins_over_legacy_when_both_exist(settings: Settings) -> None:
    Path(settings.jobs_file).write_text("version: 1\njobs: []\n", encoding="utf-8")
    Path(settings.jobs_file_legacy).write_text("version: 1\njobs: []\n", encoding="utf-8")
    assert resolve_location(settings).legacy is False


def test_the_primary_is_reported_when_neither_file_exists(settings: Settings) -> None:
    location = resolve_location(settings)
    assert location.path == Path(settings.jobs_file)
    assert location.legacy is False
