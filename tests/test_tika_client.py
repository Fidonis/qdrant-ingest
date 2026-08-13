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


def test_filename_is_ascii_sanitized(
    fake_tika: FakeTika, tika_client: TikaClient, tmp_path: Path
) -> None:
    path = tmp_path / "Bericht Müller.pdf"
    path.write_bytes(b"%PDF")
    tika_client.extract(path)
    assert fake_tika.filenames[-1] == "Bericht Muller.pdf"
