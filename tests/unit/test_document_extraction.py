import time

import pymupdf
import pytest

from tender_system.collection.documents import DocumentFailure, DownloadBudget, download, extract


def budget() -> DownloadBudget:
    return DownloadBudget(0, 0, 5, 100, 100, time.monotonic() + 10)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/file",
        "http://[::1]/file",
        "file:///etc/passwd",
        "http://user@localhost/file",
    ],
)
def test_download_rejects_private_or_invalid_targets(url: str) -> None:
    with pytest.raises(ValueError):
        download(url, {}, budget())


def test_extraction_rejects_unsupported_and_corrupt_files() -> None:
    with pytest.raises(DocumentFailure, match="unsupported"):
        extract(b"hello", "https://example.test/a.exe")
    with pytest.raises(pymupdf.FileDataError):
        extract(b"%PDF-broken", "https://example.test/a.pdf")
