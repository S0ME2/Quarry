"""Provider-independent thresholds and bounded escalation policy."""

from typing import Literal

from tender_system.core.configuration import CampaignSettings
from tender_system.core.domain import CampaignCriteria, MatchResult


class DecisionPolicy:
    def decide(
        self,
        score: float,
        confidence: float | None,
        uncertain: bool,
        criteria: CampaignCriteria,
        settings: CampaignSettings,
    ) -> Literal["match", "no_match", "unsure"]:
        thresholds = getattr(settings.thresholds, criteria.matching_mode)
        if uncertain or confidence is None or confidence < thresholds.confidence:
            return "unsure"
        if score >= thresholds.match:
            return "match"
        if score <= thresholds.no_match:
            return "no_match"
        return "unsure"


class RoutingPolicy:
    def semantic(self, settings: CampaignSettings) -> bool:
        return (
            settings.routing.semantic_enabled
            and settings.routing.max_model_calls > 0
            and settings.budget_limit > 0
        )

    def escalate(self, result: MatchResult, settings: CampaignSettings) -> bool:
        known_cost = sum(call.cost or 0 for call in result.model_calls)
        return (
            result.result == "unsure"
            and settings.routing.escalation_enabled
            and len(result.model_calls) < settings.routing.max_model_calls
            and known_cost < settings.budget_limit
        )
