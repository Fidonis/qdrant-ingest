"""Row-group chunker for tabular text (Tika sheet output, CSV/TSV).

Every chunk re-carries the sheet name and the header row — the repeated
header is the only reason a table chunk is answerable at all once it is
separated from its siblings.
"""

import re

_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")
_CELL_SEPARATORS = ("\t", ",", ";")


def _looks_like_row(line: str) -> bool:
    return any(sep in line for sep in _CELL_SEPARATORS)


def _split_block(block: str) -> tuple[str | None, list[str]]:
    """Return (sheet_name, rows). The first line is a sheet name when it has
    no cell separators but the following lines do."""
    lines = [line for line in block.splitlines() if line.strip()]
    if not lines:
        return None, []
    if len(lines) > 1 and not _looks_like_row(lines[0]) and _looks_like_row(lines[1]):
        return lines[0].strip(), lines[1:]
    return None, lines


def chunk_spreadsheet_text(
    text: str, rows_per_chunk: int, default_sheet_name: str | None = None
) -> list[str]:
    """Group data rows, prefixing every chunk with sheet name and header."""
    chunks: list[str] = []
    for block in _BLOCK_SPLIT_RE.split(text):
        sheet_name, lines = _split_block(block)
        if not lines:
            continue
        name = sheet_name or default_sheet_name
        header, data_rows = lines[0], lines[1:]
        data_groups: list[list[str]]
        if not data_rows:
            # A lone header row still carries the column vocabulary.
            data_groups = [[]]
        else:
            data_groups = [
                data_rows[i : i + rows_per_chunk]
                for i in range(0, len(data_rows), rows_per_chunk)
            ]
        for group in data_groups:
            parts = []
            if name:
                parts.append(f"sheet: {name}")
            parts.append(header)
            parts.extend(group)
            chunks.append("\n".join(parts))
    return chunks
