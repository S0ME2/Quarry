from datetime import UTC, datetime
from uuid import uuid4

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.verification import BrowserWitnesses
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    CollectionRun,
    SourceRecord,
    TenderCandidate,
)
from tender_system.testing.fakes import FakeBrowserAdapter, FakePage


def test_browser_witness_waits_for_rendered_identity_and_accepts_item_description() -> None:
    url = "https://fixture.test/lot/123"
    title = "A listing title not repeated on the rendered detail page"
    description = "Product detail populated after the initial page load"
    browser = FakeBrowserAdapter(
        {url: FakePage(html=f"<main><p>{description}</p><span>123</span></main>")}
    )
    now = datetime.now(UTC)
    candidate = TenderCandidate(
        source_id="fixture",
        external_tender_id="123",
        source_url=url,
        title=title,
        description=description,
    )
    run = CollectionRun(
        id=uuid4(),
        source_id="fixture",
        recipe_version="1",
        started_at=now,
        finished_at=now,
        candidates=(candidate,),
        complete=True,
        evidence_reference="evidence",
    )

    result = BrowserWitnesses(lambda: browser, MemoryEvidenceStore()).verify(
        run,
        SourceRecord(id="fixture", name="Fixture"),
        SourceSettings(),
    )

    assert result.checked == 1
    assert result.mismatches == ()
    assert browser.events == [
        ("navigate", url),
        (
            "wait_for_selector",
            'body:has-text("A listing title not repeated on the rendered detail page"), '
            'body:has-text("Product detail populated after the initial page load")',
        ),
    ]


def test_browser_witness_rejects_identity_without_matching_title_or_description() -> None:
    url = "https://fixture.test/lot/123"
    browser = FakeBrowserAdapter({url: FakePage(html="<main><span>123</span></main>")})
    now = datetime.now(UTC)
    candidate = TenderCandidate(
        source_id="fixture",
        external_tender_id="123",
        source_url=url,
        title="Expected title",
        description="Expected product detail",
    )
    run = CollectionRun(
        id=uuid4(),
        source_id="fixture",
        recipe_version="1",
        started_at=now,
        finished_at=now,
        candidates=(candidate,),
        complete=True,
        evidence_reference="evidence",
    )

    result = BrowserWitnesses(lambda: browser, MemoryEvidenceStore()).verify(
        run,
        SourceRecord(id="fixture", name="Fixture"),
        SourceSettings(),
    )

    assert result.checked == 0
    assert len(result.mismatches) == 1


def test_browser_witness_timeout_is_actionable_and_has_diagnostic_evidence() -> None:
    url = "https://fixture.test/lot/123"
    browser = FakeBrowserAdapter(
        {url: FakePage(html="<main>detail</main>")}, failures={"navigate": TimeoutError()}
    )
    evidence = MemoryEvidenceStore()
    now = datetime.now(UTC)
    candidate = TenderCandidate(
        source_id="fixture",
        external_tender_id="123",
        source_url=url,
        title="Expected title",
    )
    run = CollectionRun(
        id=uuid4(),
        source_id="fixture",
        recipe_version="1",
        started_at=now,
        finished_at=now,
        candidates=(candidate,),
        complete=True,
        evidence_reference="evidence",
    )

    result = BrowserWitnesses(lambda: browser, evidence).verify(
        run, SourceRecord(id="fixture", name="Fixture"), SourceSettings()
    )

    assert result.checked == 0
    assert len(result.mismatches) == 1
    assert "timed out during navigation" in result.mismatches[0].detail
    assert result.mismatches[0].code == "sample_timeout"
    assert len(result.evidence_references) == 1
    failure = evidence.get(result.evidence_references[0])
    assert failure["kind"] == "independent_detail_witness_failure"
    assert failure["phase"] == "navigation"
    assert failure["error_type"] == "TimeoutError"


