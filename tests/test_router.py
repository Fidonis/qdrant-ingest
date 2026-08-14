"""Format routing: extension wins, markdown never touches Tika."""

import json
from pathlib import Path

from catalog.schema import ChunkingConfig
from config import Settings
from extract import TikaClient, process_file

from fakes.tika import FakeTika
from support import golden_chunks, golden_input


def test_markdown_never_hits_tika(fake_tika: FakeTika, tika_client: TikaClient) -> None:
    result = process_file(
        golden_input("markdown", "input.md"),
        "input.md",
        ChunkingConfig(),
        Settings(),
        tika_client,
    )
    assert fake_tika.calls == 0  # the proof: zero Tika requests for markdown
    assert result.status == "ok"
    assert result.format_class == "markdown"
    assert result.media_type == "text/markdown"
    assert result.title == "Title"
    assert result.chunks == golden_chunks("markdown")


def test_plaintext_golden(tika_client: TikaClient, fake_tika: FakeTika) -> None:
    result = process_file(
        golden_input("paragraph", "input.txt"),
        "input.txt",
        ChunkingConfig(words=8, overlap=2),
        Settings(),
        tika_client,
    )
    assert fake_tika.calls == 0
    assert result.status == "ok"
    assert result.chunks == golden_chunks("paragraph")


def test_json_data_golden(tika_client: TikaClient) -> None:
    result = process_file(
        golden_input("data", "input.json"),
        "input.json",
        ChunkingConfig(),
        Settings(),
        tika_client,
    )
    assert result.status == "ok"
    assert result.chunks == golden_chunks("data")
    assert result.media_type == "application/json"


def test_tsv_sheet_golden(tika_client: TikaClient, fake_tika: FakeTika) -> None:
    result = process_file(
        golden_input("sheet", "input.tsv"),
        "input.tsv",
        ChunkingConfig(),
        Settings(sheet_rows=2),
        tika_client,
    )
    assert fake_tika.calls == 0
    assert result.status == "ok"
    assert result.chunks == golden_chunks("sheet")


def test_pdf_document_golden(
    tmp_path: Path, fake_tika: FakeTika, tika_client: TikaClient
) -> None:
    elements = json.loads(
        golden_input("document", "input.rmeta.json").read_text(encoding="utf-8")
    )
    fake_tika.set_response("input.pdf", elements)
    dummy = tmp_path / "input.pdf"
    dummy.write_bytes(b"%PDF-dummy")
    result = process_file(dummy, "input.pdf", ChunkingConfig(), Settings(), tika_client)
    assert fake_tika.calls == 1
    assert result.status == "ok"
    assert result.title == "Sample Report"
    assert result.media_type == "application/pdf"
    assert result.chunks == golden_chunks("document")


def test_pptx_slide_golden(
    tmp_path: Path, fake_tika: FakeTika, tika_client: TikaClient
) -> None:
    elements = json.loads(
        golden_input("slide", "input.rmeta.json").read_text(encoding="utf-8")
    )
    fake_tika.set_response("input.pptx", elements)
    dummy = tmp_path / "input.pptx"
    dummy.write_bytes(b"PK-dummy")
    result = process_file(
        dummy, "input.pptx", ChunkingConfig(words=6, overlap=0), Settings(), tika_client
    )
    assert result.status == "ok"
    assert result.chunks == golden_chunks("slide")


def test_archive_is_unsupported(
    tmp_path: Path, fake_tika: FakeTika, tika_client: TikaClient
) -> None:
    result = process_file(
        tmp_path / "bundle.zip", "bundle.zip", ChunkingConfig(), Settings(), tika_client
    )
    assert result.status == "unsupported"
    assert fake_tika.calls == 0


def test_unknown_extension_without_sniffing(
    tmp_path: Path, fake_tika: FakeTika, tika_client: TikaClient
) -> None:
    result = process_file(
        tmp_path / "mystery.bin", "mystery.bin", ChunkingConfig(), Settings(), tika_client
    )
    assert result.status == "unsupported"
    assert fake_tika.calls == 0


def test_unknown_extension_with_sniffing(
    tmp_path: Path, fake_tika: FakeTika, tika_client: TikaClient
) -> None:
    fake_tika.set_response(
        "mystery.bin",
        [{"X-TIKA:content": "Some plain text content", "Content-Type": "text/plain"}],
    )
    dummy = tmp_path / "mystery.bin"
    dummy.write_bytes(b"???")
    result = process_file(
        dummy,
        "mystery.bin",
        ChunkingConfig(),
        Settings(tika_sniff_unknown=True),
        tika_client,
    )
    assert fake_tika.calls == 1
    assert result.status == "ok"
    assert result.format_class == "plaintext"


def test_scanned_pdf_is_no_text(
    tmp_path: Path, fake_tika: FakeTika, tika_client: TikaClient
) -> None:
    fake_tika.set_response(
        "scan.pdf",
        [
            {
                "X-TIKA:content": "scan",
                "Content-Type": "application/pdf",
                "xmpTPg:NPages": "3",
            }
        ],
    )
    dummy = tmp_path / "scan.pdf"
    dummy.write_bytes(b"%PDF-scan")
    result = process_file(dummy, "scan.pdf", ChunkingConfig(), Settings(), tika_client)
    assert result.status == "no_text"


def test_image_without_ocr_is_no_text(
    tmp_path: Path, fake_tika: FakeTika, tika_client: TikaClient
) -> None:
    fake_tika.set_response(
        "photo.png", [{"X-TIKA:content": "", "Content-Type": "image/png"}]
    )
    dummy = tmp_path / "photo.png"
    dummy.write_bytes(b"\x89PNG")
    result = process_file(dummy, "photo.png", ChunkingConfig(), Settings(), tika_client)
    assert result.status == "no_text"


def test_email_embedded_documents_pass_through(
    tmp_path: Path, fake_tika: FakeTika, tika_client: TikaClient
) -> None:
    fake_tika.set_response(
        "mail.eml",
        [
            {"X-TIKA:content": "Mail body text", "Content-Type": "message/rfc822"},
            {
                "X-TIKA:content": "Attached report text",
                "Content-Type": "application/pdf",
                "resourceName": "report.pdf",
            },
        ],
    )
    dummy = tmp_path / "mail.eml"
    dummy.write_bytes(b"From: a@example.com")
    result = process_file(dummy, "mail.eml", ChunkingConfig(), Settings(), tika_client)
    assert result.status == "ok"
    assert result.chunks == ["Mail body text"]
    assert len(result.embedded) == 1
    assert result.embedded[0].name == "report.pdf"
    assert result.embedded[0].text == "Attached report text"
