"""Bounded attachment download, extraction and per-tender persistence."""

import hashlib
import io
import ipaddress
import logging
import socket
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urljoin, urlsplit
from uuid import UUID

import httpx
import olefile  # type: ignore[import-untyped]
import openpyxl
import pymupdf
import xlrd
from docx import Document
from PIL import Image
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tender_system.core.execution import current_resource_limits, remaining_seconds
from tender_system.persistence.storage import OperationRow, TenderDocumentRow, TenderRow

log = logging.getLogger(__name__)
MAX_BYTES = 10_000_000
MAX_EXPANDED = 50_000_000
MAX_TEXT = 2_000_000
MAX_PAGES = 40
RECHECK = timedelta(hours=24)


class DocumentFailure(ValueError):
    """Safe diagnostic without document content or URLs."""


@dataclass
class DownloadBudget:
    requests: int
    bytes: int
    max_requests: int
    max_bytes: int
    max_response: int
    deadline: float


def download(
    url: str, headers: dict[str, str], budget: DownloadBudget
) -> tuple[int, bytes, dict[str, str]]:
    """Resolve only public addresses, pin the connection, and validate every redirect."""
    deadline = min(budget.deadline, time.monotonic() + 20)
    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        for _ in range(4):
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise DocumentFailure("invalid attachment URL")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
            ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
            if not ips or any(not ip.is_global for ip in ips):
                raise DocumentFailure("attachment host is not public")
            ip = str(ips[0])
            pinned = httpx.URL(url).copy_with(host=ip)
            host = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
            if ":" in parsed.hostname:
                host = f"[{parsed.hostname}]" + (f":{parsed.port}" if parsed.port else "")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("download deadline exceeded")
            if budget.requests >= budget.max_requests:
                raise DocumentFailure("request budget exceeded")
            budget.requests += 1
            request = client.build_request(
                "GET", pinned, headers={**headers, "Host": host}, timeout=remaining
            )
            request.extensions["sni_hostname"] = parsed.hostname
            with closing(client.send(request, stream=True)) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise DocumentFailure("redirect missing location")
                    url = urljoin(url, location)
                    continue
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise TimeoutError("download deadline exceeded")
                    body.extend(chunk)
                    budget.bytes += len(chunk)
                    if len(body) > budget.max_response or budget.bytes > budget.max_bytes:
                        raise DocumentFailure("attachment byte budget exceeded")
                return response.status_code, bytes(body), dict(response.headers)
    raise DocumentFailure("too many redirects")


def ocr(image: bytes, deadline: float) -> str:
    log.info("OCR started")
    # Limit pixels before handing untrusted image data to Tesseract.
    with Image.open(io.BytesIO(image)) as picture:
        if picture.width * picture.height > 25_000_000:
            raise DocumentFailure("image exceeds pixel limit")
    timeout = min(30.0, deadline - time.monotonic())
    if timeout <= 0:
        raise DocumentFailure("document processing deadline exceeded")
    languages = subprocess.run(
        ["tesseract", "--list-langs"],
        capture_output=True,
        text=True,
        timeout=min(5, timeout),
        check=True,
    ).stdout.splitlines()
    selected = "+".join(lang for lang in ("eng", "rus", "uzb") if lang in languages)
    if not selected:
        raise DocumentFailure("no supported Tesseract language data installed")
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", selected],
        input=image,
        capture_output=True,
        timeout=max(0.01, min(30.0, deadline - time.monotonic())),
        check=True,
    )
    text = result.stdout.decode("utf-8", errors="replace")
    log.info("OCR completed: %s characters", len(text))
    return text


