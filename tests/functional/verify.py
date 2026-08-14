"""End-to-end driver for the functional-test stack.

Run from the repository root:

    uv run --project src python tests/functional/verify.py

Exits non-zero on the first failed assertion. On failure the stack stays up
for inspection; tear it down with `docker compose down -v` in this directory.
"""

import io
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

HERE = Path(__file__).parent
WORK = HERE / "work"
LOCAL = WORK / "local"
CONFIG_LIVE = HERE / "config-live"

INGEST = "http://localhost:18300"
QDRANT = "http://localhost:16333"
STUB = "http://localhost:18090"

API_HEADERS = {"Authorization": "Bearer functional-test-token"}
QDRANT_HEADERS = {"api-key": "functional-test-key"}
META_NAMESPACE = uuid.UUID("9e3a5c2f-8b7d-4f1e-a6b3-2d8c9e4f1a02")

_passed = 0


def check(condition: bool, message: str) -> None:
    global _passed
    if not condition:
        print(f"  FAIL  {message}")
        sys.exit(1)
    _passed += 1
    print(f"  ok    {message}")


def phase(title: str) -> None:
    print(f"\n== {title}")


# ── document generators ───────────────────────────────────────────────────────


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(lines: list[str]) -> bytes:
    stream = (
        "BT /F1 12 Tf 50 770 Td 16 TL "
        + "".join(f"({_pdf_escape(line)}) Tj T* " for line in lines)
        + "ET"
    )
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>" for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument'
        '/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def make_xlsx(rows: list[list[str]]) -> bytes:
    def cell_ref(row_index: int, col_index: int) -> str:
        return f"{chr(ord('A') + col_index)}{row_index + 1}"

    row_xml = "".join(
        f'<row r="{r + 1}">'
        + "".join(
            f'<c r="{cell_ref(r, c)}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            for c, value in enumerate(row)
        )
        + "</row>"
        for r, row in enumerate(rows)
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{row_xml}</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument'
        '/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument'
        '/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def write_test_documents() -> None:
    full = LOCAL / "full"
    append = LOCAL / "append"
    upsert = LOCAL / "upsert"
    for directory in (full, append, upsert):
        directory.mkdir(parents=True, exist_ok=True)

    (full / "sample.md").write_text(
        "# Functional Sample\n\nThe markdown document for the full job. "
        "It carries enough words to produce at least one chunk.\n",
        encoding="utf-8",
    )
    (full / "sample.pdf").write_bytes(
        make_pdf(
            [
                "The functional sample PDF for the full job.",
                "It contains two lines with plenty of characters so the",
                "scanned-page heuristic never triggers for this document.",
            ]
        )
    )
    (full / "sample.docx").write_bytes(
        make_docx(
            [
                "The functional sample DOCX for the full job.",
                "A second paragraph keeps the paragraph chunker honest.",
            ]
        )
    )
    (full / "sample.xlsx").write_bytes(
        make_xlsx(
            [
                ["name", "amount"],
                ["alpha", "1"],
                ["bravo", "2"],
                ["charlie", "3"],
            ]
        )
    )

    (append / "a1.md").write_text("# A1\n\nFirst appended document.\n", encoding="utf-8")
    (append / "a2.md").write_text("# A2\n\nSecond appended document.\n", encoding="utf-8")

    (upsert / "u1.md").write_text("# U1\n\nUpsert document one.\n", encoding="utf-8")
    (upsert / "u2.md").write_text("# U2\n\nUpsert document two.\n", encoding="utf-8")
    (upsert / "u3.md").write_text("# U3\n\nUpsert document three.\n", encoding="utf-8")
    (upsert / "u4.txt").write_text("Upsert plaintext document four.\n", encoding="utf-8")


# ── stack and API helpers ─────────────────────────────────────────────────────


def compose(*args: str, check_rc: bool = True) -> None:
    result = subprocess.run(["docker", "compose", *args], cwd=HERE)
    if check_rc and result.returncode != 0:
        print(f"docker compose {' '.join(args)} failed")
        sys.exit(1)


def wait_health(timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{INGEST}/health", timeout=3.0)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    print("service did not become healthy in time")
    sys.exit(1)


def api(method: str, path: str, **kwargs: object) -> httpx.Response:
    return httpx.request(
        method, f"{INGEST}{path}", headers=API_HEADERS, timeout=30.0, **kwargs  # type: ignore[arg-type]
    )


def run_job(job_id: str, expect: str = "success", body: dict | None = None) -> dict:
    response = api("POST", f"/v1/jobs/{job_id}/run", json=body or {})
    assert response.status_code == 202, f"trigger {job_id}: {response.status_code}"
    run_id = response.json()["run_id"]
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        detail = api("GET", f"/v1/runs/{run_id}").json()
        if detail["run"]["status"] != "running":
            run = detail["run"]
            if run["status"] != expect:
                print(f"  run {run_id} finished {run['status']}, expected {expect}")
                print(f"  error: {run.get('error')}")
                for event in detail.get("events", []):
                    print(f"    event: {event['level']} {event['message']}")
                sys.exit(1)
            return run
        time.sleep(0.3)
    print(f"run {run_id} did not finish")
    sys.exit(1)


def qdrant_count(collection: str, flt: dict | None = None) -> int:
    body: dict = {"exact": True}
    if flt is not None:
        body["filter"] = flt
    response = httpx.post(
        f"{QDRANT}/collections/{collection}/points/count",
        json=body,
        headers=QDRANT_HEADERS,
        timeout=10.0,
    )
    response.raise_for_status()
    return int(response.json()["result"]["count"])


def source_filter(source: str) -> dict:
    return {"must": [{"key": "source", "match": {"value": source}}]}


def qdrant_scroll(collection: str, flt: dict | None = None, limit: int = 100) -> list[dict]:
    body: dict = {"limit": limit, "with_payload": True}
    if flt is not None:
        body["filter"] = flt
    response = httpx.post(
        f"{QDRANT}/collections/{collection}/points/scroll",
        json=body,
        headers=QDRANT_HEADERS,
        timeout=10.0,
    )
    response.raise_for_status()
    return list(response.json()["result"]["points"])


def stub_stats() -> dict:
    return httpx.get(f"{STUB}/stats", timeout=5.0).json()


def stub_reset() -> None:
    httpx.post(f"{STUB}/stats/reset", timeout=5.0)


def touch(path: Path, seconds_forward: int = 5) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + seconds_forward * 10**9))


