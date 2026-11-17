import json
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import Engine
from test_icppa import (
    FIXTURES,
    SOURCE,
    Collector,
    IcppaFixture,
    execute,
    policy,
)

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.core.domain import CollectionRun, SourceFailure
from tender_system.persistence.storage import SqlTenderRepository


def enrichment_fixture() -> IcppaFixture:
    fixture = IcppaFixture()
    cases = TypeAdapter(list[dict[str, JsonValue]]).validate_json(
        (FIXTURES / "enrichment-cases.json").read_text()
    )
    fixture.rows = TypeAdapter(list[dict[str, JsonValue]]).validate_python(
        [c["listing"] for c in cases]
    )
    details = TypeAdapter(list[dict[str, JsonValue]]).validate_python([c["detail"] for c in cases])
    operators = TypeAdapter(list[dict[str, JsonValue]]).validate_python(
        [c["operator"] for c in cases]
    )
    fixture.details = {str(c["lotId"]): c for c in details}
    fixture.operators = {str(c["id"]): c for c in operators}
    fixture.title_rows = {
        str(operator["id"]): TypeAdapter(list[dict[str, JsonValue]]).validate_python(
            case["title_rows"]
        )
        for operator, case in zip(operators, cases, strict=True)
    }
    return fixture


def test_all_nine_observed_content_gaps_recover_without_fabricating_titles() -> None:
    fixture = enrichment_fixture()
    run = execute(fixture, MemoryEvidenceStore())
    assert isinstance(run, CollectionRun)
    candidates = [
        ConfigurableTenderNormalizer().normalize(raw, SOURCE, policy()).candidate
        for raw in run.raw_observations
    ]
    assert all(c is not None for c in candidates)
    missing = candidates[:9]
    assert len(missing) == 9
    for index, candidate in enumerate(missing):
        assert candidate is not None
        assert (candidate.title is None) == (index >= 7)
        assert candidate.description
        listing = candidate.raw_data["listing"]
        assert isinstance(listing, dict) and listing["enktNames"] in {None, ""}
        assert candidate.deadline is not None and candidate.published_at is None
        assert candidate.raw_data["operator"] and candidate.raw_data["operator_products"]
        products = candidate.raw_data["operator_products"]
        assert isinstance(products, list) and products
        first = products[0]
        assert isinstance(first, dict)
        assert str(first["Product_Name"]) in candidate.description
    plasma = next(c for c in missing if c and c.external_tender_id == "26111006512875")
    assert plasma and "донорской плазмы" in (plasma.description or "")
    assert plasma.deadline and plasma.deadline.isoformat() == "2026-10-12T11:40:39+00:00"
    pending, cancelled, usd, eur = candidates[9:]
    assert pending and pending.deadline is None and pending.status == "draft"
    assert cancelled and cancelled.deadline is None and cancelled.status == "cancelled"
    assert usd and usd.currency == "USD"
    assert eur and eur.currency == "EUR"


def test_public_attachment_witness_agrees_with_structured_enrichment() -> None:
    witness = json.loads((FIXTURES / "attachment-protocol.json").read_text())
    fixture = enrichment_fixture()
    operator = fixture.operators[witness["operator_id"]]
    assert operator["tech_file_path"] == witness["responses"][0]["path"]
    assert witness["request"]["requires_auth"] is False
    pdf = witness["sanitized_procurement_pdf_witness"]
    products = json.loads(str(operator["budget_products"]))
    assert products[0]["Product_Name"] == pdf["object"]
    assert operator["start_date"] == "2026-09-23T16:40:39"
    assert operator["end_date"] == "2026-10-12T16:40:39"
    assert pdf["publication_time_proven"] is False


def test_title_search_requires_exact_id_and_preserves_null_when_unavailable() -> None:
    fixture = enrichment_fixture()
    fixture.title_rows["515078"] = [{"id": 42, "name": "Unrelated announcement"}]
    run = execute(fixture, MemoryEvidenceStore())
    assert isinstance(run, CollectionRun)
    normalizer = ConfigurableTenderNormalizer()
    candidate = normalizer.normalize(run.raw_observations[0], SOURCE, policy()).candidate
    assert candidate and candidate.title is None
    assert run.raw_observations[0]["description"]
    # Older closed records are absent from the observed operator listing category.
    for raw in run.raw_observations[7:9]:
        candidate = normalizer.normalize(raw, SOURCE, policy()).candidate
        assert candidate and candidate.title is None and candidate.description


