import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue, SecretStr

from tender_system.core.configuration import CampaignSettings, ModelSettings
from tender_system.core.domain import CampaignCriteria, Tender
from tender_system.core.ports import ModelClient, ModelFailure, ModelRequest, ModelResponse
from tender_system.matching.calibrated_semantics import CalibratedSemanticMatcher, strict_schema
from tender_system.matching.intelligent_matching import IntelligentMatcher
from tender_system.matching.model_providers import GroqModelClient, retry_after
from tender_system.matching.semantic_matching import source_quote
from tender_system.testing.fakes import FakeClock, ScriptedModelClient

NOW = datetime(2026, 10, 6, tzinfo=UTC)
FAILURES = json.loads(Path("tests/fixtures/matcher/phase9_failures.json").read_text())
CONFIDENT = [row for row in FAILURES if row["recorded"]["result"] != "unsure"]


def matcher(settings: CampaignSettings, client: ModelClient) -> IntelligentMatcher:
    return IntelligentMatcher(settings, FakeClock(NOW), lambda _: client)


def tender(fields: dict[str, object]) -> Tender:
    return Tender.model_validate(
        {
            "source_id": "test",
            "external_tender_id": "1",
            "internal_id": uuid4(),
            "current_version_id": uuid4(),
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            **fields,
        }
    )


def recorded_response(row: dict[str, object]) -> ModelResponse:
    recorded = cast(dict[str, object], row["recorded"])
    return ModelResponse(
        output=cast(
            dict[str, JsonValue],
            {
                "result": recorded["result"],
                "score": recorded["score"],
                "confidence": recorded["confidence"],
                "reasons": [r["detail"] for r in cast(list[dict[str, str]], recorded["reasons"])],
                "uncertainties": [],
                "evidence": recorded["evidence"],
            },
        ),
        model_version="phase9-recorded",
    )


@pytest.mark.parametrize("row", CONFIDENT, ids=[f"{r['id']}/{r['mode']}" for r in CONFIDENT])
def test_original_failures_reproduce_and_hardened_version_abstains(row: dict[str, object]) -> None:
    item = tender(cast(dict[str, object], row["tender"]))
    criteria = CampaignCriteria.model_validate(
        {**cast(dict[str, object], row["campaign"]), "matching_mode": row["mode"]}
    )
    for version, expected in (
        ("1", cast(dict[str, str], row["recorded"])["result"]),
        ("2", "unsure"),
    ):
        client = ScriptedModelClient((recorded_response(row),))
        result = matcher(
            CampaignSettings(matcher_adapter="intelligent", matcher_version=version),
            client,
        ).match(item, criteria)
        assert result.result == expected


def settings(**overrides: object) -> CampaignSettings:
    return CampaignSettings.model_validate(
        {"matcher_adapter": "intelligent", "matcher_version": "2", **overrides}
    )


def judgment(**overrides: JsonValue) -> ModelResponse:
    return ModelResponse(
        output={
            "result": "match",
            "score": 0.98,
            "confidence": 0.99,
            "reasons": ["Routers satisfy the requested equipment category."],
            "uncertainties": [],
            "evidence": [{"field": "title", "quote": "Network routers"}],
            "field_assessments": [
                {
                    "field": "title",
                    "quote": "Network routers",
                    "relation": "direct",
                    "detail": "Router supply",
                }
            ],
            "scope_relation": "direct",
            "support": "explicit",
            "evidence_conflicts": False,
            "criteria_conflicts": False,
            "critical_missing_information": [],
            "justification": "clear",
            **overrides,
        },
        model_version="manual-fake-v2",
        input_tokens=42,
    )


def test_calibrated_semantic_evidence_can_cite_a_product_item() -> None:
    item = tender(
        {
            "title": "Office equipment lot",
            "description": "General office equipment",
            "raw_data": {
                "product_items": [
                    {"Product_Name": "Laser printer", "Description": "A3 duplex color"}
                ]
            },
        }
    )
    accepted = CalibratedSemanticMatcher().validate(
        judgment(
            evidence=[{"field": "product_items.0.Description", "quote": "A3 duplex color"}],
            field_assessments=[
                {
                    "field": "title",
                    "quote": "Office equipment lot",
                    "relation": "direct",
                    "detail": "Office equipment purchase",
                },
                {
                    "field": "description",
                    "quote": "General office equipment",
                    "relation": "direct",
                    "detail": "General equipment scope",
                },
            ],
        ),
        item,
    )
    assert accepted.evidence[0].field == "product_items.0.Description"


@pytest.mark.parametrize(
    "source,quote",
    [
        ("Кабель — USB‑C, “оригинал”…", 'Кабель - USB-C, "оригинал"...'),
        ("A  B\nC", "A B C"),
        ("Café", "Cafe\u0301"),
    ],
)
def test_source_quote_accepts_format_equivalents_and_returns_source_span(
    source: str, quote: str
) -> None:
    assert source_quote(quote, source) == source


