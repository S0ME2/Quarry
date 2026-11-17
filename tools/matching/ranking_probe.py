"""Offline-testable batch ranking probe, separate from pairwise production matching."""

from typing import Literal, cast

from pydantic import Field, JsonValue

from tender_system.core.configuration import ModelSettings
from tender_system.core.domain import CampaignCriteria, Contract, NonEmpty, Tender
from tender_system.core.ports import ModelRequest, ModelResponse
from tender_system.matching.product_candidates import select_product_candidates
from tender_system.matching.semantic_matching import tender_fields


class RankingEvidence(Contract):
    product_id: str | None = None
    field: NonEmpty
    quote: NonEmpty


class TenderRanking(Contract):
    tender_id: NonEmpty
    relevance: Literal[0, 1, 2]
    rank: int = Field(ge=1, le=6)
    explanation: NonEmpty
    evidence: tuple[RankingEvidence, ...]
    missing_information: tuple[NonEmpty, ...]


class RankingResponse(Contract):
    results: tuple[TenderRanking, ...] = Field(min_length=4, max_length=6)


_INSTRUCTIONS = """Rank 4 to 6 tenders for one campaign. 0=irrelevant, 1=possible/review,
2=relevant. Rank every supplied tender once, 1 is best; ranks are comparative, not
probabilities. Use only supplied evidence. Cite exact field paths and short exact quotes;
for products use original product_items.<source_index>.<field> paths and source product IDs.
Report material missing information. If product_selection.complete is false, do not label a
tender 0; use 1 and explain that omitted items remain unexamined. Return JSON matching schema."""


def _schema() -> dict[str, JsonValue]:
    schema = RankingResponse.model_json_schema()

    def require(node: object) -> None:
        if isinstance(node, dict):
            if "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                require(value)
        elif isinstance(node, list):
            for value in node:
                require(value)

    require(schema)
    return cast(dict[str, JsonValue], schema)


def ranking_request(
    tenders: tuple[Tender, ...],
    criteria: CampaignCriteria,
    model: ModelSettings,
    timeout: float,
) -> ModelRequest:
    if not 4 <= len(tenders) <= 6:
        raise ValueError("ranking probe requires 4 to 6 tenders")
    candidates: list[dict[str, JsonValue]] = []
    for position, tender in enumerate(tenders, start=1):
        fields = tender_fields(tender)
        selection = select_product_candidates(tender, criteria)
        fields.pop("product_items", None)
        if selection.total or isinstance(tender.raw_data.get("product_items"), list):
            fields["product_candidates"] = cast(JsonValue, list(selection.candidates))
        candidates.append(
            {
                "tender_id": (
                    f"{tender.source_id}:{tender.external_tender_id}"
                    if tender.external_tender_id is not None
                    else f"candidate-{position}"
                ),
                "fields": fields,
                "product_selection": {
                    "complete": selection.complete,
                    "included": len(selection.candidates),
                    "total": selection.total,
                },
            }
        )
    ids = [str(candidate["tender_id"]) for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("ranking probe requires unique tender IDs")
    return ModelRequest(
        model=model.model,
        task="tender_relevance_ranking_v1",
        payload={
            "instructions": _INSTRUCTIONS,
            "campaign": criteria.model_dump(mode="json", exclude={"matching_mode"}),
            "candidates": cast(JsonValue, candidates),
        },
        timeout_seconds=min(timeout, model.timeout_seconds),
        max_output_tokens=700,
        output_schema=_schema() if model.structured_output == "schema" else None,
    )


def validate_ranking(response: ModelResponse, request: ModelRequest) -> RankingResponse:
    ranking = RankingResponse.model_validate(response.output)
    candidates = request.payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("ranking request has no candidate batch")
    expected = {
        str(item["tender_id"]): item
        for item in candidates
        if isinstance(item, dict) and isinstance(item.get("tender_id"), str)
    }
    actual = [item.tender_id for item in ranking.results]
    if (
        len(expected) != len(candidates)
        or set(actual) != set(expected)
        or len(actual) != len(set(actual))
    ):
        raise ValueError("ranking response must contain every supplied tender exactly once")
    ranks = [item.rank for item in ranking.results]
    if set(ranks) != set(range(1, len(expected) + 1)):
        raise ValueError("ranking positions must be unique and consecutive")

    for result in ranking.results:
        candidate = expected[result.tender_id]
        fields = candidate.get("fields")
        if not isinstance(fields, dict):
            raise ValueError("ranking candidate fields are malformed")
        products = fields.get("product_candidates")
        for evidence in result.evidence:
            parts = evidence.field.split(".")
            value: JsonValue | None = None
            if len(parts) >= 3 and parts[0] == "product_items" and parts[1].isdigit():
                product_index = int(parts[1])
                product = (
                    next(
                        (
                            item
                            for item in products
                            if isinstance(products, list)
                            and isinstance(item, dict)
                            and type(item.get("source_index")) is int
                            and item["source_index"] == product_index
                        ),
                        None,
                    )
                    if isinstance(products, list)
                    else None
                )
                if product is None:
                    raise ValueError("ranking evidence refers to an unsupplied product")
                source_id = product.get("source_product_id")
                expected_product_id = str(source_id) if source_id is not None else None
                if evidence.product_id != expected_product_id:
                    raise ValueError("ranking evidence product ID does not match source")
                if len(parts) == 3:
                    value = product.get(parts[2])
                elif len(parts) >= 4 and parts[2] == "attributes":
                    attributes = product.get("attributes")
                    if isinstance(attributes, dict):
                        value = attributes.get(".".join(parts[3:]))
            elif len(parts) == 1:
                value = fields.get(evidence.field)
            if value is None or evidence.quote not in str(value):
                raise ValueError("ranking evidence does not exist in supplied fields")
    return ranking
