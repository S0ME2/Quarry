import json
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue, SecretStr

from tender_system.core.configuration import CampaignSettings, MatcherRouting, ModelSettings
from tender_system.core.domain import CampaignCriteria, MatchResult, Tender
from tender_system.core.ports import ModelClient, ModelFailure, ModelResponse
from tender_system.matching.intelligent_matching import IntelligentMatcher
from tender_system.matching.model_providers import GroqModelClient, OpenRouterModelClient
from tender_system.matching.semantic_decision import SemanticDecisionMatcher
from tender_system.testing.fakes import FakeClock, ScriptedModelClient

NOW = datetime(2026, 10, 6, tzinfo=UTC)
CRITERIA = CampaignCriteria(name="Network equipment", description="Supply of routers")


def tender(**changes: object) -> Tender:
    return Tender.model_validate(
        {
            "source_id": "fixture",
            "external_tender_id": "1",
            "internal_id": uuid4(),
            "current_version_id": uuid4(),
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            "title": "Ethernet switches",
            **changes,
        }
    )


def output(**changes: JsonValue) -> ModelResponse:
    return ModelResponse(
        output=cast(
            dict[str, JsonValue],
            {
                "decision": "match",
                "confidence": 0.95,
                "explanation": "The tender requests network equipment.",
                "evidence": [{"field": "title", "quote": "Ethernet switches"}],
                **changes,
            },
        ),
        model_version="fixture-model",
        input_tokens=21,
        output_tokens=8,
    )


def settings(provider: str = "fake", model: str = "fixture-model") -> CampaignSettings:
    return CampaignSettings(
        matcher_adapter="intelligent",
        matcher_version="3",
        batch_size=1,
        timeout_seconds=2,
        lease_seconds=10,
        model=ModelSettings(provider=provider, model=model, max_attempts=3),
    )


def run(
    client: ModelClient, *, config: CampaignSettings | None = None, item: Tender | None = None
) -> MatchResult:
    return IntelligentMatcher(
        config or settings(),
        FakeClock(NOW),
        lambda _: client,
    ).match(item or tender(), CRITERIA)


@pytest.mark.parametrize(
    "decision,expected",
    [("match", "unsure"), ("no_match", "unsure"), ("unsure", "unsure")],
)
def test_missing_evidence_preserves_opinion_but_prevents_final_decision(
    decision: str, expected: str
) -> None:
    response = output(decision=decision, evidence=[])
    result = run(ScriptedModelClient((response,)))

    assert result.result == expected
    assert result.model_opinion is not None
    assert result.model_opinion.decision == decision
    assert result.model_opinion.confidence == 0.95
    assert result.model_opinion.explanation == response.output["explanation"]
    assert result.evidence_verification is not None
    assert result.evidence_verification.status == "not_provided"
    assert result.evidence == ()


def test_invalid_optional_evidence_preserves_opinion_and_blocks_final_decision() -> None:
    client = ScriptedModelClient(
        (output(evidence=[{"field": "missing_tender_field", "quote": "invented"}]),)
    )
    result = run(client)

    assert result.result == "unsure"
    assert result.model_opinion is not None and result.model_opinion.decision == "match"
    assert result.model_opinion.confidence == 0.95
    assert result.evidence_verification is not None
    assert result.evidence_verification.status == "invalid"
    assert result.evidence_verification.error_code == "invalid_evidence"
    assert result.evidence_verification.detail == "unrecognized_field_path"
    assert result.evidence == ()
    assert result.model_calls[0].error_code is None
    assert "model_unavailable" not in {reason.code for reason in result.reasons}
    assert len(client.requests) == 1 and client.requests[0].max_attempts == 1


@pytest.mark.parametrize(
    "bad_output",
    [
        {"decision": "maybe", "confidence": 0.9, "explanation": "No decision."},
        {"decision": "match", "confidence": 1.1, "explanation": "Invalid confidence."},
        {"decision": "match", "confidence": 0.9},
        {
            "decision": "match",
            "confidence": 0.9,
            "explanation": "Unexpected output.",
            "extra": "reject",
        },
    ],
)
def test_malformed_decisions_are_rejected_without_calling_them_unavailable(
    bad_output: dict[str, JsonValue],
) -> None:
    result = run(
        ScriptedModelClient((ModelResponse(output=bad_output, model_version="fixture-model"),))
    )
    assert result.result == "unsure" and result.model_opinion is None
    assert result.model_calls[0].error_code == "invalid_judgment"
    assert result.model_calls[0].failure_stage == "response_validation"
    assert {reason.code for reason in result.reasons} == {"invalid_model_response"}


def test_provider_failure_remains_technical_and_is_not_retried() -> None:
    client = ScriptedModelClient(
        (ModelFailure("temporary_provider_error", failure_stage="provider_response"),)
    )
    result = run(client)

    assert result.result == "unsure" and result.model_opinion is None
    assert result.model_calls[0].error_code == "temporary_provider_error"
    assert result.model_calls[0].failure_stage == "provider_response"
    assert {reason.code for reason in result.reasons} == {"model_unavailable"}
    assert len(client.requests) == 1 and client.requests[0].max_attempts == 1