# ── phases ────────────────────────────────────────────────────────────────────


def phase_degraded_start() -> None:
    phase("Phase 1: startup without jobs.yaml is degraded but healthy")
    health = wait_health()
    check(health["status"] == "degraded", "health reports degraded")
    check("not found" in (health["config_error"] or ""), "config_error names jobs.yaml")
    check(health["jobs_loaded"] == 0, "zero jobs loaded")
    check(health["deps"]["qdrant"] is True, "qdrant dependency is up")
    check(health["deps"]["tika"] is True, "tika dependency is up")
    check(health["deps"]["embeddings"] is True, "embeddings dependency is up")


def phase_install_config() -> None:
    phase("Phase 2: config install, reload, and auth")
    unauthorized = httpx.get(f"{INGEST}/v1/jobs", timeout=10.0)
    check(unauthorized.status_code == 401, "missing bearer token yields 401")

    shutil.copyfile(HERE / "jobs.yaml", CONFIG_LIVE / "jobs.yaml")
    reloaded = api("POST", "/v1/config/reload").json()
    check(reloaded["valid"] is True, "catalog is valid after reload")
    jobs = api("GET", "/v1/jobs").json()
    check({job["id"] for job in jobs} == {"fx-full", "fx-append", "fx-upsert"}, "3 jobs loaded")
    health = wait_health()
    check(health["status"] == "ok", "health is ok with a valid catalog")


