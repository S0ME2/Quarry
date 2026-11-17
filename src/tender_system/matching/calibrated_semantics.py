"""Version 2 semantic contract: assess every subject field before deciding."""

from typing import Literal, Self, cast

from pydantic import Field, JsonValue, model_validator

from tender_system.core.configuration import ModelSettings
from tender_system.core.domain import (
    CampaignCriteria,
    Contract,
    MatchEvidence,
    NonEmpty,
    Score,
    Tender,
)
from tender_system.core.ports import ModelFailure, ModelRequest, ModelResponse
from tender_system.matching.semantic_matching import (
    SemanticJudgment,
    SemanticMatcher,
    evidence_failure,
    source_quote,
    tender_field_value,
    tender_fields,
)

Relation = Literal["direct", "partial", "adjacent", "unrelated", "unknown"]

INSTRUCTIONS = """
Additional safety contract:
Return all fields in the supplied JSON schema. Assess EVERY nonempty title and description
independently in field_assessments, with its exact field/quote, relation and concise detail.
Direct means the requested purchase category or an unambiguous synonym; partial is a mixed
requested scope; adjacent is an ancillary/contextual connection; unrelated is a different
purchase; unknown means insufficient scope information. Generic titles and boilerplate
descriptions can be unknown. Do not invent contents of linked specifications.
If title requests one purchase but description excludes it or names a different purchase,
mark evidence_conflicts=true, result=unsure and explain BOTH fields. Do not silently prefer
description over title. Opposing direct/partial and unrelated field assessments conflict.
A generic umbrella title (e.g. hospital equipment) can be unknown; an enumerated description
refines it and is not a conflict. Context alone never establishes a directly requested item.
Set criteria_conflicts=true for contradictory campaign requirements, never pretend they
are a normal rejection. critical_missing_information lists ONLY missing facts required
by actual campaign constraints. Optional missing metadata is not material uncertainty.
scope_relation and support describe the evidence, independently of campaign mode.
support is explicit for directly named required scope, synonym for unambiguous vocabulary
equivalence, inferred for an ancillary scope under broad umbrella wording, none for
clearly unrelated scope, unknown for insufficient evidence. Umbrella descriptions do not
explicitly request every ancillary subsystem. Explain inference instead of claiming it
was explicitly requested. justification is clear or weak; weak entails unsure.
Evidence MUST cite only tender fields: title, description, buyer, location, value, currency,
status, deadline, published_at, or an exact product_items.<index>.<field> path. NEVER use
campaign.* as evidence; discuss campaign in reasons. Every definitive semantic decision needs
title, description, or product-item evidence. Score means relevance:
0=unrelated, 1=direct requested scope, not confidence in your chosen result. Confidence is
subjective and uncalibrated. Result match needs score>=0.5; no_match needs score<=0.5.
Missing critical information, conflict, weak justification or vague scope require unsure
with concise uncertainties. Keep reasons and field details short; at most 4 reasons.
"""


class FieldAssessment(Contract):
    field: Literal["title", "description"]
    quote: NonEmpty
    relation: Relation
    detail: NonEmpty


class CalibratedJudgment(SemanticJudgment):
    score: Score = Field(strict=True)
    confidence: Score | None = Field(default=None, strict=True)
    field_assessments: tuple[FieldAssessment, ...]
    scope_relation: Relation
    support: Literal["explicit", "synonym", "inferred", "none", "unknown"]
    evidence_conflicts: bool = Field(strict=True)
    criteria_conflicts: bool = Field(strict=True)
    critical_missing_information: tuple[NonEmpty, ...]
    justification: Literal["clear", "weak"]

    @model_validator(mode="after")
    def unique_fields(self) -> Self:
        fields = [assessment.field for assessment in self.field_assessments]
        if len(fields) != len(set(fields)):
            raise ValueError("duplicate field assessments")
        return self