@pytest.mark.parametrize(
    "source,quote",
    [
        ("Network router", "Network switch"),
        ("Кабель USB‑C", "Kabel USB-C"),
        ("Part ﬁve", "Part f"),
    ],
)
def test_source_quote_rejects_fabricated_translated_or_partial_evidence(
    source: str, quote: str
) -> None:
    assert source_quote(quote, source) is None


@pytest.mark.parametrize("mode", ("broad", "balanced", "strict"))
def test_conflicting_subject_fields_abstain_even_if_model_denies_conflict(mode: str) -> None:
    client = ScriptedModelClient(
        (
            judgment(
                field_assessments=[
                    {
                        "field": "title",
                        "quote": "Network routers",
                        "relation": "direct",
                        "detail": "Router supply",
                    },
                    {
                        "field": "description",
                        "quote": "Only desks",
                        "relation": "unrelated",
                        "detail": "Office furniture",
                    },
                ]
            ),
        )
    )
    result = matcher(
        settings(
            routing={
                "escalation_enabled": True,
                "stronger_model": {"provider": "fake", "model": "strong"},
            }
        ),
        client,
    ).match(
        tender({"title": "Network routers", "description": "Only desks"}),
        CampaignCriteria.model_validate(
            {"name": "Networks", "description": "Router supply", "matching_mode": mode}
        ),
    )
    assert result.result == "unsure"
    assert "evidence_conflict" in {r.code for r in result.uncertainties}
    assert len(client.requests) == 1  # Escalation cannot manufacture missing evidence.


@pytest.mark.parametrize(
    "mode,expected", [("broad", "match"), ("balanced", "unsure"), ("strict", "unsure")]
)
def test_adjacent_inferred_scope_cannot_become_confident_from_high_score(
    mode: str, expected: str
) -> None:
    client = ScriptedModelClient((judgment(scope_relation="adjacent", support="inferred"),))
    result = matcher(settings(), client).match(
        tender({"title": "Network routers"}),
        CampaignCriteria.model_validate(
            {"name": "C", "description": "Infrastructure", "matching_mode": mode}
        ),
    )
    assert result.result == expected


@pytest.mark.parametrize(
    "change,code",
    [
        ({"justification": "weak"}, "weak_justification"),
        ({"scope_relation": "unknown"}, "weak_justification"),
        ({"criteria_conflicts": True}, "criteria_conflict"),
        (
            {"critical_missing_information": ["Required certification is not supplied"]},
            "critical_missing_information",
        ),
        ({"score": 0.72}, "borderline_relevance"),
    ],
)
def test_high_confidence_cannot_clear_uncertainty(change: dict[str, JsonValue], code: str) -> None:
    result = matcher(settings(), ScriptedModelClient((judgment(**change),))).match(
        tender({"title": "Network routers"}),
        CampaignCriteria(name="C", description="Router supply"),
    )
    assert result.result == "unsure"
    assert code in {r.code for r in result.uncertainties}


@pytest.mark.parametrize(
    "change,code",
    [
        ({"field_assessments": []}, "incomplete_assessment"),
        (
            {"evidence": [{"field": "campaign.description", "quote": "Router supply"}]},
            "invalid_evidence",
        ),
        (
            {"evidence": [{"field": "title", "quote": "Invented specifications"}]},
            "invalid_evidence",
        ),
        ({"evidence_conflicts": "false"}, "invalid_judgment"),
        ({"evidence": [{"field": "buyer", "quote": "Acme"}]}, "insufficient_subject_evidence"),
    ],
)
def test_incomplete_or_invented_output_safely_abstains(
    change: dict[str, JsonValue], code: str
) -> None:
    result = matcher(settings(), ScriptedModelClient((judgment(**change),))).match(
        tender({"title": "Network routers", "buyer": "Acme"}),
        CampaignCriteria(name="C", description="Router supply"),
    )
    assert result.result == "unsure" and result.model_calls[0].error_code == code
    assert result.model_calls[0].input_tokens == 42


def test_missing_required_fields_stops_before_ai_but_optional_missing_fields_do_not() -> None:
    client = ScriptedModelClient((judgment(),))
    item = tender({"title": "Network routers"})
    criteria = CampaignCriteria(name="C", description="Router supply")
    result = matcher(settings(uncertainty={"required_fields": ["location"]}), client).match(
        item, criteria
    )
    assert result.result == "unsure" and not client.requests
    assert matcher(settings(), client).match(item, criteria).result == "match"


def test_conflicting_campaign_criteria_is_unsure_without_ai() -> None:
    client = ScriptedModelClient(())
    result = matcher(settings(), client).match(
        tender({"title": "Network routers"}),
        CampaignCriteria(name="C", buyers=("Acme",), exclude_buyers=("Acme",)),
    )
    assert result.result == "unsure" and result.decision_stage == "rules"
    assert not client.requests


def test_uncertainty_configuration_changes_v2_identity_but_preserves_v1_identity() -> None:
    from tender_system.matching.matching import settings_digest

    for version in ("1", "2"):
        first = settings(matcher_version=version)
        changed = first.model_copy(
            update={"uncertainty": first.uncertainty.model_copy(update={"borderline_margin": 0.1})}
        )
        assert (settings_digest(first) == settings_digest(changed)) == (version == "1")


