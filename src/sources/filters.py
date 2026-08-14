"""Include/exclude glob filters.

One pattern dialect, two consumers: the same globs are translated into
rclone ``--filter`` rules for the sync phase and into regexes for the local
scan phase, so both phases see the same file set by construction.

Dialect: ``*`` matches within one path segment, ``?`` one character,
``**`` crosses segment boundaries, and a leading ``**/`` also matches zero
segments (so ``**/*.pdf`` includes a top-level ``report.pdf``).
"""

import re
from functools import lru_cache

from catalog.schema import FiltersConfig


@lru_cache(maxsize=512)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one glob pattern into an anchored regex over posix rel paths."""
    normalized = pattern.replace("\\", "/").lstrip("/")
    parts: list[str] = []
    i = 0
    length = len(normalized)
    while i < length:
        char = normalized[i]
        if char == "*":
            if normalized[i : i + 3] == "**/":
                parts.append("(?:.*/)?")
                i += 3
            elif normalized[i : i + 2] == "**":
                parts.append(".*")
                i += 2
            else:
                parts.append("[^/]*")
                i += 1
        elif char == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(char))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def path_matches(filters: FiltersConfig, rel_path: str) -> bool:
    """Excludes win; a non-empty include list is a whitelist."""
    rel = rel_path.replace("\\", "/").lstrip("/")
    for pattern in filters.exclude:
        if glob_to_regex(pattern).match(rel):
            return False
    if filters.include:
        return any(glob_to_regex(pattern).match(rel) for pattern in filters.include)
    return True


def to_rclone_pattern(pattern: str) -> str:
    """Translate one pattern of our dialect into an equivalent rclone rule.

    The dialects differ in two places, and both matter:

    - Our leading ``**/`` also matches zero segments, so ``**/*.pdf`` covers a
      top-level ``report.pdf``. rclone's ``**/`` requires a directory before
      it, but an unanchored rclone pattern already floats to any depth — so
      stripping the prefix reproduces our semantics exactly. Left in place,
      rclone would never fetch the root-level files the scan expects.
    - Everything else is anchored here, because our regexes match the whole
      relative path, whereas an unanchored rclone pattern would also match the
      same name nested anywhere below the root.
    """
    normalized = pattern.replace("\\", "/")
    if normalized.startswith("**/"):
        return normalized[3:]
    if normalized.startswith("/"):
        return normalized
    return "/" + normalized


def rclone_filter_args(filters: FiltersConfig) -> list[str]:
    """The same file set as :func:`path_matches`, as rclone rules.

    rclone applies the first matching rule, so excludes are emitted first and
    a non-empty include list is closed with a catch-all exclude.
    """
    args: list[str] = []
    for pattern in filters.exclude:
        args += ["--filter", f"- {to_rclone_pattern(pattern)}"]
    for pattern in filters.include:
        args += ["--filter", f"+ {to_rclone_pattern(pattern)}"]
    if filters.include:
        # Directories must stay walkable or nothing below them is reachable.
        args += ["--filter", "+ */", "--filter", "- **"]
    return args
