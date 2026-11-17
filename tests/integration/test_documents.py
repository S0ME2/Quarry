# mypy: disable-error-code=no-untyped-call
"""Real collection, HTTP download, extraction/OCR and PostgreSQL persistence."""

import io
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import openpyxl
import pymupdf
import pytest
import xlwt  # type: ignore[import-untyped]
from docx import Document
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import Engine, select

from tender_system.collection import documents
from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.recipes import RecipeExecutor
from tender_system.collection.source_runs import SourceRunService
from tender_system.core.domain import SourceRecipe, SourceRecord
from tender_system.persistence.storage import (
    SqlSourceRepository,
    SqlTenderRepository,
    TenderDocumentRow,
    TenderVersionRow,
    TransactionalRunStore,
    session_factory,
)
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.fakes import ScriptedHttpAdapter

pytestmark = pytest.mark.database


def fixtures() -> dict[str, bytes]:
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "TEXT PDF PROCUREMENT")
    text_pdf = pdf.tobytes()
    pdf.close()
    image = Image.new("RGB", (1000, 250), "white")
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 55)
    ImageDraw.Draw(image).text((30, 60), "SCANNED TENDER", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    png = buffer.getvalue()
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    jpeg = buffer.getvalue()
    pdf = pymupdf.open()
    page = pdf.new_page(width=1000, height=250)
    page.insert_image(page.rect, stream=png)
    scan = pdf.tobytes()
    pdf.close()
    doc = Document()
    doc.add_paragraph("WORD PROCUREMENT")
    buffer = io.BytesIO()
    doc.save(buffer)
    docx = buffer.getvalue()
    workbook = openpyxl.Workbook()
    sheet = workbook.create_sheet()
    sheet["A1"] = "EXCEL PROCUREMENT"
    buffer = io.BytesIO()
    workbook.save(buffer)
    xlsx = buffer.getvalue()
    legacy = xlwt.Workbook()
    legacy.add_sheet("Sheet1").write(0, 0, "LEGACY XLS PROCUREMENT")
    buffer = io.BytesIO()
    legacy.save(buffer)
    return {
        "text.pdf": text_pdf,
        "scan.pdf": scan,
        "image.png": png,
        "image.jpg": jpeg,
        "word.docx": docx,
        "excel.xlsx": xlsx,
        "legacy-xls": buffer.getvalue(),
        "notes.txt": b"TEXT ATTACHMENT",
        "bad.pdf": b"%PDF-broken",
    }


def test_collection_download_ocr_persistence_and_reuse(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="tender_system.collection.documents")
    files = fixtures()
    if shutil.which("antiword"):
        files["legacy-doc"] = Path("tests/fixtures/sample-legacy.doc").read_bytes()
    hits: list[tuple[str, str | None]] = []
    tags = {name: '"fixture"' for name in files}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            name = self.path.lstrip("/")
            hits.append((name, self.headers.get("If-None-Match")))
            if name == "missing.pdf":
                self.send_response(404)
                self.end_headers()
                return
            if self.headers.get("If-None-Match") == tags[name]:
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("ETag", tags[name])
            self.end_headers()
            self.wfile.write(files[name])

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Only this local fixture may bypass the public-address check; HTTP remains real.
    local_ip = SimpleNamespace(ip_address=lambda ip: PublicLocal())
    monkeypatch.setattr(
        documents,
        "ipaddress",
        local_ip,
    )

    class PublicLocal:
        is_global = True

        def __str__(self) -> str:
            return "127.0.0.1"

    origin = f"http://127.0.0.1:{server.server_port}"
    urls = [f"{origin}/{name}" for name in files] + [f"{origin}/missing.pdf"]
    rows = [
        {"external_tender_id": "one", "title": "Equipment", "document_urls": urls},
        {"external_tender_id": "two", "title": "Other", "document_urls": [f"{origin}/notes.txt"]},
    ]
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "docs",
            "version": "1",
            "completion_signal": {"kind": "total_reconciled", "value": 2},
            "expected_count": 2,
            "steps": [
                {
                    "type": "transform",
                    "parameters": {"name": "identity", "input": rows, "emit": True},
                }
            ],
        }
    )
    source = SourceRecord(id="docs", name="Documents")
    sessions = session_factory(migrated_engine)
    settings = fixture_source_settings()
    with sessions.begin() as session:
        SqlSourceRepository(session).add(source)
    store = TransactionalRunStore(sessions)
    service = SourceRunService(
        RecipeExecutor(ScriptedHttpAdapter(()), MemoryEvidenceStore(), settings),
        ConfigurableTenderNormalizer(),
        store,
    )
    try:
        receipt = service.run(uuid4(), recipe, source, settings)
        assert receipt.status == "accepted"
        assert sum(record.message == "OCR started" for record in caplog.records) == 3
        assert sum(record.message.startswith("OCR completed") for record in caplog.records) == 3
        assert store.document_outcomes == {
            "succeeded": len(files),
            "failed": 2,
            "reused": 0,
        }
        with sessions() as session:
            results = session.scalars(select(TenderDocumentRow)).all()
            assert len(results) == len(files) + 2
            assert (
                len(SqlTenderRepository(session).documents(receipt.tender_ids[0])) == len(files) + 1
            )
            one = {
                r.url.rsplit("/", 1)[-1]: r for r in results if r.tender_id == receipt.tender_ids[0]
            }
            assert "TEXT PDF PROCUREMENT" in (one["text.pdf"].extracted_text or "")
            assert "SCANNED TENDER" in (one["scan.pdf"].extracted_text or "")
            assert "SCANNED TENDER" in (one["image.png"].extracted_text or "")
            assert "SCANNED TENDER" in (one["image.jpg"].extracted_text or "")
            assert "WORD PROCUREMENT" in (one["word.docx"].extracted_text or "")
            assert "EXCEL PROCUREMENT" in (one["excel.xlsx"].extracted_text or "")
            assert "LEGACY XLS PROCUREMENT" in (one["legacy-xls"].extracted_text or "")
            if "legacy-doc" in files:
                assert "I am a test document" in (one["legacy-doc"].extracted_text or "")
            assert "TEXT ATTACHMENT" in (one["notes.txt"].extracted_text or "")
            assert one["bad.pdf"].status == one["missing.pdf"].status == "failed"
            assert one["bad.pdf"].error and one["missing.pdf"].error
            assert len(session.scalars(select(TenderVersionRow)).all()) == 2
        previous_hits = len(hits)
        receipt2 = service.run(uuid4(), recipe, source, settings)
        assert receipt2.status == "accepted"
        assert store.document_outcomes == {
            "succeeded": 0,
            "failed": 0,
            "reused": len(files) + 2,
        }
        assert len(hits) == previous_hits
        with sessions.begin() as session:
            row = session.get(TenderDocumentRow, (receipt.tender_ids[0], f"{origin}/text.pdf"))
            assert row is not None
            row.checked_at = datetime.now(UTC) - timedelta(days=2)
        service.run(uuid4(), recipe, source, settings)
        assert hits[-1] == ("text.pdf", '"fixture"')
        with sessions() as session:
            row = session.get(TenderDocumentRow, (receipt.tender_ids[0], f"{origin}/text.pdf"))
            assert row is not None
            assert row.status == "success" and "TEXT PDF PROCUREMENT" in (row.extracted_text or "")
            assert len(session.scalars(select(TenderVersionRow)).all()) == 2
        pdf = pymupdf.open()
        pdf.new_page().insert_text((72, 72), "UPDATED PDF CONTENT")
        files["text.pdf"] = pdf.tobytes()
        pdf.close()
        tags["text.pdf"] = '"changed"'
        with sessions.begin() as session:
            row = session.get(TenderDocumentRow, (receipt.tender_ids[0], f"{origin}/text.pdf"))
            assert row is not None
            row.checked_at = datetime.now(UTC) - timedelta(days=2)
        service.run(uuid4(), recipe, source, settings)
        with sessions() as session:
            row = session.get(TenderDocumentRow, (receipt.tender_ids[0], f"{origin}/text.pdf"))
            assert row is not None and "UPDATED PDF CONTENT" in (row.extracted_text or "")
            assert len(session.scalars(select(TenderVersionRow)).all()) == 2
        budget = documents.DownloadBudget(0, 0, 1, 5, 5, time.monotonic() + 10)
        with pytest.raises(documents.DocumentFailure, match="byte budget"):
            documents.download(f"{origin}/notes.txt", {}, budget)
        with pytest.raises(documents.DocumentFailure, match="request budget"):
            documents.download(f"{origin}/notes.txt", {}, budget)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "kind",
    [
        "body",
        "header",
        "footer",
        "table",
        "digital",
        "scan",
        "mixed",
        "empty",
        "relative",
        "malformed",
        "relevant",
        "download_failed",
        "ocr_failed",
    ],
)
def test_attachment_evidence_survives_publication_and_matching(
    migrated_engine: Engine, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tender_system.core.configuration import CampaignSettings
    from tender_system.core.domain import CampaignCriteria
    from tender_system.matching.intelligent_matching import IntelligentMatcher
    from tender_system.testing.fakes import FakeClock, ScriptedModelClient

    scope = "Supply 200 medical ventilators" if kind != "relevant" else "Supply office furniture"
    url = "https://example.com/spec.docx"
    if kind in {"digital", "scan", "mixed", "ocr_failed"}:
        url = "https://example.com/spec.pdf"
        pdf = pymupdf.open()
        page = pdf.new_page(width=1000, height=400)
        if kind != "digital":
            image = Image.new("RGB", (1000, 250), "white")
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40)
            ImageDraw.Draw(image).text((20, 70), scope, fill="black", font=font)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            page.insert_image(pymupdf.Rect(0, 100, 1000, 350), stream=buffer.getvalue())
        if kind != "scan":
            page.insert_text((30, 50), scope if kind == "digital" else "Tender Document")
        data = pdf.tobytes()
        pdf.close()
    else:
        doc = Document()
        if kind in {"body", "relevant"}:
            doc.add_paragraph(scope)
        elif kind in {"header", "footer"}:
            getattr(doc.sections[0], kind).paragraphs[0].text = scope
        elif kind == "table":
            doc.add_table(rows=1, cols=1).cell(0, 0).text = scope
        buffer = io.BytesIO()
        doc.save(buffer)
        data = buffer.getvalue()
    sessions = session_factory(migrated_engine)
    source = SourceRecord(id="audit-doc", name="Audit documents")
    with sessions.begin() as session:
        SqlSourceRepository(session).add(source)
    if kind == "ocr_failed":

        def failed_ocr(_data: bytes, _deadline: float) -> str:
            raise documents.DocumentFailure("OCR unavailable")

        monkeypatch.setattr(documents, "ocr", failed_ocr)

    def fetch_attachment(
        _url: str, headers: dict[str, str], _budget: documents.DownloadBudget
    ) -> tuple[int, bytes, dict[str, str]]:
        if headers.get("If-None-Match") == "fixture":
            return 304, b"", {}
        return (404 if kind == "download_failed" else 200), data, {"etag": "fixture"}

    processor = documents.DocumentProcessor(sessions, fetch=fetch_attachment)
    store = TransactionalRunStore(sessions, document_processor=processor)
    settings = fixture_source_settings().model_copy(
        update={
            "health": fixture_source_settings().health.model_copy(
                update={"reject_quality_issues": False}
            ),
            "normalization": fixture_source_settings().normalization.model_copy(
                update={"parse_failure_policy": "null_with_issue"}
            ),
        }
    )
    attachments: list[object] = (
        [{"url": "/spec.pdf"}]
        if kind == "relative"
        else [{"missing": "url"}]
        if kind == "malformed"
        else [url]
    )
    recipe = SourceRecipe.model_validate(
        {
            "source_id": source.id,
            "version": "1",
            "completion_signal": {"kind": "total_reconciled", "value": 1},
            "expected_count": 1,
            "steps": [
                {
                    "type": "transform",
                    "parameters": {
                        "name": "identity",
                        "input": [
                            {
                                "external_tender_id": "1",
                                "title": "General equipment",
                                "document_urls": attachments,
                            }
                        ],
                        "emit": True,
                    },
                }
            ],
        }
    )
    receipt = SourceRunService(
        RecipeExecutor(ScriptedHttpAdapter(()), MemoryEvidenceStore(), settings),
        ConfigurableTenderNormalizer(),
        store,
    ).run(uuid4(), recipe, source, settings)
    assert receipt.status == "accepted"
    if kind == "header":
        # Repair historical success-with-empty-text without waiting a day or trusting 304.
        with sessions.begin() as session:
            stored = session.get(TenderDocumentRow, (receipt.tender_ids[0], url))
            assert stored is not None
            stored.extracted_text = ""
        with sessions() as session:
            previous = SqlTenderRepository(session).get(receipt.tender_ids[0])
            assert previous is not None and previous.documents_incomplete
        assert processor.process(receipt.tender_ids)["succeeded"] == 1
    with sessions() as session:
        tender = SqlTenderRepository(session).get(receipt.tender_ids[0])
        assert tender is not None
        incomplete = kind in {"empty", "relative", "malformed", "download_failed", "ocr_failed"}
        assert tender.documents_incomplete is incomplete
        if kind in {"relative", "malformed"}:
            assert any(issue.field == "document_urls" for issue in tender.quality_issues)
        elif not incomplete:
            assert scope.casefold() in tender.document_text.casefold()
        model = ScriptedModelClient(())
        matcher = IntelligentMatcher(
            CampaignSettings(matcher_adapter="intelligent", matcher_version="3"),
            FakeClock(datetime.now(UTC)),
            lambda _: model,
        )
        result = matcher.match(
            tender,
            CampaignCriteria(name="Exclude medical", exclude_keywords=("medical ventilators",)),
        )
        assert result.result == (
            "unsure" if incomplete else "match" if kind == "relevant" else "no_match"
        )
        assert not model.requests
