"""Staged matcher; dependencies are injected through existing model and clock ports."""

from collections.abc import Callable
from dataclasses import replace
from time import monotonic
from typing import Literal

from tender_system.core.configuration import CampaignSettings, ModelSettings
from tender_system.core.domain import (
    CampaignCriteria,
    EvidenceVerification,
    MatchEvidence,
    MatchResult,
    ModelCall,
    ModelOpinion,
    Reason,
    Tender,
)
from tender_system.core.ports import Clock, ModelClient, ModelFailure
from tender_system.matching.calibrated_semantics import (
    CalibratedJudgment,
    CalibratedSemanticMatcher,
)
from tender_system.matching.matcher_policy import DecisionPolicy, RoutingPolicy
from tender_system.matching.matcher_rules import RuleEvaluator, keyword_found
from tender_system.matching.matcher_uncertainty import ConservativeRuleEvaluator, UncertaintyPolicy
from tender_system.matching.request_context import bounded_context
from tender_system.matching.semantic_decision import SemanticDecision, SemanticDecisionMatcher
from tender_system.matching.semantic_evidence import verify_optional_evidence
from tender_system.matching.semantic_matching import SemanticMatcher, bounded_generate


class IntelligentMatcher:
    implementation = "intelligent"
    version = "1"

    def __init__(
        self,
        settings: CampaignSettings,
        clock: Clock,
        providers: Callable[[ModelSettings], ModelClient],
        *,
        rules: RuleEvaluator | None = None,
        semantics: SemanticMatcher | None = None,
        routing: RoutingPolicy | None = None,
        thresholds: DecisionPolicy | None = None,
    ) -> None:
        if settings.matcher_version not in {"1", "2", "3"}:
            raise ValueError("intelligent matcher supports version 1, 2 or 3")
        self.version = settings.matcher_version
        self.settings, self.clock, self.providers = settings, clock, providers
        self.rules = rules or (
            ConservativeRuleEvaluator(settings.uncertainty)
            if self.version in {"2", "3"}
            else RuleEvaluator()
        )
        self.semantics = semantics or (
            CalibratedSemanticMatcher() if self.version == "2" else SemanticMatcher()
        )
        self.decision_semantics = SemanticDecisionMatcher()
        self.uncertainty = UncertaintyPolicy()
        self.routing, self.thresholds = routing or RoutingPolicy(), thresholds or DecisionPolicy()

    def match(self, tender: Tender, criteria: CampaignCriteria) -> MatchResult:
        started = monotonic()
        rules = self.rules.evaluate(tender, criteria, self.clock.now(), self.settings.routing)
        base = MatchResult(
            result="no_match" if rules.failures else "unsure" if rules.unknowns else "match",
            score=0 if rules.failures else 0.5 if rules.unknowns else 1,
            confidence=1 if rules.failures else 0 if rules.unknowns else 1,
            reasons=rules.failures
            or rules.unknowns
            or (
                Reason(
                    code="constraints_satisfied",
                    detail="Supplied structured constraints are satisfied",
                ),
            ),
            uncertainties=() if rules.failures else rules.unknowns,
            evidence=rules.evidence,
            decision_stage="rules",
            matcher_implementation=self.implementation,
            matcher_version=self.version,
            tender_version_id=tender.current_version_id,
        )
        if not rules.failures and tender.documents_incomplete:
            # Neither semantic decision can finalize while attachments are missing.
            # Keep the review recoverable without spending a paid request.
            reason = Reason(
                code="document_processing_incomplete",
                detail="One or more tender documents could not be processed",
            )
            return base.model_copy(
                update={
                    "result": "unsure",
                    "score": 0.5,
                    "confidence": 0,
                    "uncertainties": base.uncertainties + (reason,),
                }
            )
        if rules.failures or rules.unknowns or not rules.needs_semantics:
            return base
        if not self.routing.semantic(self.settings):
            return base.model_copy(
                update={
                    "result": "unsure",
                    "score": 0.5,
                    "confidence": 0,
                    "uncertainties": (
                        Reason(
                            code="semantic_disabled",
                            detail="Semantic evaluation unavailable under routing or budget policy",
                        ),
                    ),
                }
            )
        # Leave time for the batch worker to validate/store the bounded result.
        deadline = started + self.settings.timeout_seconds * 0.9
        result = self._evaluate(tender, criteria, self.settings.model, "semantic", base, deadline)
        blocked = {"evidence_conflict", "criteria_conflict", "critical_missing_information"}
        can_escalate = self.version == "1" or (
            self.version == "2"
            and not any(reason.code in blocked for reason in result.uncertainties)
        )
        if (
            self.version in {"2", "3"}
            and any(call.error_code for call in result.model_calls)
            and not self.settings.uncertainty.escalate_provider_failures
        ):
            can_escalate = False
        if can_escalate and self.routing.escalate(result, self.settings) and monotonic() < deadline:
            stronger = self.settings.routing.stronger_model
            assert stronger is not None
            result = self._evaluate(tender, criteria, stronger, "escalation", result, deadline)
        final_rules = self.rules.evaluate(tender, criteria, self.clock.now(), self.settings.routing)
        if final_rules.failures:
            return result.model_copy(
                update={
                    "result": "no_match",
                    "score": 0,
                    "confidence": 1,
                    "reasons": final_rules.failures,
                    "uncertainties": (),
                    "evidence": result.evidence + final_rules.evidence,
                    "decision_stage": "rules",
                }
            )
        if final_rules.unknowns:
            return result.model_copy(
                update={
                    "result": "unsure",
                    "score": 0.5,
                    "confidence": 0,
                    "uncertainties": final_rules.unknowns,
                    "evidence": result.evidence + final_rules.evidence,
                    "decision_stage": "rules",
                }
            )
        return result

    def _evaluate(
        self,
        tender: Tender,
        criteria: CampaignCriteria,
        model: ModelSettings,
        stage: Literal["semantic", "escalation"],
        previous: MatchResult,
        deadline: float,
    ) -> MatchResult:
        call = ModelCall(stage=stage, provider=model.provider, model=model.model)
        failure_stage = "request_construction"
        try:
            remaining = max(0.001, deadline - monotonic())
            if (
                self.version != "3"
                and stage == "semantic"
                and self.settings.routing.escalation_enabled
                and self.settings.routing.max_model_calls > 1
            ):
                remaining *= 0.6  # Reserve time for the configured stronger evaluation.
            if keyword_found(tender, criteria):
                criteria = criteria.model_copy(update={"exclude_keywords": ()})
            request = (
                self.decision_semantics.request(tender, criteria, model, remaining)
                if self.version == "3"
                else self.semantics.request(tender, criteria, model, remaining)
            )
            if stage == "escalation" and self.version != "3":
                request = replace(
                    request,
                    payload={
                        **request.payload,
                        "unresolved_issues": [reason.detail for reason in previous.uncertainties],
                        "instructions": str(request.payload.get("instructions", ""))
                        + "\nIndependently reassess the unresolved issues using the original "
                        "tender evidence. Resolve ambiguity only where supplied facts support it; "
                        "retain unsure for actual conflict or missing required information.",
                    },
                )
            request = bounded_context(request, model)
            selection = request.payload.get("document_selection")
            if isinstance(selection, dict) and selection.get("complete") is False:
                # Partial documents cannot finalize either semantic outcome. Do not spend
                # a provider request merely to store the same recoverable review state.
                return context_result(previous, selection)
            failure_stage = "provider_configuration"
            client = self.providers(model)
            failure_stage = "provider_call"
            response = bounded_generate(
                client,
                replace(
                    request,
                    validate_output=(
                        None
                        if self.version == "3"
                        else lambda response: self.semantics.validate(response, tender, request)
                    ),
                ),
            )
            failure_stage = "response_validation"
            call = ModelCall(
                stage=stage,
                provider=model.provider,
                model=model.model,
                model_version=response.model_version,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost=response.cost,
                attempts=response.attempts,
                attempt_errors=response.attempt_errors,
            )
            selection = request.payload.get("product_selection")
            incomplete_product_context = (
                isinstance(selection, dict) and selection.get("complete") is False
            )
            product_coverage = (
                f"{selection.get('included')} of {selection.get('total')} products"
                if isinstance(selection, dict)
                and type(selection.get("included")) is int
                and type(selection.get("total")) is int
                else "some tender products"
            )
            if self.version == "3":
                opinion, proposed_evidence, evidence_provided = self.decision_semantics.parse(
                    response
                )
                evidence_status, verified_evidence = verify_optional_evidence(
                    proposed_evidence, evidence_provided, tender, request
                )
                result = self._decision_result(
                    previous,
                    call,
                    opinion,
                    evidence_status,
                    verified_evidence,
                    criteria,
                    incomplete_product_context,
                    product_coverage,
                )
                return context_result(result, request.payload.get("document_selection"))
            judgment = self.semantics.validate(response, tender, request)
            policy_unknowns = (
                self.uncertainty.evaluate(judgment, criteria, self.settings)
                if isinstance(judgment, CalibratedJudgment)
                else ()
            )
            decision = self.thresholds.decide(
                judgment.score,
                judgment.confidence,
                judgment.result == "unsure"
                or bool(judgment.uncertainties)
                or bool(policy_unknowns),
                criteria,
                self.settings,
            )
            if incomplete_product_context and judgment.result == "no_match":
                decision = "unsure"
            uncertainties = policy_unknowns + tuple(
                Reason(code="semantic_uncertainty", detail=x) for x in judgment.uncertainties
            )
            if incomplete_product_context and judgment.result == "no_match":
                uncertainties += (
                    Reason(
                        code="unexamined_products",
                        detail=(
                            f"Semantic context included {product_coverage}; omitted products "
                            "remain unexamined"
                        ),
                    ),
                )
            if decision == "unsure" and not uncertainties:
                uncertainties = (
                    Reason(
                        code="below_decision_threshold",
                        detail="Relevance or reliability is insufficient for this campaign mode",
                    ),
                )
            return context_result(
                MatchResult(
                    result=decision,
                    score=judgment.score,
                    confidence=judgment.confidence,
                    reasons=tuple(
                        Reason(code="semantic_relevance", detail=x) for x in judgment.reasons
                    ),
                    uncertainties=uncertainties,
                    evidence=previous.evidence + judgment.evidence,
                    model_calls=previous.model_calls + (call,),
                    decision_stage=stage,
                    matcher_implementation=self.implementation,
                    matcher_version=self.version,
                    tender_version_id=tender.current_version_id,
                ),
                request.payload.get("document_selection"),
            )
        except Exception as error:
            # Never persist exception text: providers may include headers or sensitive content.
            if isinstance(error, ModelFailure):
                metadata = error.metadata
                call = ModelCall(
                    stage=stage,
                    provider=model.provider,
                    model=model.model,
                    model_version=metadata.model_version if metadata else call.model_version,
                    input_tokens=metadata.input_tokens if metadata else call.input_tokens,
                    output_tokens=metadata.output_tokens if metadata else call.output_tokens,
                    cost=metadata.cost if metadata else call.cost,
                    error_code=error.code,
                    failure_stage=error.failure_stage or failure_stage,
                    failure_detail=error.failure_detail,
                    http_status=error.http_status,
                    retry_after_seconds=error.retry_after_seconds,
                    attempts=metadata.attempts if metadata else call.attempts,
                    attempt_errors=metadata.attempt_errors if metadata else call.attempt_errors,
                )
            else:
                code = (
                    "timeout"
                    if self.version in {"2", "3"} and isinstance(error, TimeoutError)
                    else type(error).__name__
                )
                call = call.model_copy(update={"error_code": code, "failure_stage": failure_stage})
            response_failure = call.failure_stage in {
                "response_parsing",
                "response_validation",
            }
            evidence_failure = response_failure and call.error_code == "invalid_evidence"
            error_reason = (
                "evidence_verification_failed"
                if evidence_failure
                else "invalid_model_response"
                if response_failure
                else "model_unavailable"
            )
            error_detail = (
                "Optional model evidence failed verification; no citation was treated as verified"
                if evidence_failure
                else "Model responded, but its decision did not satisfy the response contract"
                if response_failure
                else "Model evaluation failed; relevance remains unresolved"
            )
            uncertainty_code = (
                "evidence_verification_failed" if evidence_failure else "model_failure"
            )
            return previous.model_copy(
                update={
                    "result": "unsure",
                    "score": 0.5 if previous.decision_stage == "rules" else previous.score,
                    "confidence": 0,
                    "reasons": (
                        Reason(
                            code=error_reason,
                            detail=error_detail,
                        ),
                    ),
                    "uncertainties": previous.uncertainties
                    + (
                        Reason(
                            code=uncertainty_code,
                            detail=f"{stage} evaluation failed ({call.error_code})",
                        ),
                    ),
                    "model_calls": previous.model_calls + (call,),
                    "decision_stage": stage,
                }
            )

    def _decision_result(
        self,
        previous: MatchResult,
        call: ModelCall,
        opinion: SemanticDecision,
        verification: EvidenceVerification,
        evidence: tuple[MatchEvidence, ...],
        criteria: CampaignCriteria,
        incomplete_product_context: bool,
        product_coverage: str,
    ) -> MatchResult:
        model_opinion = ModelOpinion.model_validate(opinion.model_dump())
        threshold = getattr(self.settings.thresholds, criteria.matching_mode).confidence
        if verification.status == "invalid":
            decision = "unsure"
            uncertainty = Reason(
                code="evidence_verification_failed",
                detail="Optional model evidence failed verification; model opinion was retained",
            )
        elif opinion.decision == "unsure":
            decision = "unsure"
            uncertainty = Reason(code="semantic_uncertainty", detail=opinion.explanation)
        elif verification.status != "verified" or not any(
            item.field in {"title", "description", "document_text", "requirements", "conditions"}
            or item.field.startswith("product_items.")
            for item in evidence
        ):
            decision = "unsure"
            uncertainty = Reason(
                code="evidence_required",
                detail="Model opinion lacks a verified procurement-scope citation. "
                "Run a new evaluation with source evidence.",
            )
        elif opinion.decision == "no_match" and incomplete_product_context:
            decision = "unsure"
            uncertainty = Reason(
                code="unexamined_products",
                detail=(
                    f"Semantic context included {product_coverage}; omitted products "
                    "remain unexamined"
                ),
            )
        elif opinion.confidence < threshold:
            decision = "unsure"
            uncertainty = Reason(
                code="below_confidence_threshold",
                detail="Model confidence is below the campaign decision threshold",
            )
        else:
            decision = opinion.decision
            uncertainty = None
        score = {"match": 1.0, "no_match": 0.0, "unsure": 0.5}[opinion.decision]
        return previous.model_copy(
            update={
                "result": decision,
                "score": score if decision != "unsure" else 0.5,
                "confidence": opinion.confidence if decision != "unsure" else 0,
                "reasons": (Reason(code="model_opinion", detail=opinion.explanation),),
                "uncertainties": (uncertainty,) if uncertainty is not None else (),
                "evidence": previous.evidence + evidence,
                "model_calls": previous.model_calls + (call,),
                "model_opinion": model_opinion,
                "evidence_verification": verification,
                "decision_stage": "semantic",
            }
        )


def context_result(result: MatchResult, selection: object) -> MatchResult:
    if not isinstance(selection, dict) or selection.get("complete") is not False:
        return result
    return result.model_copy(
        update={
            "result": "unsure",
            "score": 0.5,
            "confidence": 0,
            "uncertainties": result.uncertainties
            + (
                Reason(
                    code="document_context_incomplete",
                    detail=f"Reviewed {selection.get('included_passages')} of "
                    f"{selection.get('total_passages')} document passages. "
                    "Omitted requirements or conditions could change relevance. "
                    "Select a model with a larger input budget and run a new evaluation.",
                ),
            ),
        }
    )
