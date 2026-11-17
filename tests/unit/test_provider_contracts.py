"""Provider replacements share response/failure contracts; all HTTP is intercepted."""

import json

import httpx
import pytest
from pydantic import SecretStr

from tender_system.core.ports import ModelFailure, ModelRequest
from tender_system.matching.model_providers import (
    ChatCompletionsModelClient,
    GemmaModelClient,
    GroqModelClient,
    OpenRouterModelClient,
)


@pytest.mark.parametrize(
    "implementation", [GroqModelClient, OpenRouterModelClient, GemmaModelClient]
)
@pytest.mark.parametrize(
    "scenario", ["success", "rate_limit", "oversized", "timeout", "malformed", "truncated"]
)
def test_provider_contract(implementation: type[ChatCompletionsModelClient], scenario: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if scenario == "timeout":
            raise httpx.ReadTimeout("private response details", request=request)
        if scenario == "rate_limit":
            return httpx.Response(
                429, headers={"Retry-After": "7"}, text="private response details"
            )
        if scenario == "oversized":
            return httpx.Response(413, text="private response details")
        if scenario == "malformed":
            return httpx.Response(200, json={"choices": "private response details"})
        return httpx.Response(
            200,
            json={
                "model": "resolved-fixture",
                "system_fingerprint": "fixture-fingerprint",
                "choices": [
                    {
                        "finish_reason": "length" if scenario == "truncated" else "stop",
                        "message": {"content": json.dumps({"result": "unsure"})},
                    }
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 7},
            },
        )

    provider = implementation(SecretStr("fixture-key"), httpx.MockTransport(handle))
    request = ModelRequest(model="fixture-model", task="matching", payload={}, max_attempts=1)
    if scenario == "success":
        response = provider.generate(request)
        assert response.output == {"result": "unsure"}
        assert response.model_version == "resolved-fixture/fixture-fingerprint"
        assert response.input_tokens == 42 and response.output_tokens == 7
        assert response.attempts == 1
    else:
        with pytest.raises(ModelFailure) as caught:
            provider.generate(request)
        assert (
            caught.value.code
            == {
                "rate_limit": "rate_limited",
                "oversized": "request_too_large",
                "timeout": "timeout",
                "malformed": "malformed_response",
                "truncated": "truncated_output",
            }[scenario]
        )
        assert "private" not in str(caught.value) and "fixture-key" not in str(caught.value)
        if scenario == "rate_limit":
            assert caught.value.http_status == 429 and caught.value.retry_after_seconds == 7
    assert len(requests) == 1  # budget applies equally to transient and malformed replies


@pytest.mark.parametrize(
    "implementation", [GroqModelClient, OpenRouterModelClient, GemmaModelClient]
)
def test_json_action_arrays_require_explicit_opt_in(
    implementation: type[ChatCompletionsModelClient],
) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '[{"kind":"get","url":"https://example.org/api"}]'},
                    }
                ],
            },
        )
    )
    provider = implementation(SecretStr("fixture-key"), transport)
    with pytest.raises(ModelFailure, match="invalid_judgment"):
        provider.generate(
            ModelRequest(model="fixture", task="matching", payload={}, max_attempts=1)
        )
    response = provider.generate(
        ModelRequest(
            model="fixture",
            task="recipe_generation",
            payload={},
            max_attempts=1,
            array_output_key="actions",
        )
    )
    assert response.output == {"actions": [{"kind": "get", "url": "https://example.org/api"}]}


def test_gemma_uses_configured_key_alias_and_exact_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tender_system.core.configuration import ModelSettings
    from tender_system.matching.model_providers import model_client

    monkeypatch.setenv("GEMMA_API_KEY", "fixture-gemma")
    monkeypatch.setenv("GROQ_API_KEY", "unrelated")
    client = model_client(ModelSettings(provider="gemma", model="openai/gemma4-e4b"))
    assert isinstance(client, GemmaModelClient)
    requests: list[httpx.Request] = []

    def reject(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer fixture-gemma"
        assert str(request.url) == "https://llm.buildx.uz/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "gemma4-e4b"
        assert "reasoning_effort" not in body
        return httpx.Response(401, json={"error": {"type": "auth_error"}})

    client._transport = httpx.MockTransport(reject)
    with pytest.raises(ModelFailure, match="authentication_error") as caught:
        client.generate(ModelRequest(model="openai/gemma4-e4b", task="test", payload={}))
    assert caught.value.http_status == 401
    assert len(requests) == 1


@pytest.mark.parametrize("implementation", [GroqModelClient, GemmaModelClient])
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (408, {}, "timeout"),
        (400, {"error": {"code": "context_length_exceeded"}}, "request_too_large"),
    ],
)
def test_provider_context_and_http_timeout_classification(
    implementation: type[ChatCompletionsModelClient],
    status: int,
    body: dict[str, object],
    expected: str,
) -> None:
    client = implementation(
        SecretStr("fixture"), httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    )
    with pytest.raises(ModelFailure, match=expected):
        client.generate(ModelRequest(model="fixture", task="match", payload={}, max_attempts=1))


def test_gemma_selection_reaches_existing_web_matching_configuration() -> None:
    from uuid import uuid4

    from tender_system.core.configuration import load_configuration
    from tender_system.operator.web_matching import bounded_config

    config = load_configuration(
        environ={
            "QUARRY_AI_SEMANTIC_ENABLED": "true",
            "QUARRY_MATCHER__PROVIDER": "gemma",
            "QUARRY_MATCHER__MODEL": "gemma4-e4b",
            "QUARRY_MATCHER__MAX_INPUT_BYTES": "27000",
        },
        validate_models=False,
    )
    campaign = uuid4()
    selected = bounded_config(config, campaign, True).for_campaign(str(campaign))
    assert selected.model.provider == "gemma"
    assert selected.model.credential_env == "GEMMA_API_KEY"
    assert selected.model.context_window_tokens == 32768
    assert selected.model.max_input_bytes == 27000