def strict_schema() -> dict[str, JsonValue]:
    schema = CalibratedJudgment.model_json_schema()

    def require_fields(node: object) -> None:
        if isinstance(node, dict):
            if "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                require_fields(value)
        elif isinstance(node, list):
            for value in node:
                require_fields(value)

    require_fields(schema)
    return cast(dict[str, JsonValue], schema)


class CalibratedSemanticMatcher(SemanticMatcher):
    def request(
        self, tender: Tender, criteria: CampaignCriteria, model: ModelSettings, timeout: float
    ) -> ModelRequest:
        legacy = super().request(tender, criteria, model, timeout)
        schema = strict_schema()
        return ModelRequest(
            model=legacy.model,
            task="tender_match_v2",
            timeout_seconds=legacy.timeout_seconds,
            payload={
                **{key: value for key, value in legacy.payload.items() if key != "output_contract"},
                "instructions": str(legacy.payload["instructions"]) + INSTRUCTIONS,
                **({"output_contract": schema} if model.structured_output != "schema" else {}),
            },
            max_output_tokens=legacy.max_output_tokens,
            max_attempts=legacy.max_attempts,
            output_schema=schema if model.structured_output == "schema" else None,
        )

    def validate(
        self, response: ModelResponse, tender: Tender, request: ModelRequest | None = None
    ) -> CalibratedJudgment:
        try:
            judgment = CalibratedJudgment.model_validate(response.output)
        except ValueError:
            raise ModelFailure("invalid_judgment") from None
        fields = tender_fields(tender)
        supplied = {
            field for field in ("title", "description") if str(fields.get(field) or "").strip()
        }
        if {item.field for item in judgment.field_assessments} != supplied:
            raise ModelFailure("incomplete_assessment")
        evidence: tuple[MatchEvidence | FieldAssessment, ...] = (
            *judgment.evidence,
            *judgment.field_assessments,
        )
        allowed_product_indexes: set[int] | None = None
        if request is not None:
            supplied_data = request.payload.get("tender")
            candidates = (
                supplied_data.get("product_candidates") if isinstance(supplied_data, dict) else None
            )
            if isinstance(candidates, list):
                allowed_product_indexes = {
                    cast(int, candidate["source_index"])
                    for candidate in candidates
                    if isinstance(candidate, dict) and type(candidate.get("source_index")) is int
                }
        quotes: dict[tuple[str, str], str] = {}
        for item in evidence:
            value = tender_field_value(fields, item.field)
            parts = item.field.split(".")
            if (
                isinstance(item, MatchEvidence)
                and allowed_product_indexes is not None
                and len(parts) >= 3
                and parts[0] == "product_items"
                and parts[1].isdigit()
                and int(parts[1]) not in allowed_product_indexes
            ):
                raise evidence_failure(item.field, "product_not_supplied")
            if request is not None and not item.field.startswith("product_items."):
                supplied_fields = request.payload.get("tender")
                supplied_value = (
                    tender_field_value(supplied_fields, item.field)
                    if isinstance(supplied_fields, dict)
                    else None
                )
                if source_quote(item.quote, supplied_value) is None:
                    raise evidence_failure(item.field, "quote_not_supplied")
            quote = source_quote(item.quote, value)
            if quote is None:
                raise evidence_failure(item.field, "field_not_supplied_or_quote_not_found")
            quotes[item.field, item.quote] = quote
        if judgment.result != "unsure" and not any(
            item.field in {"title", "description", "document_text", "requirements", "conditions"}
            or item.field.startswith("product_items.")
            for item in judgment.evidence
        ):
            raise ModelFailure("insufficient_subject_evidence")
        return judgment.model_copy(
            update={
                "evidence": tuple(
                    item.model_copy(update={"quote": quotes[item.field, item.quote]})
                    for item in judgment.evidence
                ),
                "field_assessments": tuple(
                    item.model_copy(update={"quote": quotes[item.field, item.quote]})
                    for item in judgment.field_assessments
                ),
            }
        )