def test_ambiguous_title_search_fails_with_evidence() -> None:
    fixture = enrichment_fixture()
    fixture.title_rows["515078"] *= 2
    evidence = MemoryEvidenceStore()
    result = execute(fixture, evidence)
    assert isinstance(result, SourceFailure)
    assert "ambiguous operator title" in result.error_detail
    assert evidence.get(result.failed_evidence_reference)


@pytest.mark.parametrize("missing_title", [None, "", " \t "])
def test_title_enrichment_handles_observed_null_and_blank_representations(
    missing_title: JsonValue,
) -> None:
    fixture = enrichment_fixture()
    fixture.rows[0]["enktNames"] = missing_title
    result = execute(fixture, MemoryEvidenceStore())
    assert isinstance(result, CollectionRun)
    assert result.raw_observations[0]["title"] == fixture.title_rows["515078"][0]["name"]
    assert result.raw_observations[0]["operator_title_lookup"]


@pytest.mark.database
def test_incomplete_minority_enriches_atomically_and_replays_idempotently(
    migrated_engine: Engine,
) -> None:
    collector = Collector(migrated_engine)
    fixture = enrichment_fixture()
    receipt = collector.collect(fixture)
    assert receipt.status == "accepted" and receipt.missing_incremented == 0
    before = collector.state()
    assert len(before) == 13 and all(row[1] == 1 for row in before)
    assert collector.collect(enrichment_fixture()).status == "accepted"
    assert collector.state() == before
    fixture = enrichment_fixture()
    # A genuinely contentless minority still cannot replace already healthy state.
    first = fixture.operators[str(fixture.rows[0]["lotId"])[-6:]]
    first.update(
        budget_products="[]", description=None, addon_description=None, technical_description=None
    )
    fixture.title_rows[str(fixture.rows[0]["lotId"])[-6:]] = []
    rejected = collector.collect(fixture)
    assert rejected.status != "accepted" and not rejected.rematch_tender_ids
    assert rejected.health and "missing_required_content" in {
        r.code for r in rejected.health.reasons
    }
    assert collector.state() == before


@pytest.mark.database
def test_enrichment_countdown_changes_only_evidence_real_deadline_changes_history(
    migrated_engine: Engine,
) -> None:
    collector = Collector(migrated_engine)
    fixture = IcppaFixture()
    identifier = str(fixture.rows[0]["lotId"])
    operator_id = identifier[-6:]
    assert collector.collect(fixture).status == "accepted"
    before = collector.state()
    volatile = IcppaFixture()
    volatile.operators[operator_id]["rest_time"] = "01:00:00"
    receipt = collector.collect(volatile)
    assert receipt.status == "accepted" and not receipt.rematch_tender_ids
    assert collector.state() == before
    with collector.sessions() as session:
        tender = next(
            t
            for t in SqlTenderRepository(session).for_source(SOURCE.id)
            if t.external_tender_id == identifier
        )
        origin = tender.raw_data["operator"]
        assert isinstance(origin, dict) and origin["rest_time"] == "01:00:00"
        assert tender.deadline
        new_deadline = tender.deadline + timedelta(days=1)
    changed = IcppaFixture()
    changed.operators[operator_id]["end_date"] = new_deadline.isoformat()
    receipt = collector.collect(changed)
    assert receipt.status == "accepted" and len(receipt.rematch_tender_ids) == 1
    assert sum(row[1] for row in collector.state()) == len(before) + 1


@pytest.mark.database
def test_operator_amount_formatting_does_not_make_history(migrated_engine: Engine) -> None:
    collector = Collector(migrated_engine)
    fixture = IcppaFixture()
    assert collector.collect(fixture).status == "accepted"
    before = collector.state()
    for origin in fixture.operators.values():
        origin["start_cost"] = str(Decimal(str(origin["start_cost"]))) + "0"
    receipt = collector.collect(fixture)
    assert receipt.status == "accepted" and not receipt.rematch_tender_ids
    assert collector.state() == before