def test_rendered_content_timeout_records_selector_and_playwright_error() -> None:
    url = "https://fixture.test/lot/123"
    browser = FakeBrowserAdapter(
        {url: FakePage(html="<main>detail</main>")},
        failures={"wait_for_selector": TimeoutError("selector timed out")},
    )
    evidence = MemoryEvidenceStore()
    now = datetime.now(UTC)
    candidate = TenderCandidate(
        source_id="fixture",
        external_tender_id="123",
        source_url=url,
        title="Expected title",
    )
    run = CollectionRun(
        id=uuid4(),
        source_id="fixture",
        recipe_version="1",
        started_at=now,
        finished_at=now,
        candidates=(candidate,),
        complete=True,
        evidence_reference="evidence",
    )

    result = BrowserWitnesses(lambda: browser, evidence).verify(
        run, SourceRecord(id="fixture", name="Fixture"), SourceSettings()
    )

    failure = evidence.get(result.evidence_references[0])
    assert failure["phase"] == "rendered tender content"
    assert failure["selector"] == 'body:has-text("Expected title")'
    assert "exception text withheld" in str(failure["error"])


def test_browser_witness_uses_rendered_content_and_resolved_url_for_identity() -> None:
    url = "https://fixture.test/tender/slug-123"
    browser = FakeBrowserAdapter(
        {url: FakePage(html="<main><h1>Verified tender title</h1></main>")}
    )
    evidence = MemoryEvidenceStore()
    now = datetime.now(UTC)
    candidate = TenderCandidate(
        source_id="fixture",
        external_tender_id="123",
        source_url=url,
        title="Verified tender title",
    )
    run = CollectionRun(
        id=uuid4(),
        source_id="fixture",
        recipe_version="1",
        started_at=now,
        finished_at=now,
        candidates=(candidate,),
        complete=True,
        evidence_reference="evidence",
    )

    result = BrowserWitnesses(lambda: browser, evidence).verify(
        run, SourceRecord(id="fixture", name="Fixture"), SourceSettings()
    )

    assert result.checked == 1
    assert result.mismatches == ()
    assert "123" not in str(evidence.get(result.evidence_references[0])["content"])


def test_browser_witness_rejects_matching_text_at_wrong_tender_url() -> None:
    url = "https://fixture.test/tender/other"
    browser = FakeBrowserAdapter({url: FakePage(html="<h1>Verified tender title</h1>")})
    now = datetime.now(UTC)
    run = CollectionRun(
        id=uuid4(),
        source_id="fixture",
        recipe_version="1",
        started_at=now,
        finished_at=now,
        candidates=(
            TenderCandidate(
                source_id="fixture",
                external_tender_id="123",
                source_url=url,
                title="Verified tender title",
            ),
        ),
        complete=True,
        evidence_reference="evidence",
    )

    result = BrowserWitnesses(lambda: browser, MemoryEvidenceStore()).verify(
        run, SourceRecord(id="fixture", name="Fixture"), SourceSettings()
    )

    assert result.checked == 0
    assert result.mismatches[0].code == "sample_mismatch"


def test_witness_request_limit_failure_names_setting_and_keeps_guard() -> None:
    from tender_system.core.execution import RequestBudgetExceeded

    url = "https://fixture.test/lot/123"
    browser = FakeBrowserAdapter(
        {url: FakePage(html="<h1>Equipment</h1>")},
        failures={"navigate": RequestBudgetExceeded(100, 101)},
    )
    now = datetime.now(UTC)
    result = BrowserWitnesses(lambda: browser, MemoryEvidenceStore()).verify(
        CollectionRun(
            id=uuid4(),
            source_id="fixture",
            recipe_version="1",
            started_at=now,
            finished_at=now,
            complete=False,
            evidence_reference="fixture",
            candidates=(
                TenderCandidate(
                    source_id="fixture", external_tender_id="123", title="Equipment", source_url=url
                ),
            ),
        ),
        SourceRecord(id="fixture", name="Fixture"),
        SourceSettings(),
    )
    assert result.checked == 0
    assert result.mismatches[0].code == "request_budget_exceeded"
    assert "max_requests" in result.mismatches[0].detail
    assert "101/100" in result.mismatches[0].detail