def test_partial_document_review_spends_nothing_and_full_context_can_be_reevaluated() -> None:
    item = tender(document_text="Technical background and contractual obligations. " * 300)
    client = ScriptedModelClient((output(),))
    small = settings().model_copy(
        update={"model": settings().model.model_copy(update={"max_input_bytes": 4000})}
    )
    result = run(client, config=small, item=item)
    assert result.result == "unsure" and not result.model_calls and not client.requests
    assert any(r.code == "document_context_incomplete" for r in result.uncertainties)
    large = small.model_copy(
        update={"model": small.model.model_copy(update={"max_input_bytes": 24000})}
    )
    recovered = run(client, config=large, item=item)
    assert recovered.result == "match" and len(client.requests) == 1
    assert "document_selection" not in client.requests[0].payload


def test_version_three_does_not_run_escalation_requests() -> None:
    client = ScriptedModelClient((output(decision="unsure", confidence=0.4),))
    config = settings().model_copy(
        update={
            "routing": MatcherRouting(
                escalation_enabled=True,
                stronger_model=ModelSettings(provider="fake", model="stronger"),
            )
        }
    )
    result = run(client, config=config)
    assert result.result == "unsure"
    assert len(client.requests) == len(result.model_calls) == 1


def test_decision_schema_contains_provider_neutral_contract_only() -> None:
    config = settings()
    request = SemanticDecisionMatcher().request(
        tender(), CRITERIA, config.model.model_copy(update={"structured_output": "schema"}), 2
    )
    assert request.max_attempts == 1
    assert request.output_schema is not None
    properties = request.output_schema["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {
        "decision",
        "confidence",
        "explanation",
        "evidence",
    }


def test_hard_rule_exclusion_precedes_model_opinion() -> None:
    client = ScriptedModelClient(())
    result = run(
        client,
        item=tender(status="closed"),
    )
    assert result.result == "no_match" and result.decision_stage == "rules"
    assert result.model_opinion is None and not result.model_calls and not client.requests


@pytest.mark.parametrize("product_count", [1, 19])
@pytest.mark.parametrize("decision", ["match", "no_match"])
def test_negative_decision_requires_complete_product_context(
    product_count: int, decision: str
) -> None:
    client = ScriptedModelClient((output(decision=decision),))
    result = run(
        client,
        item=tender(product_items=[{"name": f"Item {i}"} for i in range(product_count)]),
    )
    assert result.model_opinion is not None and result.model_opinion.decision == decision
    selection = client.requests[0].payload["product_selection"]
    assert isinstance(selection, dict)
    assert selection["complete"] is (product_count == 1)
    if product_count == 19 and decision == "no_match":
        assert result.result == "unsure"
        assert {reason.code for reason in result.uncertainties} == {"unexamined_products"}
        assert result.score == 0.5 and result.confidence == 0
    else:
        assert result.result == decision


@pytest.mark.parametrize(
    ("adapter", "provider"),
    [(GroqModelClient, "groq"), (OpenRouterModelClient, "openrouter")],
)
def test_decision_contract_is_shared_by_provider_adapters(
    adapter: type[GroqModelClient] | type[OpenRouterModelClient], provider: str
) -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "model": "fixture-model",
                "usage": {"prompt_tokens": 21, "completion_tokens": 8},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "match",
                                    "confidence": 0.95,
                                    "explanation": "The tender requests network equipment.",
                                    "evidence": [{"field": "title", "quote": "Ethernet switches"}],
                                }
                            )
                        },
                    }
                ],
            },
        )

    client = adapter(SecretStr("offline-key"), httpx.MockTransport(handle))
    config = settings(provider=provider)
    config = config.model_copy(
        update={"model": config.model.model_copy(update={"structured_output": "schema"})}
    )
    result = run(client, config=config)

    assert result.result == "match" and result.model_opinion is not None
    assert result.model_opinion.decision == "match"
    assert result.evidence_verification is not None
    assert result.evidence_verification.status == "verified"
    assert len(sent) == 1
    body = json.loads(sent[0].content)
    assert body["model"] == "fixture-model"
    assert body["response_format"]["type"] == "json_schema"
    assert result.model_calls[0].attempts == 1


@pytest.mark.parametrize("decision", ["match", "no_match"])
def test_partial_document_context_cannot_finalize_even_confident_decisions(decision: str) -> None:
    client = ScriptedModelClient((output(decision=decision, confidence=1.0),))
    config = settings()
    config = config.model_copy(
        update={"model": config.model.model_copy(update={"max_input_bytes": 4000})}
    )
    result = run(
        client,
        config=config,
        item=tender(
            document_text=(
                "Background terms and obligations. " * 2000 + "Supplier must deliver solar lights."
            )
        ),
    )
    assert not client.requests and not result.model_calls
    assert result.result == "unsure"
    assert "document_context_incomplete" in {reason.code for reason in result.uncertainties}
    assert result.model_opinion is None


def test_missing_citations_are_not_counted_as_genuine_model_uncertainty() -> None:
    from tender_system.matching.matcher_evaluation import metrics

    result = run(ScriptedModelClient((output(evidence=[]),)))
    report = metrics([("case", "unsure", result)])
    assert report["technical_failure_unsure_rate"] == 1
    assert report["genuine_unsure_rate"] == 0
    assert report["unsure_causes"] == {"citation_evidence_rejection": 1}