def test_borderline_escalation_uses_same_guards_and_preserves_call_versions() -> None:
    client = ScriptedModelClient((judgment(score=0.72), judgment()))
    result = matcher(
        settings(
            routing={
                "escalation_enabled": True,
                "stronger_model": {"provider": "other", "model": "strong"},
            }
        ),
        client,
    ).match(
        tender({"title": "Network routers"}),
        CampaignCriteria(name="C", description="Router supply"),
    )
    assert result.result == "match" and result.decision_stage == "escalation"
    assert [c.stage for c in result.model_calls] == ["semantic", "escalation"]
    assert result.model_calls[1].provider == "other"
    assert result.matcher_version == "2"
    assert all(c.model_version == "manual-fake-v2" for c in result.model_calls)


def test_uncertainty_policy_can_be_configured_without_provider_changes() -> None:
    result = matcher(
        settings(
            uncertainty={
                "adjacent_modes": ["broad", "balanced"],
                "inferred_modes": ["broad", "balanced"],
            }
        ),
        ScriptedModelClient((judgment(scope_relation="adjacent", support="inferred"),)),
    ).match(
        tender({"title": "Network routers"}),
        CampaignCriteria(name="C", description="Infrastructure"),
    )
    assert result.result == "match"


@pytest.mark.parametrize("fixture", ("synthetic", "real"))
def test_v2_benchmarks_are_offline_reproducible_and_have_no_wrong_confident_decisions(
    fixture: str,
) -> None:
    from tender_system.matching.matcher_evaluation import Benchmark, ReplayClient, evaluate

    name = "matcher_synthetic_v2" if fixture == "synthetic" else "matcher_real_v2"
    replay = "matcher_replay_v2" if fixture == "synthetic" else "matcher_real_replay_v2"
    benchmark = Benchmark.model_validate_json(Path(f"benchmarks/matcher/{name}.json").read_text())
    outputs = json.loads(Path(f"benchmarks/matcher/{replay}.json").read_text())
    clients: dict[str, ModelClient] = {
        case.id: ReplayClient(outputs[case.id]) for case in benchmark.cases if case.id in outputs
    }
    first = evaluate(benchmark, settings(), clients)
    assert first == evaluate(benchmark, settings(), clients)
    aggregate = cast(dict[str, object], first["aggregate"])
    assert aggregate["wrong_confident_decisions"] == 0
    assert aggregate["decision_accuracy"] == 1
    if fixture == "real":
        for case in benchmark.cases:
            assert case.provenance["source_url"] == case.tender["source_url"]
            assert case.tender["current_version_id"]


def request() -> ModelRequest:
    return ModelRequest(model="arbitrary", task="test", payload={}, timeout_seconds=1)


