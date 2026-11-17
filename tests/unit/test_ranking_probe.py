from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from pydantic import JsonValue

from tender_system.core.configuration import ModelSettings
from tender_system.core.domain import CampaignCriteria, Tender
from tender_system.core.ports import ModelResponse
from tools.matching.ranking_probe import ranking_request, validate_ranking

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _tender(index: int, name: str) -> Tender:
    return Tender.model_validate(
        {
            "source_id": "synthetic",
            "external_tender_id": str(index),
            "internal_id": uuid4(),
            "current_version_id": uuid4(),
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            "title": name,
            "raw_data": {
                "product_items": [
                    {"id": f"P-{index}", "Product_Name": name, "Description": f"{name} supply"}
                ]
            },
        }
    )


def test_ranking_probe_requires_four_to_six_and_uses_structured_schema() -> None:
    with pytest.raises(ValueError, match="4 to 6"):
        ranking_request(
            (_tender(1, "Laptop"),),
            CampaignCriteria(name="Laptop"),
            ModelSettings(model="offline"),
            3,
        )
    tenders = tuple(
        _tender(index, name)
        for index, name in enumerate(("Laptop", "Road repair", "Printer", "Medical mask"), 1)
    )
    request = ranking_request(
        tenders,
        CampaignCriteria(name="Laptop procurement"),
        ModelSettings(model="offline", structured_output="schema"),
        3,
    )
    assert request.task == "tender_relevance_ranking_v1"
    assert request.max_output_tokens == 700
    assert request.output_schema is not None
    assert len(cast(list[JsonValue], request.payload["candidates"])) == 4


def test_ranking_response_requires_exact_ids_ranks_and_supplied_evidence() -> None:
    tenders = tuple(
        _tender(index, name)
        for index, name in enumerate(("Laptop", "Road repair", "Printer", "Medical mask"), 1)
    )
    request = ranking_request(
        tenders, CampaignCriteria(name="Laptop procurement"), ModelSettings(model="offline"), 3
    )
    candidates = cast(list[dict[str, JsonValue]], request.payload["candidates"])
    results = [
        {
            "tender_id": candidate["tender_id"],
            "relevance": level,
            "rank": rank,
            "explanation": "Supplied tender fields support this relative rank.",
            "evidence": (
                [{"product_id": f"P-{rank}", "field": "product_items.0.name", "quote": "Laptop"}]
                if rank == 1
                else []
            ),
            "missing_information": [],
        }
        for rank, (candidate, level) in enumerate(zip(candidates, (2, 0, 1, 0), strict=True), 1)
    ]
    response = ModelResponse(
        output=cast(dict[str, JsonValue], {"results": results}), model_version="offline"
    )
    validated = validate_ranking(response, request)
    assert validated.results[0].relevance == 2 and validated.results[0].rank == 1

    invalid = replace(
        response,
        output=cast(
            dict[str, JsonValue],
            {
                "results": [
                    {
                        **results[0],
                        "evidence": [{"field": "product_items.99.name", "quote": "Laptop"}],
                    },
                    *results[1:],
                ]
            },
        ),
    )
    with pytest.raises(ValueError, match="unsupplied product"):
        validate_ranking(invalid, request)
