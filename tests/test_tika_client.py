"""Tika client: retries, terminal errors, metadata parsing."""

from pathlib import Path

import pytest

from extract import TikaClient, TikaError

from fakes.tika import FakeTika


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-sample")
    return path


def test_retry_on_5xx_then_success(
    fake_tika: FakeTika, tika_client: TikaClient, sample: Path
) -> None:
    fake_tika.status_queue = [500]
    result = tika_client.extract(sample)
    assert result.text == "extracted text"
    assert fake_tika.calls == 2


def test_5xx_exhausts_retries(
    fake_tika: FakeTika, tika_client: TikaClient, sample: Path
) -> None:
    fake_tika.status_queue = [500, 503, 500]
    with pytest.raises(TikaError) as excinfo:
        tika_client.extract(sample)
    assert not excinfo.value.terminal
    assert fake_tika.calls == 3


def test_422_is_terminal_without_retry(
    fake_tika: FakeTika, tika_client: TikaClient, sample: Path
) -> None:
    fake_tika.status_queue = [422]
    with pytest.raises(TikaError) as excinfo:
        tika_client.extract(sample)
    assert excinfo.value.terminal
    assert fake_tika.calls == 1


def test_other_4xx_is_terminal(
    fake_tika: FakeTika, tika_client: TikaClient, sample: Path
) -> None:
    fake_tika.status_queue = [400]
    with pytest.raises(TikaError) as excinfo:
        tika_client.extract(sample)
    assert excinfo.value.terminal


def test_connect_errors_are_retried_then_fail(
    fake_tika: FakeTika, tika_client: TikaClient, sample: Path
) -> None:
    fake_tika.connect_errors = 3
    with pytest.raises(TikaError) as excinfo:
        tika_client.extract(sample)
    assert not excinfo.value.terminal
    assert fake_tika.calls == 3


def test_connect_error_then_success(
    fake_tika: FakeTika, tika_client: TikaClient, sample: Path
) -> None:
    fake_tika.connect_errors = 1
    result = tika_client.extract(sample)
    assert result.text == "extracted text"
    assert fake_tika.calls == 2


def test_metadata_parsing(fake_tika: FakeTika, tika_client: TikaClient, sample: Path) -> None:
    fake_tika.set_response(
        "sample.pdf",
        [
            {
                "X-TIKA:content": "  body text  ",
                "Content-Type": "application/pdf",
                "dc:title": " The Title ",
                "xmpTPg:NPages": "7",
                "X-TIKA:EXCEPTION:embedded": "boom",
            },
            {"X-TIKA:content": "inner", "resourceName": "/inner.docx"},
        ],
    )
    result = tika_client.extract(sample)
    assert result.text == "body text"
    assert result.title == "The Title"
    assert result.pages == 7
    assert result.exceptions == ["X-TIKA:EXCEPTION:embedded: boom"]
    assert len(result.embedded) == 1
    assert result.embedded[0].name == "inner.docx"


def test_metadata_parsing_tika4_keys(
    fake_tika: FakeTika, tika_client: TikaClient, sample: Path
) -> None:
    fake_tika.set_response(
        "sample.pdf",
        [
            {
                "tk:content": "  body text  ",
                "Content-Type": "application/pdf",
                "dc:title": " The Title ",
                "xmpTPg:NPages": "7",
                "tk:exception:embedded-exception": "boom",
                "tk:parsed-by": ["org.apache.tika.parser.DefaultParser"],
            },
            {
                "tk:content": "inner",
                "tk:resource-name": "inner.docx",
                "tk:embedded-resource-path": "/inner.docx",
                "Content-Type": "application/msword",
            },
        ],
    )
    result = tika_client.extract(sample)
    assert result.text == "body text"
    assert result.title == "The Title"
    assert result.pages == 7
    assert result.exceptions == ["tk:exception:embedded-exception: boom"]
    assert len(result.embedded) == 1
    assert result.embedded[0].name == "inner.docx"
    assert result.embedded[0].text == "inner"
    assert result.embedded[0].media_type == "application/msword"


def test_embedded_name_falls_back_to_resource_path(
    fake_tika: FakeTika, tika_client: TikaClient, sample: Path
) -> None:
    fake_tika.set_response(
        "sample.pdf",
        [
            {"tk:content": "outer"},
            {"tk:content": "a", "tk:embedded-resource-path": "/folder/a.pdf"},
            {"X-TIKA:content": "b", "X-TIKA:embedded_resource_path": "/b.pdf"},
            {"tk:content": "c"},
        ],
    )
    result = tika_client.extract(sample)
    assert [doc.name for doc in result.embedded] == ["folder/a.pdf", "b.pdf", "embedded-3"]


def test_429_is_retried_then_success(
    fake_tika: FakeTika, tika_client: TikaClient, sample: Path
) -> None:
    fake_tika.status_queue = [429, 429]
    result = tika_client.extract(sample)
    assert result.text == "extracted text"
    assert fake_tika.calls == 3


def test_429_exhausts_retries_but_is_not_terminal(
    fake_tika: FakeTika, tika_client: TikaClient, sample: Path
) -> None:
    fake_tika.status_queue = [429, 429, 429]
    with pytest.raises(TikaError) as excinfo:
        tika_client.extract(sample)
    assert not excinfo.value.terminal
    assert fake_tika.calls == 3


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        (None, [0.5, 1.0]),
        ("5", [5.0, 5.0]),
        ("600", [30.0, 30.0]),
        ("0", [0.5, 1.0]),
        ("Wed, 21 Oct 2026 07:28:00 GMT", [0.5, 1.0]),
    ],
)
def test_retry_waits_for_retry_after_within_a_cap(
    fake_tika: FakeTika, sample: Path, retry_after: str | None, expected: list[float]
) -> None:
    sleeps: list[float] = []
    client = TikaClient("http://tika.test", transport=fake_tika.transport(), sleep=sleeps.append)
    fake_tika.status_queue = [429, 503]
    fake_tika.retry_after = retry_after
    try:
        result = client.extract(sample)
    finally:
        client.close()
    assert result.text == "extracted text"
    assert sleeps == expected


def test_filename_is_ascii_sanitized(
    fake_tika: FakeTika, tika_client: TikaClient, tmp_path: Path
) -> None:
    path = tmp_path / "Bericht Müller.pdf"
    path.write_bytes(b"%PDF")
    tika_client.extract(path)
    assert fake_tika.filenames[-1] == "Bericht Muller.pdf"
