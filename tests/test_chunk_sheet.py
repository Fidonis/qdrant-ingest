"""Row-group chunker for tabular text."""

from chunk import chunk_spreadsheet_text

from support import golden_chunks, golden_input


def test_golden_sheet() -> None:
    text = golden_input("sheet", "input.tsv").read_text(encoding="utf-8")
    assert chunk_spreadsheet_text(text, rows_per_chunk=2) == golden_chunks("sheet")


def test_every_chunk_repeats_the_header() -> None:
    text = "col_a\tcol_b\n" + "\n".join(f"r{i}\t{i}" for i in range(10))
    chunks = chunk_spreadsheet_text(text, rows_per_chunk=3)
    assert len(chunks) == 4
    assert all(chunk.startswith("col_a\tcol_b\n") for chunk in chunks)


def test_sheet_name_blocks_are_prefixed() -> None:
    text = "Revenue\na\tb\n1\t2\n\nCosts\nc\td\n3\t4"
    chunks = chunk_spreadsheet_text(text, rows_per_chunk=40)
    assert chunks == [
        "sheet: Revenue\na\tb\n1\t2",
        "sheet: Costs\nc\td\n3\t4",
    ]


def test_default_sheet_name_applies_when_undetected() -> None:
    chunks = chunk_spreadsheet_text("a\tb\n1\t2", rows_per_chunk=40, default_sheet_name="Data")
    assert chunks == ["sheet: Data\na\tb\n1\t2"]


def test_lone_header_still_produces_a_chunk() -> None:
    assert chunk_spreadsheet_text("a\tb", rows_per_chunk=5) == ["a\tb"]


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_spreadsheet_text("", rows_per_chunk=5) == []