@pytest.mark.parametrize(
    "status,code",
    [
        (429, "rate_limited"),
        (503, "temporary_provider_error"),
        (401, "authentication_error"),
        (403, "authentication_error"),
        (400, "provider_rejected"),
    ],
)
def test_http_errors_are_classified_without_retaining_sensitive_body(
    status: int, code: str
) -> None:
    requests = []

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return httpx.Response(
            status, json={"error": {"message": "private-body"}}, headers={"Retry-After": "2"}
        )

    client = GroqModelClient(SecretStr("private-key"), httpx.MockTransport(handle))
    with pytest.raises(ModelFailure) as failure:
        client.generate(request())
    assert failure.value.code == code and failure.value.http_status == status
    assert failure.value.retry_after_seconds == 2
    assert "private" not in str(failure.value)
    assert len(requests) == 1  # Retry-After exceeds this request's one-second deadline.
    result = matcher(settings(), client).match(
        tender({"title": "Network routers"}), CampaignCriteria(name="C", description="Networks")
    )
    assert result.result == "unsure" and result.model_calls[0].http_status == status
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize(
    "content,finish,code",
    [
        ("{", "stop", "malformed_json"),
        ('{"result":"match","result":"no_match"}', "stop", "malformed_json"),
        ('{"score":NaN}', "stop", "malformed_json"),
        ("[]", "stop", "invalid_judgment"),
        ("{}", "length", "truncated_output"),
        ("{}", None, "incomplete_output"),
        (None, "stop", "malformed_response"),
    ],
)
def test_bad_output_abstains_and_retains_usage(content: object, finish: object, code: str) -> None:
    client = GroqModelClient(
        SecretStr("fake"),
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "model": "provider-version",
                    "usage": {"prompt_tokens": 123, "completion_tokens": 45},
                    "choices": [{"finish_reason": finish, "message": {"content": content}}],
                },
            )
        ),
    )
    result = matcher(settings(), client).match(
        tender({"title": "Network routers"}), CampaignCriteria(name="C", description="Networks")
    )
    assert result.result == "unsure"
    call = result.model_calls[0]
    assert call.error_code == code and call.input_tokens == 246 and call.output_tokens == 90
    assert call.attempts == 2 and call.attempt_errors == (code, code)
    assert call.model_version == "provider-version"


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"choices": []},
        {"choices": [1]},
        {"model": 42},
        {"error": {"message": "secret"}},
    ],
)
def test_incomplete_envelope_safely_fails(body: object) -> None:
    client = GroqModelClient(
        SecretStr("fake"), httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    with pytest.raises(ModelFailure):
        client.generate(request())


@pytest.mark.parametrize(
    "exception,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "transport_error")]
)
def test_transport_failure_safely_abstains(
    exception: type[httpx.TransportError], code: str
) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        raise exception("secret exception text", request=req)

    client = GroqModelClient(SecretStr("fake"), httpx.MockTransport(handle))
    result = matcher(settings(), client).match(
        tender({"title": "Network routers"}), CampaignCriteria(name="C", description="Networks")
    )
    assert result.result == "unsure" and result.model_calls[0].error_code == code
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize("value", ("not-a-date", "-1", "nan", "inf", ""))
def test_invalid_retry_after_is_optional(value: str) -> None:
    assert retry_after(value) is None


def test_retry_after_date_and_schema_validation_failure() -> None:
    assert (
        0
        < cast(float, retry_after(format_datetime(datetime.now(UTC) + timedelta(seconds=60))))
        <= 60
    )
    client = GroqModelClient(
        SecretStr("fake"),
        httpx.MockTransport(
            lambda _: httpx.Response(
                400, json={"error": {"code": "json_validate_failed", "message": "private-output"}}
            )
        ),
    )
    with pytest.raises(ModelFailure, match="provider_output_invalid"):
        client.generate(request())


def test_v2_schema_is_complete_and_provider_adapter_forwards_it() -> None:
    schema = strict_schema()

    def check(node: object) -> None:
        if isinstance(node, dict):
            if "properties" in node:
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)
    req = CalibratedSemanticMatcher().request(
        tender({"title": "Network routers"}),
        CampaignCriteria(name="C"),
        ModelSettings(structured_output="schema"),
        1,
    )
    assert req.max_output_tokens == 600
    assert "output_contract" not in req.payload

    def handle(sent: httpx.Request) -> httpx.Response:
        body = json.loads(sent.content)
        assert body["response_format"]["json_schema"]["schema"] == schema
        assert body["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        )

    GroqModelClient(SecretStr("fake"), httpx.MockTransport(handle)).generate(req)


@pytest.mark.parametrize("failure_kind", ["wrong_type", "missing_fields"])
def test_groq_invalid_calibrated_judgment_is_rejected_without_retry(
    failure_kind: str,
) -> None:
    from dataclasses import replace

    sent: list[httpx.Request] = []
    output = dict(judgment().output) if failure_kind == "wrong_type" else {}
    if failure_kind == "wrong_type":
        output["evidence_conflicts"] = "false"

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "model": "qwen/qwen3.8-27b",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}],
            },
        )

    item = tender({"title": "Network routers"})
    criteria = CampaignCriteria(name="C", description="Router supply")
    request = CalibratedSemanticMatcher().request(item, criteria, ModelSettings(), 5)
    request = replace(
        request,
        max_attempts=1,
        validate_output=lambda response: CalibratedSemanticMatcher().validate(
            response, item, request
        ),
    )
    with pytest.raises(ModelFailure) as failure:
        GroqModelClient(SecretStr("fake"), httpx.MockTransport(handle)).generate(request)

    assert failure.value.code == "invalid_judgment"
    assert failure.value.metadata is not None
    assert failure.value.metadata.attempts == 1
    assert failure.value.metadata.attempt_errors == ("invalid_judgment",)
    assert len(sent) == 1


def test_groq_v1_validation_value_error_keeps_response_metadata_and_abstains() -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "model": "qwen/qwen3.8-27b",
                "usage": {"prompt_tokens": 31, "completion_tokens": 12},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "result": "match",
                                    "score": 0.9,
                                    "confidence": 0.9,
                                    "reasons": ["The tender is relevant."],
                                    "uncertainties": [],
                                    "evidence": [
                                        {"field": "campaign.description", "quote": "network"}
                                    ],
                                }
                            )
                        },
                    }
                ],
            },
        )

    item = tender({"title": "Network routers"})
    config = CampaignSettings(
        matcher_adapter="intelligent",
        matcher_version="1",
        model=ModelSettings(provider="groq", model="qwen/qwen3.8-27b", max_attempts=1),
    )
    client = GroqModelClient(SecretStr("fake"), httpx.MockTransport(handle))
    outcome = IntelligentMatcher(config, FakeClock(NOW), lambda _: client).match(
        item, CampaignCriteria(name="Network", description="network equipment")
    )

    assert len(sent) == 1
    body = json.loads(sent[0].content)
    assert body["model"] == "qwen/qwen3.8-27b"
    assert body["response_format"] == {"type": "json_object"}
    assert outcome.result == "unsure" and outcome.confidence == 0
    call = outcome.model_calls[0]
    assert call.error_code == "invalid_evidence"
    assert call.failure_stage == "response_validation"
    assert call.failure_detail == "field_not_supplied:campaign.*"
    assert call.input_tokens == 31 and call.output_tokens == 12
    assert call.model_version == "qwen/qwen3.8-27b"
    assert call.attempts == 1
    assert "evidence_verification_failed" in {reason.code for reason in outcome.reasons}
    assert "model_unavailable" not in {reason.code for reason in outcome.reasons}


