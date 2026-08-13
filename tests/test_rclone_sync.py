"""Integration: drive a real rclone binary against a local-type remote.

Skipped when no rclone binary is on PATH; CI installs one. The functional
Docker harness exercises the binary embedded in the image.
"""

import shutil
from pathlib import Path

import pytest

from catalog.schema import JobConfig
from sources import scan_tree
from sources.rclone import execute_sync

from support import make_job

rclone_missing = shutil.which("rclone") is None
pytestmark = pytest.mark.skipif(rclone_missing, reason="rclone binary not on PATH")


def _local_remote_conf(src: Path) -> tuple[str, str]:
    return "[src]\ntype = local\n", f"src:{src.as_posix()}"


def _job(**overrides: object) -> JobConfig:
    return JobConfig.model_validate(make_job(**overrides))


def _run(src: Path, dest: Path, tmp_path: Path, job: JobConfig) -> object:
    conf_text, remote = _local_remote_conf(src)
    return execute_sync(
        "rclone",
        conf_text,
        tmp_path / "state" / "rclone-test.conf",
        remote,
        dest,
        job,
        timeout_seconds=120.0,
    )


def test_sync_copies_and_deletes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "sub").mkdir(parents=True)
    (src / "a.md").write_text("alpha", encoding="utf-8")
    (src / "sub" / "b.md").write_text("beta", encoding="utf-8")

    result = _run(src, dest, tmp_path, _job())
    assert result.ok, result.stderr_tail  # type: ignore[attr-defined]
    assert (dest / "a.md").read_text(encoding="utf-8") == "alpha"
    assert (dest / "sub" / "b.md").exists()

    # A vanished source file must vanish from the destination on re-sync.
    (src / "a.md").unlink()
    result = _run(src, dest, tmp_path, _job())
    assert result.ok, result.stderr_tail  # type: ignore[attr-defined]
    assert not (dest / "a.md").exists()
    assert (dest / "sub" / "b.md").exists()


def test_sync_applies_include_filters(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "nested").mkdir(parents=True)
    (src / "keep.pdf").write_bytes(b"pdf")
    (src / "skip.tmp").write_bytes(b"tmp")
    (src / "nested" / "deep.pdf").write_bytes(b"pdf")
    (src / "nested" / "deep.tmp").write_bytes(b"tmp")

    job = _job(filters={"include": ["**/*.pdf"], "exclude": ["**/*.tmp"]})
    result = _run(src, dest, tmp_path, job)
    assert result.ok, result.stderr_tail  # type: ignore[attr-defined]
    # `**/` must also match zero segments, so the root-level file comes too.
    assert (dest / "keep.pdf").exists()
    assert (dest / "nested" / "deep.pdf").exists()
    assert not (dest / "skip.tmp").exists()
    assert not (dest / "nested" / "deep.tmp").exists()


def test_sync_filters_match_the_scan(tmp_path: Path) -> None:
    """rclone and the scan phase must agree on the file set."""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "drafts").mkdir(parents=True)
    (src / "published").mkdir(parents=True)
    (src / "top.pdf").write_bytes(b"pdf")
    (src / "published" / "q1.pdf").write_bytes(b"pdf")
    (src / "drafts" / "wip.pdf").write_bytes(b"pdf")

    job = _job(filters={"include": ["**/*.pdf"], "exclude": ["**/drafts/**"]})
    result = _run(src, dest, tmp_path, job)
    assert result.ok, result.stderr_tail  # type: ignore[attr-defined]

    synced = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    scanned = {f.rel_path for f in scan_tree(dest, job.filters)}
    assert synced == {"top.pdf", "published/q1.pdf"}
    assert scanned == synced


def test_failed_sync_reports_stderr(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    result = _run(tmp_path / "does-not-exist", dest, tmp_path, _job())
    assert not result.ok  # type: ignore[attr-defined]
    assert result.returncode != 0  # type: ignore[attr-defined]
    assert result.stderr_tail  # type: ignore[attr-defined]