def extract(data: bytes, url: str, content_type: str = "", *, deadline: float | None = None) -> str:
    deadline = deadline if deadline is not None else time.monotonic() + 120
    suffix = Path(unquote(urlsplit(url).path)).suffix.lower()
    ole_format = None
    if data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        with olefile.OleFileIO(io.BytesIO(data)) as compound:
            if compound.exists("WordDocument"):
                ole_format = "doc"
            elif compound.exists("Workbook") or compound.exists("Book"):
                ole_format = "xls"
    if data.startswith(b"%PDF-") or content_type.split(";")[0] == "application/pdf":
        with pymupdf.open(stream=data, filetype="pdf") as pdf:  # type: ignore[no-untyped-call]
            if pdf.is_encrypted or len(pdf) > MAX_PAGES:
                raise DocumentFailure("encrypted PDF or page limit exceeded")
            pages = []
            for page in pdf:
                value = page.get_text()
                # Digital headers must not suppress scanned procurement scope.
                images = page.get_image_info()
                image_area = sum(
                    float(info["bbox"][2] - info["bbox"][0])
                    * float(info["bbox"][3] - info["bbox"][1])
                    for info in images
                )
                if not value.strip() or (
                    images and (len(value.strip()) < 200 or image_area > page.rect.get_area() * 0.1)
                ):
                    if page.rect.width * page.rect.height > 5_000_000:
                        raise DocumentFailure("PDF page exceeds size limit")
                    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)  # type: ignore[no-untyped-call]
                    scanned = ocr(pixmap.tobytes("png"), deadline)
                    if not scanned.strip():
                        raise DocumentFailure("PDF page has no extractable evidence")
                    value = value + "\n" + scanned
                pages.append(value)
                if sum(map(len, pages)) > MAX_TEXT:
                    raise DocumentFailure("extracted text exceeds limit")
            return "\n".join(pages)
    if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        result = ocr(data, deadline)
    elif not data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")) and zipfile.is_zipfile(
        io.BytesIO(data)
    ):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if (
                sum(item.file_size for item in archive.infolist()) > MAX_EXPANDED
                or len(archive.infolist()) > 5000
            ):
                raise DocumentFailure("office archive exceeds expansion limit")
            names = set(archive.namelist())
        if "word/document.xml" in names:
            doc = Document(io.BytesIO(data))
            pieces = [p.text for p in doc.paragraphs]
            pieces.extend(
                cell.text for table in doc.tables for row in table.rows for cell in row.cells
            )
            for section in doc.sections:
                for part in (
                    section.header,
                    section.first_page_header,
                    section.even_page_header,
                    section.footer,
                    section.first_page_footer,
                    section.even_page_footer,
                ):
                    pieces.extend(p.text for p in part.paragraphs)
                    pieces.extend(
                        cell.text
                        for table in part.tables
                        for row in table.rows
                        for cell in row.cells
                    )
            result = "\n".join(pieces)
        elif "xl/workbook.xml" in names:
            workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            try:
                pieces = []
                text_length = 0
                for sheet in workbook:
                    if (
                        sheet.max_row
                        and sheet.max_column
                        and sheet.max_row * sheet.max_column > 1_000_000
                    ):
                        raise DocumentFailure("spreadsheet exceeds cell limit")
                    for row in sheet.iter_rows(values_only=True):
                        line = "\t".join(str(cell) for cell in row if cell is not None)
                        pieces.append(line)
                        text_length += len(line)
                        if text_length > MAX_TEXT or len(pieces) > 100_000:
                            raise DocumentFailure("spreadsheet exceeds extraction limit")
                result = "\n".join(pieces)
            finally:
                workbook.close()
        else:
            raise DocumentFailure("unsupported office archive")
    elif ole_format == "xls":
        legacy_book = xlrd.open_workbook(file_contents=data, on_demand=True)
        pieces = []
        try:
            for legacy_sheet in legacy_book.sheets():
                if (
                    legacy_sheet.nrows > 100_000
                    or legacy_sheet.nrows * legacy_sheet.ncols > 1_000_000
                ):
                    raise DocumentFailure("spreadsheet exceeds extraction limit")
                pieces.extend(
                    "\t".join(str(v) for v in legacy_sheet.row_values(i) if v != "")
                    for i in range(legacy_sheet.nrows)
                )
            result = "\n".join(pieces)
        finally:
            legacy_book.release_resources()
    elif ole_format == "doc":
        with tempfile.TemporaryDirectory(prefix="quarry-doc-") as directory:
            path = Path(directory) / "document.doc"
            path.write_bytes(data)
            output = subprocess.run(
                ["antiword", "-m", "UTF-8.txt", str(path)],
                capture_output=True,
                timeout=max(0.01, min(30.0, deadline - time.monotonic())),
                check=True,
            )
            result = output.stdout.decode("utf-8", errors="replace")
    elif suffix == ".txt" or content_type.split(";")[0] == "text/plain":
        result = data.decode("utf-8-sig")
    else:
        raise DocumentFailure("unsupported document format")
    if len(result) > MAX_TEXT:
        raise DocumentFailure("extracted text exceeds limit")
    return result