def test_groq_v2_structured_decision_passes_full_validation_offline() -> None:
    sent: list[httpx.Request] = []
    item = tender({"title": "Network routers"})
    criteria = CampaignCriteria(name="Network", description="router supply")
    settings_v2 = CampaignSettings(
        matcher_adapter="intelligent",
        matcher_version="2",
        model=ModelSettings(
            provider="groq",
            model="qwen/qwen3.8-27b",
            structured_output="schema",
            max_attempts=1,
        ),
    )

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        output = dict(judgment().output)
        output["field_assessments"] = [
            {
                "field": "title",
                "quote": "Network routers",
                "relation": "direct",
                "detail": "Router supply",
            }
        ]
        return httpx.Response(
            200,
            json={
                "model": "qwen/qwen3.8-27b",
                "usage": {"prompt_tokens": 80, "completion_tokens": 45},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(output)},
                    }
                ],
            },
        )

    client = GroqModelClient(SecretStr("fake"), httpx.MockTransport(handle))
    outcome = IntelligentMatcher(settings_v2, FakeClock(NOW), lambda _: client).match(
        item, criteria
    )
    assert len(sent) == 1
    assert outcome.result == "match" and outcome.confidence == 0.99
    call = outcome.model_calls[0]
    assert call.error_code is None and call.failure_stage is None
    assert call.input_tokens == 80 and call.output_tokens == 45


@pytest.mark.parametrize(
    "row",
    [r for r in FAILURES if r["recorded"]["result"] == "unsure"],
    ids=lambda r: f"{r['id']}/{r['mode']}",
)
def test_each_historical_output_failure_stays_unsure(row: dict[str, object]) -> None:
    recorded = cast(dict[str, object], row["recorded"])
    # The original status was not retained. A classified temporary error exercises the fallback
    # without inventing a historical HTTP cause. ValueError rows reproduce campaign evidence.
    response = (
        ModelFailure("temporary_provider_error", http_status=503)
        if cast(list[dict[str, object]], recorded["model_calls"])[0]["error_code"]
        == "HTTPStatusError"
        else judgment(evidence=[{"field": "campaign.description", "quote": "Networks"}])
    )
    client = ScriptedModelClient((response,))
    result = matcher(settings(), client).match(
        tender(cast(dict[str, object], row["tender"])),
        CampaignCriteria.model_validate(
            {**cast(dict[str, JsonValue], row["campaign"]), "matching_mode": row["mode"]}
        ),
    )
    assert result.result == "unsure" and result.model_calls[0].error_code
    assert result.model_calls[0].error_code == (
        "temporary_provider_error" if isinstance(response, ModelFailure) else "invalid_evidence"
    )


def test_provider_failures_do_not_escalate_by_default_but_can_be_configured() -> None:
    routing = {
        "escalation_enabled": True,
        "stronger_model": {"provider": "other", "model": "strong"},
    }
    item = tender({"title": "Network routers"})
    criteria = CampaignCriteria(name="C", description="Networks")
    first = ScriptedModelClient((ModelFailure("rate_limited", http_status=429), judgment()))
    assert matcher(settings(routing=routing), first).match(item, criteria).result == "unsure"
    assert len(first.requests) == 1
    second = ScriptedModelClient(
        (ModelFailure("temporary_provider_error", http_status=503), judgment())
    )
    assert (
        matcher(settings(routing=routing, uncertainty={"escalate_provider_failures": True}), second)
        .match(item, criteria)
        .result
        == "match"
    )
    assert len(second.requests) == 2


@pytest.mark.parametrize(
    "mode,expected", [("broad", "match"), ("balanced", "match"), ("strict", "unsure")]
)
def test_partial_scope_policy_across_modes(mode: str, expected: str) -> None:
    result = matcher(settings(), ScriptedModelClient((judgment(scope_relation="partial"),))).match(
        tender({"title": "Network routers"}),
        CampaignCriteria.model_validate(
            {"name": "C", "description": "Networks", "matching_mode": mode}
        ),
    )
    assert result.result == expected


@pytest.mark.parametrize(
    "field,value,criteria",
    [
        ("value", 5, {"min_value": 10}),
        ("deadline", "2026-10-01T00:00:00Z", {}),
        ("title", "Network routers", {"description": "Networks"}),
    ],
)
def test_quality_errors_on_decision_fields_force_abstention(
    field: str, value: object, criteria: dict[str, object]
) -> None:
    client = ScriptedModelClient(())
    result = matcher(settings(), client).match(
        tender(
            {
                field: value,
                "quality_issues": [
                    {
                        "field": field,
                        "code": "invalid",
                        "detail": "Normalization failed",
                        "severity": "error",
                    }
                ],
            }
        ),
        CampaignCriteria.model_validate({"name": "C", **criteria}),
    )
    assert result.result == "unsure" and not client.requests
    assert "unreliable_evidence" in {r.code for r in result.uncertainties}


