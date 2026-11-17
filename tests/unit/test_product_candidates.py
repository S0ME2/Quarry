import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import JsonValue

from tender_system.core.configuration import MatcherRouting, ModelSettings
from tender_system.core.domain import CampaignCriteria, Tender
from tender_system.matching.matcher_rules import RuleEvaluator
from tender_system.matching.product_candidates import select_product_candidates
from tender_system.matching.semantic_matching import SemanticMatcher

CASES = Path("benchmarks/matcher/phase16f/synthetic_cases.json")
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _load_cases() -> list[dict[str, JsonValue]]:
    return cast(list[dict[str, JsonValue]], json.loads(CASES.read_text()))


def _contracts(case: dict[str, JsonValue]) -> tuple[Tender, CampaignCriteria]:
    campaign = cast(dict[str, JsonValue], case["campaign"])
    tender_data = cast(dict[str, JsonValue], case["tender"])
    products = cast(list[JsonValue], tender_data.get("products", []))
    normalized_products = [
        {
            "id": product.get("id"),
            "Product_Name": product.get("name"),
            "Description": product.get("description"),
        }
        for product in (cast(dict[str, JsonValue], item) for item in products)
    ]
    tender_fields: dict[str, object] = {
        "source_id": "synthetic",
        "external_tender_id": str(case["id"]),
        "internal_id": uuid4(),
        "current_version_id": uuid4(),
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "title": tender_data.get("title"),
        "description": tender_data.get("description"),
        "value": tender_data.get("value"),
        "currency": tender_data.get("currency"),
        "raw_data": {"product_items": normalized_products},
    }
    tender = Tender.model_validate(tender_fields)
    criteria = CampaignCriteria.model_validate(
        {key: value for key, value in campaign.items() if key != "max_value"}
        | ({"max_value": campaign["max_value"]} if "max_value" in campaign else {})
    )
    return tender, criteria


def test_synthetic_evaluation_contracts_are_complete_and_consistent() -> None:
    cases = _load_cases()
    assert len(cases) == 15
    ids = {str(case["id"]) for case in cases}
    assert len(ids) == len(cases)
    for case in cases:
        assert case["relevance"] in {0, 1, 2}
        assert case["hard_filter"] in {"pass", "fail", "unknown"}
        assert isinstance(case["reason"], str) and case["reason"]
        tender_data = cast(dict[str, JsonValue], case["tender"])
        products = cast(list[JsonValue], tender_data.get("products", []))
        product_ids = {str(cast(dict[str, JsonValue], product)["id"]) for product in products}
        support = cast(list[JsonValue], case["supporting_product_ids"])
        assert {str(product_id) for product_id in support} <= product_ids
        for path in cast(list[JsonValue], case["expected_evidence"]):
            parts = str(path).split(".")
            if parts[0] == "product_items":
                assert int(parts[1]) < len(products)
                assert parts[2] in {"name", "description"}
                source_product = cast(dict[str, JsonValue], products[int(parts[1])])
                assert source_product.get(parts[2])
            else:
                assert tender_data.get(str(path)) is not None


def test_synthetic_expected_hard_filter_outcomes_match_rules() -> None:
    for case in _load_cases():
        tender, criteria = _contracts(case)
        result = RuleEvaluator().evaluate(tender, criteria, NOW, MatcherRouting())
        observed = "fail" if result.failures else "unknown" if result.unknowns else "pass"
        assert observed == case["hard_filter"], str(case["id"])


def test_product_312_is_retrieved_with_original_id_and_evidence_path() -> None:
    case = next(case for case in _load_cases() if case["id"] == "large-tender-position-312")
    tender, criteria = _contracts(case)
    selection = select_product_candidates(tender, criteria)
    assert not selection.complete and selection.total == 312
    assert len(selection.candidates) <= 18
    candidate = next(item for item in selection.candidates if item["source_index"] == 311)
    assert candidate["source_product_id"] == "INS-312"
    assert candidate["name"] == "Insulin cartridges"

    request = SemanticMatcher().request(
        tender,
        criteria,
        model=ModelSettings(model="offline"),
        timeout=1,
    )
    payload = cast(dict[str, JsonValue], request.payload["tender"])
    assert payload["product_candidates"]
