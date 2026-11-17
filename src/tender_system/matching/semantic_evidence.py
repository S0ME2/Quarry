"""Independent verification for optional model-proposed semantic citations."""

from typing import cast

from pydantic import JsonValue, ValidationError

from tender_system.core.domain import (
    EvidenceVerification,
    MatchEvidence,
    Tender,
)
from tender_system.core.ports import ModelRequest
from tender_system.matching.semantic_matching import (
    evidence_field_label,
    source_quote,
    tender_field_value,
    tender_fields,
)


def verify_optional_evidence(
    raw: object,
    provided: bool,
    tender: Tender,
    request: ModelRequest,
) -> tuple[EvidenceVerification, tuple[MatchEvidence, ...]]:
    if not provided:
        return EvidenceVerification(status="not_provided"), ()
    if not isinstance(raw, list):
        return _invalid("invalid_evidence_shape"), ()
    if not raw:
        return EvidenceVerification(status="not_provided"), ()

    supplied_tender = request.payload.get("tender")
    if not isinstance(supplied_tender, dict):
        return _invalid("tender_not_supplied"), ()
    candidates = supplied_tender.get("product_candidates")
    # Resolve only against actual request leaves, never the full source metadata.
    fields = dict(supplied_tender)
    products: dict[str, JsonValue] = {}
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict) and type(candidate.get("source_index")) is int:
                products[str(candidate["source_index"])] = candidate
    fields["product_items"] = products
    original_fields = tender_fields(tender)
    verified: list[MatchEvidence] = []
    for item in raw:
        if not isinstance(item, dict):
            return _invalid("invalid_evidence_item"), ()
        try:
            evidence = MatchEvidence.model_validate(item)
        except ValidationError:
            return _invalid("invalid_evidence_item"), ()
        path = evidence.field.removeprefix("tender.")
        parts = path.split(".")
        # A path into the supplied candidate array uses array position; the
        # persisted canonical citation uses that candidate's original source index.
        if len(parts) >= 3 and parts[0] == "product_candidates" and parts[1].isdigit():
            position = int(parts[1])
            if not isinstance(candidates, list) or position >= len(candidates):
                return _invalid("product_not_supplied"), ()
            candidate = candidates[position]
            if not isinstance(candidate, dict) or type(candidate.get("source_index")) is not int:
                return _invalid("product_not_supplied"), ()
            path = ".".join(("product_items", str(candidate["source_index"]), *parts[2:]))
        label = evidence_field_label(path)
        if label == "other":
            return _invalid("unrecognized_field_path"), ()
        if path.startswith("campaign."):
            return _invalid("campaign_field_forbidden:campaign"), ()
        value = tender_field_value(fields, path)
        if value is None or isinstance(value, (dict, list)):
            return _invalid(f"field_not_supplied:{label}"), ()
        quote = source_quote(evidence.quote, cast(JsonValue, value))
        if quote is None:
            return _invalid(f"quote_not_found:{label}"), ()
        original = tender_field_value(original_fields, path)
        if original is None or isinstance(original, (dict, list)):
            return _invalid(f"field_not_in_source:{label}"), ()
        source_span = source_quote(quote, original)
        if source_span is None:
            return _invalid(f"quote_not_in_source:{label}"), ()
        verified.append(evidence.model_copy(update={"field": path, "quote": source_span}))
    return EvidenceVerification(status="verified"), tuple(verified)


def _invalid(detail: str) -> EvidenceVerification:
    return EvidenceVerification(
        status="invalid",
        error_code="invalid_evidence",
        detail=detail,
    )