def phase_full_contract() -> None:
    phase("Phase 3: full run writes the payload and meta contract")
    run = run_job("fx-full")
    check(run["docs_indexed"] == 4, f"4 documents indexed (got {run['docs_indexed']})")
    check(run["sync_status"] == "skipped", "local source has no sync step")

    for name in ("sample.md", "sample.pdf", "sample.docx", "sample.xlsx"):
        source = f"local://fx-full/{name}"
        count = qdrant_count("fx-full", source_filter(source))
        check(count > 0, f"points exist for {name} ({count} chunks)")

    points = qdrant_scroll(
        "fx-full", source_filter("local://fx-full/sample.md"), limit=10
    )
    payload = points[0]["payload"]
    check(payload["ingest_job"] == "fx-full", "payload carries ingest_job")
    check(payload["ingest_run"] == run["run_id"], "payload carries the run id")
    check(payload["acl_tags"] == ["team:qa"], "payload carries acl_tags")
    check(payload["origin"] == "functional", "extra_payload is merged")
    check(payload["title"] == "Functional Sample", "markdown title extracted")
    check(payload["embedding_model"] == "stub-embed", "payload names the model")
    check("text" in payload and payload["text"], "payload carries text")

    info = httpx.get(
        f"{QDRANT}/collections/fx-full", headers=QDRANT_HEADERS, timeout=10.0
    ).json()["result"]
    schema = info.get("payload_schema", {})
    for field in ("source", "ingest_job", "ingest_run", "acl_tags"):
        check(field in schema, f"KEYWORD index on {field}")

    meta_id = str(uuid.uuid5(META_NAMESPACE, "fx-full"))
    meta = httpx.post(
        f"{QDRANT}/collections/_collection_meta/points",
        json={"ids": [meta_id], "with_payload": True},
        headers=QDRANT_HEADERS,
        timeout=10.0,
    ).json()["result"]
    check(len(meta) == 1, "meta point exists under the contract uuid5 id")
    check(meta[0]["payload"]["embedding_model"] == "stub-embed", "meta names the model")
    check(meta[0]["payload"]["vector_dimension"] == 768, "meta records the dimension")


def phase_change_detection() -> None:
    phase("Phase 4: four-stage change detection (stub counter as instrument)")
    run_job("fx-upsert")

    stub_reset()
    run = run_job("fx-upsert")
    stats = stub_stats()
    check(run["docs_unchanged"] == 4, "all documents unchanged on re-run")
    check(
        stats["texts"] == 1,
        f"unchanged re-run embeds only the dimension probe (got {stats['texts']})",
    )

    stub_reset()
    touch(LOCAL / "upsert" / "u1.md")
    run = run_job("fx-upsert")
    stats = stub_stats()
    check(run["docs_unchanged"] == 4, "touch-only change counts as unchanged")
    check(
        stats["texts"] == 1,
        f"touch-only change does not re-embed (got {stats['texts']})",
    )

    stub_reset()
    target = LOCAL / "upsert" / "u2.md"
    target.write_text("# U2\n\nUpsert document two, rewritten.\n", encoding="utf-8")
    touch(target)
    run = run_job("fx-upsert")
    stats = stub_stats()
    check(run["docs_indexed"] == 1, "content change re-indexes exactly one document")
    check(stats["texts"] == 2, f"one chunk plus the probe embedded (got {stats['texts']})")
    points = qdrant_scroll("fx-upsert", source_filter("local://fx-upsert/u2.md"))
    check("rewritten" in points[0]["payload"]["text"], "qdrant holds the new text")