@pytest.mark.parametrize("change", [{"score": True}, {"score": "0.98"}, {"confidence": "0.99"}])
def test_coerced_provider_scores_are_invalid(change: dict[str, JsonValue]) -> None:
    result = matcher(settings(), ScriptedModelClient((judgment(**change),))).match(
        tender({"title": "Network routers"}), CampaignCriteria(name="C", description="Networks")
    )
    assert result.result == "unsure" and result.model_calls[0].error_code == "invalid_judgment"


def test_high_score_with_unrelated_assessment_is_unsure() -> None:
    result = matcher(
        settings(), ScriptedModelClient((judgment(scope_relation="unrelated", support="none"),))
    ).match(
        tender({"title": "Network routers"}), CampaignCriteria(name="C", description="Networks")
    )
    assert result.result == "unsure"
    assert "inconsistent_assessment" in {r.code for r in result.uncertainties}


@pytest.mark.parametrize("score,expected", [(0.949, "unsure"), (0.95, "match"), (0.951, "match")])
def test_borderline_margin_excludes_exact_endpoint_without_float_rounding(
    score: float, expected: str
) -> None:
    result = matcher(settings(), ScriptedModelClient((judgment(score=score),))).match(
        tender({"title": "Network routers"}),
        CampaignCriteria(name="C", description="Networks", matching_mode="strict"),
    )
    assert result.result == expected


def test_real_mixed_script_evidence_cannot_be_normalized_or_paraphrased() -> None:
    from tender_system.matching.matcher_evaluation import Benchmark, case_tender

    benchmark = Benchmark.model_validate_json(
        Path("benchmarks/matcher/matcher_real_v2.json").read_text()
    )
    case = benchmark.cases[0]
    item = case_tender(case, benchmark.evaluated_at)
    output = json.loads(Path("tests/fixtures/matcher/unicode_evidence_failure.json").read_text())
    bad = ModelResponse(output=output, model_version="recorded-qwen-diagnostic")
    result = matcher(settings(), ScriptedModelClient((bad,))).match(item, case.campaign)
    assert result.result == "unsure" and result.model_calls[0].error_code == "invalid_evidence"
    # A short exact source excerpt remains accepted: no fuzzy/translated evidence repair.
    quote = "kompyuter texnikasi"
    assert quote in (item.title or "") and quote in (item.description or "")
    output["score"] = 0.98
    output["evidence"] = [{"field": f, "quote": quote} for f in ("title", "description")]
    for assessment in output["field_assessments"]:
        assessment["quote"] = quote
    repaired = ModelResponse(output=output, model_version="manual-exact-evidence")
    assert (
        matcher(settings(), ScriptedModelClient((repaired,))).match(item, case.campaign).result
        == "match"
    )


