"""Slide chunker."""

from chunk import chunk_slides


def test_short_slides_merge_up_to_word_budget() -> None:
    text = "Deck overview\n\nSlide two content here\n\nSlide three words"
    chunks = chunk_slides(text, words=6, deck_title="Quarterly Deck")
    assert chunks == [
        "Quarterly Deck\n\nDeck overview\n\nSlide two content here",
        "Quarterly Deck\n\nSlide three words",
    ]


def test_without_title_no_prefix() -> None:
    chunks = chunk_slides("Alpha\n\nBeta", words=1)
    assert chunks == ["Alpha", "Beta"]


def test_single_long_slide_stays_whole() -> None:
    slide = " ".join(f"w{i}" for i in range(50))
    assert chunk_slides(slide, words=10) == [slide]


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_slides("", words=10) == []