class DocumentProcessor:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        fetch: Callable[
            [str, dict[str, str], DownloadBudget], tuple[int, bytes, dict[str, str]]
        ] = download,
    ) -> None:
        self.sessions = sessions
        self.fetch = fetch

    def process(
        self,
        tender_ids: tuple[UUID, ...],
        requests_used: int = 0,
        bytes_used: int = 0,
        *,
        run_id: UUID | None = None,
        document_urls: dict[str, list[str]] | None = None,
    ) -> dict[str, int]:
        limits = current_resource_limits()
        try:
            remaining = remaining_seconds("document processing")
        except TimeoutError:
            remaining = 0.0
        duration = min(
            120.0, limits.max_duration_seconds, remaining if remaining is not None else 120.0
        )
        budget = DownloadBudget(
            requests_used,
            bytes_used,
            limits.max_requests,
            limits.max_total_response_bytes,
            min(MAX_BYTES, limits.max_response_bytes),
            time.monotonic() + duration,
        )
        with self.sessions() as session:
            rows = session.execute(
                select(TenderRow.id, TenderRow.document_urls).where(TenderRow.id.in_(tender_ids))
            ).all()
        failed_to_persist = False
        outcomes = {"succeeded": 0, "failed": 0, "reused": 0}
        for tender_id, urls in rows:
            if document_urls is not None:
                urls = document_urls.get(str(tender_id), [])
            for url in dict.fromkeys(urls):
                try:
                    outcomes[self._process_one(tender_id, url, budget, run_id)] += 1
                except Exception:
                    log.exception("Could not persist document result for tender %s", tender_id)
                    failed_to_persist = True
        if failed_to_persist:
            raise DocumentFailure("could not persist document processing results")
        return outcomes

    def _process_one(
        self,
        tender_id: UUID,
        url: str,
        budget: DownloadBudget,
        run_id: UUID | None = None,
        document_urls: dict[str, list[str]] | None = None,
    ) -> str:
        now = datetime.now(UTC)
        with self.sessions() as session:
            existing = session.get(TenderDocumentRow, (tender_id, url))
            if run_id is not None:
                operation = session.get(OperationRow, run_id)
                token = hashlib.sha256(f"{tender_id}/{url}".encode()).hexdigest()
                recorded = (
                    cast(dict[str, JsonValue], operation.result.get("documents", {}))
                    if operation
                    else {}
                )
                if token in recorded:
                    return "reused"
            if (
                existing
                and existing.checked_at > now - RECHECK
                and (existing.status != "success" or (existing.extracted_text or "").strip())
            ):
                if run_id is not None:
                    with self.sessions.begin() as write:
                        operation = write.get(OperationRow, run_id, with_for_update=True)
                        if operation:
                            operation.result = {
                                **operation.result,
                                "documents": {
                                    **cast(
                                        dict[str, JsonValue], operation.result.get("documents", {})
                                    ),
                                    token: {"status": existing.status, "reused": True},
                                },
                            }
                return "reused"
            old = {
                key: getattr(existing, key) if existing else None
                for key in ("status", "extracted_text", "etag", "last_modified", "content_hash")
            }
        headers = {}
        if old["status"] == "success" and (old["extracted_text"] or "").strip():
            if old["etag"]:
                headers["If-None-Match"] = old["etag"]
            if old["last_modified"]:
                headers["If-Modified-Since"] = old["last_modified"]
        values = dict(old)
        log.info("Document processing started for tender %s", tender_id)
        try:
            if time.monotonic() >= budget.deadline:
                raise DocumentFailure("document processing deadline exceeded")
            status, body, response_headers = self.fetch(url, headers, budget)
            if status == 304 and old["status"] == "success":
                pass
            elif status == 200:
                digest = hashlib.sha256(body).hexdigest()
                if (
                    digest != old["content_hash"]
                    or old["status"] != "success"
                    or not (old["extracted_text"] or "").strip()
                ):
                    values["extracted_text"] = extract(
                        body,
                        url,
                        response_headers.get("content-type", ""),
                        deadline=budget.deadline,
                    )
                values.update(
                    status="success",
                    content_hash=digest,
                    etag=response_headers.get("etag"),
                    last_modified=response_headers.get("last-modified"),
                )
            else:
                raise DocumentFailure(f"HTTP status {status}")
            if not (values.get("extracted_text") or "").strip():
                raise DocumentFailure("document has no extractable evidence")
            values["error"] = None
        except Exception as error:
            # Do not persist exception strings that may contain signed URLs or secrets.
            detail = str(error) if isinstance(error, DocumentFailure) else type(error).__name__
            values.update(status="failed", error=detail)
            log.warning(
                "Document processing failed for tender %s: %s", tender_id, type(error).__name__
            )
        values["checked_at"] = now
        with self.sessions.begin() as session:
            row = session.get(TenderDocumentRow, (tender_id, url))
            if row is None:
                row = TenderDocumentRow(tender_id=tender_id, url=url, **values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            if run_id is not None:
                operation = session.get(OperationRow, run_id, with_for_update=True)
                if operation:
                    token = hashlib.sha256(f"{tender_id}/{url}".encode()).hexdigest()
                    operation.result = {
                        **operation.result,
                        "documents": {
                            **cast(dict[str, JsonValue], operation.result.get("documents", {})),
                            token: {"status": values["status"], "reused": False},
                        },
                    }
        log.info("Document processing %s for tender %s", values["status"], tender_id)
        return "succeeded" if values["status"] == "success" else "failed"
