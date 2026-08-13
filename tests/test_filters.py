"""Glob dialect and filter precedence."""

from catalog.schema import FiltersConfig
from sources import (
    glob_to_regex,
    path_matches,
    rclone_filter_args,
    to_rclone_pattern,
)


def test_double_star_crosses_segments() -> None:
    regex = glob_to_regex("**/*.pdf")
    assert regex.match("report.pdf")
    assert regex.match("a/b/c/report.pdf")
    assert not regex.match("report.pdf.bak")


def test_single_star_stays_in_segment() -> None:
    regex = glob_to_regex("*.md")
    assert regex.match("readme.md")
    assert not regex.match("docs/readme.md")


def test_question_mark_one_char() -> None:
    regex = glob_to_regex("file-?.txt")
    assert regex.match("file-1.txt")
    assert not regex.match("file-12.txt")
    assert not regex.match("file-a/b.txt")


def test_directory_exclusion_pattern() -> None:
    regex = glob_to_regex("**/.git/**")
    assert regex.match(".git/config")
    assert regex.match("sub/.git/hooks/pre-commit")
    assert not regex.match("git/config")


def test_prefix_folder_pattern() -> None:
    regex = glob_to_regex("**/drafts/**")
    assert regex.match("drafts/x.pdf")
    assert regex.match("published/drafts/x.pdf")
    assert not regex.match("published/x.pdf")


def test_exclude_wins_over_include() -> None:
    filters = FiltersConfig(include=["**/*.pdf"], exclude=["**/drafts/**"])
    assert path_matches(filters, "published/q1.pdf")
    assert not path_matches(filters, "drafts/q1.pdf")


def test_empty_include_means_everything() -> None:
    filters = FiltersConfig(exclude=["**/*.tmp"])
    assert path_matches(filters, "anything/else.bin")
    assert not path_matches(filters, "x/y.tmp")


def test_include_is_whitelist() -> None:
    filters = FiltersConfig(include=["**/*.md"])
    assert path_matches(filters, "a/b.md")
    assert not path_matches(filters, "a/b.txt")


def test_windows_separators_normalized() -> None:
    filters = FiltersConfig(include=["**/*.md"])
    assert path_matches(filters, "a\\b.md")


def test_rclone_pattern_translation() -> None:
    # A leading **/ must go: rclone requires a directory before it, while an
    # unanchored rclone pattern already floats to any depth — including none.
    assert to_rclone_pattern("**/*.pdf") == "*.pdf"
    assert to_rclone_pattern("**/drafts/**") == "drafts/**"
    assert to_rclone_pattern("**/.git/**") == ".git/**"
    # Everything else is anchored, matching the whole-path regexes.
    assert to_rclone_pattern("*.md") == "/*.md"
    assert to_rclone_pattern("keep/a.md") == "/keep/a.md"
    assert to_rclone_pattern("/already.md") == "/already.md"
    assert to_rclone_pattern("a\\b.md") == "/a/b.md"


def test_rclone_filter_args_order() -> None:
    filters = FiltersConfig(include=["**/*.pdf", "**/*.docx"], exclude=["**/drafts/**"])
    assert rclone_filter_args(filters) == [
        "--filter",
        "- drafts/**",
        "--filter",
        "+ *.pdf",
        "--filter",
        "+ *.docx",
        "--filter",
        "+ */",
        "--filter",
        "- **",
    ]


def test_rclone_filter_args_without_includes() -> None:
    filters = FiltersConfig(exclude=["**/*.tmp"])
    assert rclone_filter_args(filters) == ["--filter", "- *.tmp"]
    assert rclone_filter_args(FiltersConfig()) == []
