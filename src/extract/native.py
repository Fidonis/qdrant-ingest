"""Native readers for formats that must not travel through Tika.

Markdown bypassing Tika is a correctness requirement, not an optimisation:
Tika's plaintext handler strips the ``#`` markers — the only signal the
heading-aware chunker has.
"""

import json
from pathlib import Path

import yaml

_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".mdx": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".rst": "text/x-rst",
    ".adoc": "text/asciidoc",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
}


def native_media_type(suffix: str) -> str | None:
    return _MEDIA_TYPES.get(suffix.lower())


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def read_data_file(path: Path) -> str:
    """Pretty-print JSON/YAML so the paragraph window gets stable boundaries.

    Unparseable content falls back to the raw text — a syntax error in a
    config file should not make the document invisible to search.
    """
    raw = read_text_file(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        parsed = yaml.safe_load(raw)
    except (ValueError, yaml.YAMLError):
        return raw
    if parsed is None:
        return raw
    return yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)
