"""Heading-aware markdown chunker."""

from chunk import chunk_markdown, extract_markdown_title

from support import golden_chunks, golden_input


def test_golden_markdown() -> None:
    text = golden_input("markdown", "input.md").read_text(encoding="utf-8")
    assert chunk_markdown(text, 400, 50) == golden_chunks("markdown")


def test_heading_re_prepended_to_every_sub_chunk() -> None:
    body = " ".join(f"word{i}" for i in range(30))
    text = f"## Long Section\n\n{body}"
    chunks = chunk_markdown(text, words=12, overlap=2)
    assert len(chunks) > 1
    assert all(chunk.startswith("## Long Section\n\n") for chunk in chunks)


def test_headingless_text_falls_back_to_word_window() -> None:
    text = " ".join(f"w{i}" for i in range(20))
    chunks = chunk_markdown(text, words=8, overlap=2)
    assert len(chunks) > 1
    assert chunks[0] == " ".join(f"w{i}" for i in range(8))
    # The window steps by words - overlap, so consecutive chunks share words.
    assert chunks[1].split()[0] == "w6"


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_markdown("", 400, 50) == []
    assert chunk_markdown("   \n\n  ", 400, 50) == []


def test_extract_markdown_title() -> None:
    assert extract_markdown_title("# The Title\n\nbody") == "The Title"
    assert extract_markdown_title("intro\n\n## Deep Title\n") == "Deep Title"
    assert extract_markdown_title("no headings here") is None
