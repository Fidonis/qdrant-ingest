"""Local tree scanning."""

from pathlib import Path

from catalog.schema import FiltersConfig
from sources import scan_tree


def _touch(root: Path, rel: str, content: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_missing_root_yields_nothing(tmp_path: Path) -> None:
    assert scan_tree(tmp_path / "absent", FiltersConfig()) == []


def test_scan_is_sorted_and_posix(tmp_path: Path) -> None:
    _touch(tmp_path, "b/second.md")
    _touch(tmp_path, "a/first.md")
    files = scan_tree(tmp_path, FiltersConfig())
    assert [f.rel_path for f in files] == ["a/first.md", "b/second.md"]
    assert all("\\" not in f.rel_path for f in files)


def test_scan_applies_filters(tmp_path: Path) -> None:
    _touch(tmp_path, "keep/doc.pdf")
    _touch(tmp_path, "keep/skip.tmp")
    _touch(tmp_path, "drafts/doc.pdf")
    filters = FiltersConfig(include=["**/*.pdf"], exclude=["**/drafts/**", "**/*.tmp"])
    files = scan_tree(tmp_path, filters)
    assert [f.rel_path for f in files] == ["keep/doc.pdf"]


def test_scan_reports_size_and_mtime(tmp_path: Path) -> None:
    path = _touch(tmp_path, "doc.md", b"hello world")
    files = scan_tree(tmp_path, FiltersConfig())
    assert files[0].size == 11
    assert files[0].mtime_ns == path.stat().st_mtime_ns
    assert files[0].abs_path == path
