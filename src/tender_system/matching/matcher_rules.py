"""Cheap, explainable constraints. Keywords include hints; exclusions are hard rules."""

import re
from dataclasses import dataclass
from datetime import datetime

from tender_system.core.configuration import MatcherRouting
from tender_system.core.domain import CampaignCriteria, MatchEvidence, Reason, Tender
from tender_system.core.tenders import normalized_text
from tender_system.matching.semantic_matching import product_text_fields, supplementary_text_fields


@dataclass(frozen=True)
class RuleEvaluation:
    failures: tuple[Reason, ...]
    unknowns: tuple[Reason, ...]
    evidence: tuple[MatchEvidence, ...]
    needs_semantics: bool


def contains_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(normalized_text(phrase)) + r"(?!\w)", text))


def keyword_found(tender: Tender, criteria: CampaignCriteria) -> bool:
    fields = (
        tender.title,
        tender.description,
        tender.document_text,
        *(value for _, value in supplementary_text_fields(tender)),
        *(value for _, value in product_text_fields(tender)),
    )
    return any(
        contains_phrase(normalized_text(value or ""), word)
        for value in fields
        for word in criteria.include_keywords
        if word.strip()
    )


class RuleEvaluator:
    def evaluate(
        self, tender: Tender, criteria: CampaignCriteria, now: datetime, routing: MatcherRouting
    ) -> RuleEvaluation:
        failures: list[Reason] = []
        unknowns: list[Reason] = []
        evidence: list[MatchEvidence] = []

        def reason(code: str, detail: str, field: str, *, unknown: bool = False) -> None:
            (unknowns if unknown else failures).append(
                Reason(code=code, detail=detail, field=field)
            )

        def use(field: str) -> object:
            value = getattr(tender, field)
            if value is not None and str(value).strip():
                evidence.append(MatchEvidence(field=field, quote=str(value)))
                return value
            reason("missing_evidence", f"Tender {field} is unknown", field, unknown=True)
            return None

        for field, wanted in (
            ("currency", criteria.currencies),
            ("buyer", criteria.buyers),
            ("location", criteria.locations),
            ("status", criteria.status_rules),
        ):
            if wanted:
                value = use(field)
                if value is not None and normalized_text(str(value)) not in {
                    normalized_text(x) for x in wanted
                }:
                    reason("criterion_failed", f"Tender {field} is outside campaign choices", field)
        excluded_buyers = {normalized_text(x) for x in criteria.exclude_buyers}
        if excluded_buyers:
            if set(map(normalized_text, criteria.buyers)) <= excluded_buyers and criteria.buyers:
                reason("conflicting_criteria", "All included buyers are also excluded", "buyer")
            value = use("buyer")
            if value is not None and normalized_text(str(value)) in excluded_buyers:
                reason("excluded_buyer", "Tender buyer is excluded by campaign", "buyer")
        if criteria.min_value is not None or criteria.max_value is not None:
            value = use("value")
            if not criteria.currencies:
                reason(
                    "currency_basis_unknown",
                    "Value bounds need campaign currency",
                    "currency",
                    unknown=True,
                )
                value = None
            elif normalized_text(tender.currency or "") not in {
                normalized_text(currency) for currency in criteria.currencies
            }:
                # Currency check above records mismatch/unknown; amounts are incomparable.
                value = None
            if value is not None:
                if criteria.min_value is not None and tender.value is not None:
                    if tender.value < criteria.min_value:
                        reason("value_below_minimum", "Value below campaign minimum", "value")
                if criteria.max_value is not None and tender.value is not None:
                    if tender.value > criteria.max_value:
                        reason("value_above_maximum", "Value above campaign maximum", "value")
        if routing.exclude_expired and tender.deadline is not None:
            evidence.append(MatchEvidence(field="deadline", quote=str(tender.deadline)))
            if tender.deadline <= now:
                reason("expired", "Tender deadline has passed", "deadline")
        if tender.status and normalized_text(tender.status) in {
            normalized_text(x) for x in routing.excluded_statuses
        }:
            evidence.append(MatchEvidence(field="status", quote=tender.status))
            reason("inactive_status", "Tender status is closed or excluded", "status")
        deadline = criteria.deadline_rules
        if deadline.min_days_remaining is not None or deadline.max_days_remaining is not None:
            if use("deadline") is not None and tender.deadline is not None:
                days = (tender.deadline - now).total_seconds() / 86400
                if (
                    deadline.min_days_remaining is not None and days < deadline.min_days_remaining
                ) or (
                    deadline.max_days_remaining is not None and days > deadline.max_days_remaining
                ):
                    reason(
                        "deadline_outside_window", "Deadline outside campaign window", "deadline"
                    )
        subject_fields = (
            [(field, getattr(tender, field)) for field in ("title", "description", "document_text")]
            + list(product_text_fields(tender))
            + list(supplementary_text_fields(tender))
        )
        included = keyword_found(tender, criteria)
        for field, value in subject_fields:
            text = normalized_text(value or "")
            for word in () if included else criteria.exclude_keywords:
                if contains_phrase(text, word):
                    evidence.append(MatchEvidence(field=field, quote=value))
                    reason("excluded_keyword", f"Excluded phrase found: {word}", field)
        if criteria.status_rules and set(map(normalized_text, criteria.status_rules)) <= {
            normalized_text(x) for x in routing.excluded_statuses
        }:
            reason("conflicting_criteria", "Campaign accepts only excluded statuses", "status")
        semantic = bool(
            criteria.description.strip()
            or criteria.custom_notes.strip()
            or criteria.include_keywords
        )
        if semantic and not any(value for _, value in subject_fields):
            reason(
                "missing_content", "Tender has no subject description", "description", unknown=True
            )
        return RuleEvaluation(tuple(failures), tuple(unknowns), tuple(evidence), semantic)