@pytest.mark.parametrize("status", [429, 503])
def test_groq_transient_recovery_is_bounded_and_counted(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    waits: list[float] = []
    requests: list[httpx.Request] = []
    monkeypatch.setattr("tender_system.matching.model_providers.sleep", waits.append)

    def handle(sent: httpx.Request) -> httpx.Response:
        requests.append(sent)
        if len(requests) < 3:
            return httpx.Response(status, headers={"Retry-After": "2"})
        return httpx.Response(
            200,
            json={
                "model": "actual-version",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    response = GroqModelClient(SecretStr("fake"), httpx.MockTransport(handle)).generate(
        replace(request(), timeout_seconds=10)
    )
    assert len(requests) == 3 and waits == [2, 2]
    code = "rate_limited" if status == 429 else "temporary_provider_error"
    assert response.attempts == 3 and response.attempt_errors == (code, code)
    assert response.input_tokens == 10 and response.output_tokens == 5


def test_groq_outage_stops_at_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    sent: list[httpx.Request] = []
    waits: list[float] = []
    monkeypatch.setattr("tender_system.matching.model_providers.sleep", waits.append)

    def handle(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        raise httpx.ConnectError("private", request=req)

    with pytest.raises(ModelFailure) as failed:
        GroqModelClient(SecretStr("fake"), httpx.MockTransport(handle)).generate(
            replace(request(), timeout_seconds=10)
        )
    assert len(sent) == 3 and waits == [0.5, 1.0]
    assert failed.value.metadata is not None
    assert failed.value.metadata.attempts == 3
    assert failed.value.metadata.attempt_errors == ("transport_error",) * 3
    assert "private" not in str(failed.value)


def test_groq_retry_after_does_not_exceed_remaining_deadline() -> None:
    sent: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(429, headers={"Retry-After": "120"})

    with pytest.raises(ModelFailure) as failed:
        GroqModelClient(SecretStr("fake"), httpx.MockTransport(handle)).generate(request())
    assert len(sent) == 1 and failed.value.retry_after_seconds == 120
    assert failed.value.metadata is not None and failed.value.metadata.attempts == 1


@pytest.mark.parametrize("failure", ["schema", "json", "citation"])
def test_groq_output_regeneration_keeps_grounding_and_counts_usage(failure: str) -> None:
    sent: list[dict[str, object]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        if len(sent) == 1 and failure == "schema":
            return httpx.Response(400, json={"error": {"code": "json_validate_failed"}})
        output = judgment().output
        content = json.dumps(output)
        if len(sent) == 1:
            content = (
                "{"
                if failure == "json"
                else json.dumps(
                    {**output, "evidence": [{"field": "title", "quote": "Invented routers"}]}
                )
            )
        return httpx.Response(
            200,
            json={
                "model": "actual-version",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            },
        )

    client = GroqModelClient(SecretStr("fake"), httpx.MockTransport(handle))
    result = matcher(settings(), client).match(
        tender({"title": "Network routers"}), CampaignCriteria(name="C", description="Routers")
    )
    assert result.result == "match" and len(sent) == 2
    assert result.evidence[-1].quote == "Network routers"
    call = result.model_calls[0]
    code = {
        "schema": "provider_output_invalid",
        "json": "malformed_json",
        "citation": "invalid_evidence",
    }[failure]
    assert call.attempts == 2 and call.attempt_errors == (code,) and call.error_code is None
    assert call.input_tokens == (10 if failure == "schema" else 20)
    messages = cast(list[dict[str, str]], sent[1]["messages"])
    assert "Previous generation failed" in messages[0]["content"]
    assert "Invented routers" not in messages[1]["content"]


@pytest.mark.parametrize(
    "content,valid",
    [
        ('```json\n{"score":0.9}\n```', True),
        ('```\n{"score":0.9}\n```', True),
        ('Here is JSON: {"score":0.9}', False),
        ('```json\n{"score":0.9,"score":0.1}\n```', False),
        ('```json\n{"score":NaN}\n```', False),
    ],
)
def test_groq_parsing_strips_only_whole_json_fences(content: str, valid: bool) -> None:
    client = GroqModelClient(SecretStr("fake"))
    if valid:
        assert client.parse_output(content) == {"score": 0.9}
    else:
        with pytest.raises(ValueError):
            client.parse_output(content)


def test_citation_whitespace_is_canonicalized_to_original_source() -> None:
    item = tender({"title": "Network\n  routers"})
    output = judgment().output
    result = matcher(
        settings(), ScriptedModelClient((ModelResponse(output=output, model_version="fake"),))
    ).match(item, CampaignCriteria(name="C", description="Routers"))
    assert result.result == "match" and result.evidence[-1].quote == "Network\n  routers"
    assert result.evidence[-1].quote in (item.title or "")


@pytest.mark.parametrize("quote", ["Network switches", "Network-routers", "Сетевые роутеры"])
def test_citation_formatting_tolerance_never_repairs_facts(quote: str) -> None:
    result = matcher(
        settings(), ScriptedModelClient((judgment(evidence=[{"field": "title", "quote": quote}]),))
    ).match(tender({"title": "Network routers"}), CampaignCriteria(name="C", description="Routers"))
    assert result.result == "unsure" and result.model_calls[0].error_code == "invalid_evidence"


def test_genuine_ambiguity_escalates_with_original_evidence_and_unresolved_issue() -> None:
    client = ScriptedModelClient(
        (
            judgment(
                result="unsure",
                score=0.6,
                scope_relation="unknown",
                support="unknown",
                uncertainties=["Category wording is ambiguous"],
            ),
            judgment(),
        )
    )
    config = settings(
        routing={
            "escalation_enabled": True,
            "stronger_model": {"provider": "fake", "model": "strong"},
        }
    )
    result = matcher(config, client).match(
        tender({"title": "Network routers"}), CampaignCriteria(name="C", description="Routers")
    )
    assert result.result == "match" and result.decision_stage == "escalation"
    first, second = client.requests
    assert first.payload["tender"] == second.payload["tender"]
    assert "Category wording is ambiguous" in cast(list[str], second.payload["unresolved_issues"])
    assert first.timeout_seconds <= config.timeout_seconds * 0.9 * 0.6


def test_generic_title_can_be_refined_without_weakening_conflict_guard() -> None:
    response = judgment(
        result="no_match",
        score=0.02,
        scope_relation="unrelated",
        support="none",
        evidence=[{"field": "description", "quote": "Operating tables"}],
        field_assessments=[
            {
                "field": "title",
                "quote": "Hospital equipment",
                "relation": "unknown",
                "detail": "Generic umbrella title",
            },
            {
                "field": "description",
                "quote": "Operating tables",
                "relation": "unrelated",
                "detail": "Tables do not request MRI scanners",
            },
        ],
    )
    for mode in ("broad", "balanced", "strict"):
        result = matcher(settings(), ScriptedModelClient((response,))).match(
            tender({"title": "Hospital equipment", "description": "Operating tables"}),
            CampaignCriteria.model_validate(
                {"name": "MRI", "description": "MRI scanners only", "matching_mode": mode}
            ),
        )
        assert result.result == "no_match"


def test_reliability_metrics_include_recovered_attempts_and_separate_abstention() -> None:
    from tender_system.core.domain import MatchResult, ModelCall
    from tender_system.matching.matcher_evaluation import metrics

    call = ModelCall(
        stage="semantic",
        provider="groq",
        model="qwen",
        attempts=2,
        attempt_errors=("rate_limited",),
    )
    matched = MatchResult(
        result="match", score=1, reasons=(), uncertainties=(), model_calls=(call,)
    )
    technical = matched.model_copy(
        update={
            "result": "unsure",
            "model_calls": (call.model_copy(update={"error_code": "rate_limited"}),),
        }
    )
    genuine = matched.model_copy(update={"result": "unsure", "model_calls": ()})
    report = metrics(
        [
            ("a", "match", matched),
            ("b", "match", technical),
            ("c", "unsure", genuine),
            ("d", "match", genuine),
        ]
    )
    assert report["model_calls_per_100_cases"] == 100
    assert report["technical_failure_unsure_rate"] == 0.25
    assert report["genuine_unsure_rate"] == 0.25
    assert report["nontechnical_unsure_rate"] == 0.5
    assert report["useful_matches_lost_to_abstention"] == 2
    assert report["provider_failures"] == {"rate_limited": 2}


@pytest.mark.parametrize(
    "code,category",
    [
        ("rate_limited", "http_429"),
        ("invalid_evidence", "citation_evidence_rejection"),
        ("provider_output_invalid", "malformed_schema_output"),
        ("invalid_judgment", "malformed_schema_output"),
        ("timeout", "other_provider_failure"),
    ],
)
def test_unsure_failure_categories_use_recorded_errors(code: str, category: str) -> None:
    from tender_system.core.domain import MatchResult, ModelCall
    from tender_system.matching.matcher_evaluation import unsure_category

    result = MatchResult(
        result="unsure",
        score=0.5,
        reasons=(),
        uncertainties=(),
        model_calls=(ModelCall(stage="semantic", provider="fake", model="fake", error_code=code),),
    )
    assert unsure_category(result) == category


def test_valid_escalated_abstention_is_not_a_technical_failure() -> None:
    from tender_system.core.domain import MatchResult, ModelCall, Reason
    from tender_system.matching.matcher_evaluation import metrics, unsure_category

    result = MatchResult(
        result="unsure",
        score=0.5,
        reasons=(),
        uncertainties=(Reason(code="critical_missing_information", detail="Certification absent"),),
        model_calls=(
            ModelCall(stage="semantic", provider="fake", model="normal", error_code="timeout"),
            ModelCall(stage="escalation", provider="fake", model="strong"),
        ),
    )
    assert unsure_category(result) == "insufficient_tender_data"
    report = metrics([("case", "unsure", result)])
    assert report["technical_failure_unsure_rate"] == 0
    assert report["genuine_unsure_rate"] == 1
    assert report["provider_failures"] == {"timeout": 1}


def test_groq_does_not_dispatch_after_deadline_during_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    time = [0.0]
    sent: list[httpx.Request] = []
    monkeypatch.setattr("tender_system.matching.model_providers.monotonic", lambda: time[0])

    def stalled_sleep(seconds: float) -> None:
        time[0] += seconds + 2  # Scheduling stalls beyond the remaining request window.

    monkeypatch.setattr("tender_system.matching.model_providers.sleep", stalled_sleep)

    def handle(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(429)

    with pytest.raises(ModelFailure, match="timeout") as failure:
        GroqModelClient(SecretStr("fake"), httpx.MockTransport(handle)).generate(request())
    assert len(sent) == 1
    assert failure.value.metadata is not None and failure.value.metadata.attempts == 1
    assert failure.value.metadata.attempt_errors == ("rate_limited",)


def test_one_stage_budget_does_not_reserve_unusable_escalation_time() -> None:
    client = ScriptedModelClient((judgment(),))
    config = settings(
        routing={
            "escalation_enabled": True,
            "max_model_calls": 1,
            "stronger_model": {"provider": "fake", "model": "strong"},
        }
    )
    result = matcher(config, client).match(
        tender({"title": "Network routers"}), CampaignCriteria(name="C", description="Routers")
    )
    assert result.result == "match" and len(client.requests) == 1
    assert client.requests[0].timeout_seconds > config.timeout_seconds * 0.8


def test_live_benchmark_does_not_duplicate_exhausted_provider_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tender_system.matching.matcher_evaluation import Benchmark, evaluate

    benchmark = Benchmark.model_validate_json(
        Path("benchmarks/matcher/matcher_real_v2.json").read_text()
    )
    benchmark = benchmark.model_copy(update={"cases": benchmark.cases[:1]})
    client = ScriptedModelClient(
        tuple(
            ModelFailure("rate_limited", http_status=429, retry_after_seconds=86400)
            for _ in range(3)
        )
    )
    monkeypatch.setattr("tender_system.matching.matcher_evaluation.model_client", lambda _: client)

    def no_outer_backoff(seconds: float) -> None:
        raise AssertionError("Benchmark repeated exhausted provider cooldown")

    monkeypatch.setattr("tender_system.matching.matcher_evaluation.sleep", no_outer_backoff)
    report = evaluate(benchmark, settings(), request_interval=0)
    assert len(client.requests) == 3
    aggregate = cast(dict[str, object], report["aggregate"])
    assert aggregate["technical_failure_unsure_rate"] == 1
    assert aggregate["provider_failures"] == {"rate_limited": 3}