def phase_generation_sweep() -> None:
    phase("Phase 5: the generation sweep removes deleted documents")
    (LOCAL / "full" / "sample.pdf").unlink()
    run = run_job("fx-full")
    check(run["docs_indexed"] == 3, "three documents re-indexed")
    gone = qdrant_count("fx-full", source_filter("local://fx-full/sample.pdf"))
    check(gone == 0, "deleted pdf swept from the collection")
    kept = qdrant_count("fx-full", source_filter("local://fx-full/sample.md"))
    check(kept > 0, "surviving documents keep their points")


def phase_upsert_guards() -> None:
    phase("Phase 6: vanished-source deletion and both guards")
    (LOCAL / "upsert" / "u1.md").unlink()  # 1 of 4 = 25% <= 50%
    run = run_job("fx-upsert")
    check(run["docs_deleted"] == 1, "one vanished source deleted")
    check(
        qdrant_count("fx-upsert", source_filter("local://fx-upsert/u1.md")) == 0,
        "vanished document's points are gone",
    )

    remaining = qdrant_count("fx-upsert")
    for name in ("u2.md", "u3.md", "u4.txt"):
        (LOCAL / "upsert" / name).unlink()
    run = run_job("fx-upsert", expect="aborted_guard")
    check("refusing" in (run["error"] or ""), "empty tree trips the empty_source_guard")
    check(qdrant_count("fx-upsert") == remaining, "guard abort deleted nothing")

    (LOCAL / "upsert" / "u2.md").write_text(
        "# U2\n\nUpsert document two, rewritten.\n", encoding="utf-8"
    )
    run = run_job("fx-upsert", expect="aborted_guard")  # 2 of 3 vanished = 67% > 50%
    check("max_delete_ratio" in (run["error"] or ""), "ratio guard names itself")
    check(qdrant_count("fx-upsert") == remaining, "ratio abort deleted nothing")

    (LOCAL / "upsert" / "u3.md").write_text(
        "# U3\n\nUpsert document three.\n", encoding="utf-8"
    )
    (LOCAL / "upsert" / "u4.txt").write_text(
        "Upsert plaintext document four.\n", encoding="utf-8"
    )
    run = run_job("fx-upsert")
    check(run["docs_deleted"] == 0, "restored tree deletes nothing")


def phase_append() -> None:
    phase("Phase 7: append is add-only and reports drift")
    run = run_job("fx-append")
    check(run["docs_indexed"] == 2, "initial append indexes both documents")

    stub_reset()
    (LOCAL / "append" / "a3.md").write_text(
        "# A3\n\nThird appended document.\n", encoding="utf-8"
    )
    run = run_job("fx-append")
    stats = stub_stats()
    check(run["docs_indexed"] == 1, "only the new document is indexed")
    check(stats["texts"] == 2, f"one chunk plus the probe embedded (got {stats['texts']})")

    stub_reset()
    target = LOCAL / "append" / "a1.md"
    target.write_text("# A1\n\nFirst appended document, rewritten.\n", encoding="utf-8")
    touch(target)
    run = run_job("fx-append")
    stats = stub_stats()
    check(run["docs_skipped_changed"] == 1, "drift is reported as skipped_changed")
    check(stats["texts"] == 1, "changed document is not re-embedded")
    points = qdrant_scroll("fx-append", source_filter("local://fx-append/a1.md"))
    check(
        "rewritten" not in points[0]["payload"]["text"],
        "append keeps the original content",
    )


def main() -> None:
    print("Resetting work directories")
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(CONFIG_LIVE, ignore_errors=True)
    CONFIG_LIVE.mkdir(parents=True)
    write_test_documents()

    print("Building the ingest image")
    compose("build", "ingest")
    print("Starting the stack")
    compose("up", "-d")

    phase_degraded_start()
    phase_install_config()
    phase_full_contract()
    phase_change_detection()
    phase_generation_sweep()
    phase_upsert_guards()
    phase_append()

    print(f"\nAll {_passed} checks passed. Tearing down.")
    compose("down", "-v")


if __name__ == "__main__":
    main()
