"""Provider-neutral v2 abstention gates. Scores alone cannot clear these gates."""

from datetime import datetime
from math import isclose

from tender_system.core.configuration import CampaignSettings, MatcherRouting, UncertaintySettings
from tender_system.core.domain import CampaignCriteria, Reason, Tender
from tender_system.matching.calibrated_semantics import CalibratedJudgment
from tender_system.matching.matcher_rules import RuleEvaluation, RuleEvaluator


class ConservativeRuleEvaluator(RuleEvaluator):
    def __init__(self, policy: UncertaintySettings) -> None:
        self.policy = policy

    def evaluate(
        self, tender: Tender, criteria: CampaignCriteria, now: datetime, routing: MatcherRouting
    ) -> RuleEvaluation:
        result = super().evaluate(tender, criteria, now, routing)
        if not self.policy.enabled:
            return result
        conflicts = tuple(r for r in result.failures if r.code == "conflicting_criteria")
        if conflicts and self.policy.abstain_on_conflicts:
            conflicted_fields = {reason.field for reason in conflicts}
            result = RuleEvaluation(
                tuple(r for r in result.failures if r.field not in conflicted_fields),
                result.unknowns + conflicts,
                result.evidence,
                result.needs_semantics,
            )
        missing = [
            Reason(
                code="critical_missing_information",
                detail=f"Required {field} is unknown",
                field=field,
            )
            for field in self.policy.required_fields
            if getattr(tender, field) is None
            or getattr(tender, field) == ()
            or not str(getattr(tender, field)).strip()
        ]
        constrained = set(self.policy.required_fields) | {"title", "description"}
        for field, active in (
            ("value", criteria.min_value is not None or criteria.max_value is not None),
            ("location", bool(criteria.locations)),
            ("buyer", bool(criteria.buyers or criteria.exclude_buyers)),
            ("currency", bool(criteria.currencies)),
            ("status", bool(criteria.status_rules)),
            (
                "deadline",
                routing.exclude_expired
                or criteria.deadline_rules.min_days_remaining is not None
                or criteria.deadline_rules.max_days_remaining is not None,
            ),
        ):
            if active:
                constrained.add(field)
        unreliable = [
            Reason(
                code="unreliable_evidence",
                detail=f"Tender {issue.field} has a quality error",
                field=issue.field,
            )
            for issue in tender.quality_issues
            if issue.severity == "error" and issue.field in constrained
        ]
        if missing or unreliable:
            return RuleEvaluation(
                tuple(
                    r
                    for r in result.failures
                    if r.field not in {reason.field for reason in unreliable}
                ),
                result.unknowns + tuple(missing + unreliable),
                result.evidence,
                result.needs_semantics,
            )
        return result


class UncertaintyPolicy:
    def evaluate(
        self, judgment: CalibratedJudgment, criteria: CampaignCriteria, settings: CampaignSettings
    ) -> tuple[Reason, ...]:
        policy = settings.uncertainty
        if not policy.enabled:
            return ()
        reasons: list[Reason] = []

        def unknown(code: str, detail: str) -> None:
            reasons.append(Reason(code=code, detail=detail))

        relations = {item.relation for item in judgment.field_assessments}
        conflict = judgment.evidence_conflicts or (
            "unrelated" in relations and bool(relations & {"direct", "partial"})
        )
        if policy.abstain_on_conflicts and conflict:
            unknown("evidence_conflict", "Tender subject fields conflict; neither takes precedence")
        if policy.abstain_on_conflicts and judgment.criteria_conflicts:
            unknown("criteria_conflict", "Campaign requirements conflict")
        for fact in judgment.critical_missing_information:
            unknown("critical_missing_information", fact)
        if judgment.result == "match" and (
            judgment.scope_relation == "unrelated" or judgment.support == "none"
        ):
            unknown("inconsistent_assessment", "Evidence assessment does not support a match")
        if (
            judgment.justification == "weak"
            or judgment.scope_relation == "unknown"
            or judgment.support == "unknown"
        ):
            unknown(
                "weak_justification",
                "Procurement scope does not clearly justify a definitive decision",
            )
        if (
            judgment.scope_relation == "adjacent"
            and criteria.matching_mode not in policy.adjacent_modes
        ):
            unknown(
                "adjacent_scope", "Ancillary relevance needs clarification in this campaign mode"
            )
        if (
            judgment.scope_relation == "partial"
            and criteria.matching_mode not in policy.partial_modes
        ):
            unknown(
                "partial_scope", "Mixed procurement scope needs clarification in this campaign mode"
            )
        if judgment.support == "inferred" and criteria.matching_mode not in policy.inferred_modes:
            unknown(
                "inferred_scope",
                "Campaign wording does not explicitly establish this inferred scope",
            )
        thresholds = getattr(settings.thresholds, criteria.matching_mode)
        margin = policy.borderline_margin
        if margin and any(
            distance < margin and not isclose(distance, margin, rel_tol=0, abs_tol=1e-12)
            for distance in (
                abs(judgment.score - thresholds.match),
                abs(judgment.score - thresholds.no_match),
            )
        ):
            unknown("borderline_relevance", "Relevance is too close to a decision boundary")
        return tuple(reasons)
