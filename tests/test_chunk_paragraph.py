"""Paragraph-packing chunker."""

from chunk import chunk_paragraphs

from support import golden_chunks, golden_input


def test_golden_paragraphs() -> None:
    text = golden_input("paragraph", "input.txt").read_text(encoding="utf-8")
    assert chunk_paragraphs(text, words=8, overlap=2) == golden_chunks("paragraph")


def test_small_paragraphs_are_packed_together() -> None:
    text = "one two.\n\nthree four.\n\nfive six."
    assert chunk_paragraphs(text, words=10, overlap=2) == [
        "one two.\n\nthree four.\n\nfive six."
    ]


def test_oversized_paragraph_uses_word_window_with_overlap() -> None:
    text = " ".join(f"w{i}" for i in range(20))
    chunks = chunk_paragraphs(text, words=8, overlap=2)
    assert len(chunks) > 1
    assert chunks[0].split()[-2:] == ["w6", "w7"]
    assert chunks[1].split()[:2] == ["w6", "w7"]


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_paragraphs("", 400, 50) == []
    assert chunk_paragraphs("\n\n\n", 400, 50) == []
