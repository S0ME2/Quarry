"""OpenAI-compatible adapters for ModelClient. No matching policy here."""

import json
import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Lock
from time import monotonic, sleep
from typing import cast

import httpx
from pydantic import JsonValue, SecretStr

from tender_system.core.configuration import ModelSettings
from tender_system.core.ports import ModelClient, ModelFailure, ModelRequest, ModelResponse


class ChatCompletionsModelClient:
    endpoint: str
    aliases: dict[str, str] = {}
    settings: ModelSettings | None = None
    _rate_lock = Lock()
    _next_request: dict[tuple[str, str], float] = {}

    def __init__(self, api_key: SecretStr, transport: httpx.BaseTransport | None = None) -> None:
        self._api_key, self._transport = api_key, transport

    def configured_request(self, request: ModelRequest) -> ModelRequest:
        if self.settings is None:
            return request
        return replace(
            request,
            timeout_seconds=min(request.timeout_seconds, self.settings.timeout_seconds),
            max_output_tokens=min(
                request.max_output_tokens,
                self.settings.max_output_tokens or request.max_output_tokens,
            ),
            max_attempts=min(request.max_attempts, self.settings.max_attempts),
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        request = self.configured_request(request)
        if self.settings and self.settings.request_interval_seconds:
            key = (self.endpoint, request.model)
            with self._rate_lock:
                now = monotonic()
                wait = max(0, self._next_request.get(key, now) - now)
                if wait >= request.timeout_seconds:
                    raise ModelFailure("rate_limited")
                self._next_request[key] = now + wait + self.settings.request_interval_seconds
            sleep(wait)
            request = replace(request, timeout_seconds=request.timeout_seconds - wait)
        model = self.aliases.get(request.model, request.model)
        instructions = request.payload.get("instructions", "Return structured JSON only.")
        data = {key: value for key, value in request.payload.items() if key != "instructions"}
        response_format: dict[str, object] = {"type": "json_object"}
        if request.output_schema is not None:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.task,
                    "strict": True,
                    "schema": request.output_schema,
                },
            }
        try:
            with httpx.Client(timeout=request.timeout_seconds, transport=self._transport) as client:
                reply = client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                    json={
                        "model": model,
                        "temperature": 0,
                        "max_tokens": request.max_output_tokens,
                        **self.parameters(model),
                        **(
                            {"reasoning_effort": self.settings.reasoning_effort}
                            if self.settings and self.settings.reasoning_effort is not None
                            else {}
                        ),
                        "response_format": response_format,
                        "messages": [
                            {"role": "system", "content": instructions},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    data, ensure_ascii=False, separators=(",", ":")
                                ),
                            },
                        ],
                    },
                )
        except httpx.TimeoutException:
            raise ModelFailure("timeout", failure_stage="transport") from None
        except httpx.TransportError:
            raise ModelFailure("transport_error", failure_stage="transport") from None
        if not reply.is_success:
            status = reply.status_code
            code = (
                "rate_limited"
                if status == 429
                else "request_too_large"
                if status == 413
                else "timeout"
                if status == 408
                else "temporary_provider_error"
                if status >= 500
                else "authentication_error"
                if status in {401, 403}
                else "provider_rejected"
            )
            if status == 400:
                try:
                    provider_code = reply.json().get("error", {}).get("code")
                    if provider_code in {
                        "json_validate_failed",
                        "invalid_json",
                        "schema_validation_failed",
                    }:
                        code = "provider_output_invalid"
                    elif provider_code in {"context_length_exceeded", "request_too_large"}:
                        code = "request_too_large"
                except (ValueError, AttributeError, TypeError):
                    pass
            raise ModelFailure(
                code,
                http_status=status,
                retry_after_seconds=retry_after(reply.headers.get("retry-after")),
                failure_stage="provider_response",
            )
        try:
            body = reply.json()
        except ValueError:
            raise ModelFailure("malformed_json", failure_stage="response_parsing") from None
        if not isinstance(body, dict) or body.get("error"):
            raise ModelFailure("malformed_response", failure_stage="response_parsing")
        version = body.get("model") or model
        if not isinstance(version, str) or not version.strip():
            raise ModelFailure("malformed_response", failure_stage="response_parsing")
        if isinstance(body.get("system_fingerprint"), str):
            version += "/" + body["system_fingerprint"]
        usage = body.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        metadata = ModelResponse(
            output={},
            model_version=version,
            input_tokens=token_count(usage.get("prompt_tokens")),
            output_tokens=token_count(usage.get("completion_tokens")),
            cost=nonnegative_number(usage.get("cost")),
        )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ModelFailure(
                "malformed_response", metadata=metadata, failure_stage="response_parsing"
            )
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            code = (
                "truncated_output"
                if choice.get("finish_reason") == "length"
                else "incomplete_output"
            )
            raise ModelFailure(code, metadata=metadata, failure_stage="response_parsing")
        message = choice.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ModelFailure(
                "malformed_response", metadata=metadata, failure_stage="response_parsing"
            )
        try:
            output = self.parse_output(message["content"])
        except ValueError:
            raise ModelFailure(
                "malformed_json", metadata=metadata, failure_stage="response_parsing"
            ) from None
        if isinstance(output, list) and request.array_output_key:
            output = {request.array_output_key: output}
        if not isinstance(output, dict):
            raise ModelFailure(
                "invalid_judgment", metadata=metadata, failure_stage="response_parsing"
            )
        return ModelResponse(
            output=cast(Mapping[str, JsonValue], output),
            model_version=version,
            input_tokens=metadata.input_tokens,
            output_tokens=metadata.output_tokens,
            cost=metadata.cost,
        )

    def parameters(self, model: str) -> dict[str, JsonValue]:
        return {}

    def parse_output(self, content: str) -> object:
        return json.loads(content, object_pairs_hook=unique_object, parse_constant=reject_constant)


class OpenRouterModelClient(ChatCompletionsModelClient):
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    aliases = {"space-bunny-alpha": "stealth/space-bunny-alpha"}

    def generate(self, request: ModelRequest) -> ModelResponse:
        request = self.configured_request(request)
        deadline = monotonic() + request.timeout_seconds
        for attempt in range(1, request.max_attempts + 1):
            try:
                return replace(
                    super().generate(
                        replace(request, timeout_seconds=max(0.001, deadline - monotonic()))
                    ),
                    attempts=attempt,
                )
            except ModelFailure as failure:
                wait = max(0.5 * 2 ** (attempt - 1), failure.retry_after_seconds or 0)
                if (
                    failure.code
                    not in {
                        "rate_limited",
                        "temporary_provider_error",
                        "transport_error",
                        "timeout",
                    }
                    or attempt == request.max_attempts
                    or wait >= deadline - monotonic()
                ):
                    raise
                sleep(wait)
        raise ValueError("model max_attempts must be positive")


class RetryingChatCompletionsModelClient(ChatCompletionsModelClient):
    """Shared bounded transport retry and one validated-output correction."""

    def generate(self, request: ModelRequest) -> ModelResponse:
        request = self.configured_request(request)
        deadline = monotonic() + request.timeout_seconds
        errors: list[str] = []
        responses: list[ModelResponse] = []
        repaired = False
        active = request
        if request.max_attempts < 1:
            raise ValueError("model request max_attempts must be positive")
        for attempt in range(1, request.max_attempts + 1):
            remaining = deadline - monotonic()
            if remaining <= 0.001:
                raise ModelFailure(
                    "timeout",
                    metadata=self._metadata(request, responses, max(1, attempt - 1), errors),
                )
            response = None
            try:
                response = super().generate(replace(active, timeout_seconds=remaining))
                if request.validate_output:
                    try:
                        request.validate_output(response)
                    except ModelFailure as failure:
                        if failure.failure_stage is None:
                            failure.failure_stage = "response_validation"
                        if failure.metadata is None:
                            failure.metadata = response
                        raise
                    except ValueError:
                        raise ModelFailure(
                            "invalid_judgment",
                            metadata=response,
                            failure_stage="response_validation",
                        ) from None
            except ModelFailure as failure:
                errors.append(failure.code)
                if response is not None or failure.metadata is not None:
                    responses.append(response or cast(ModelResponse, failure.metadata))
                transient = failure.code in {
                    "rate_limited",
                    "temporary_provider_error",
                    "transport_error",
                    "timeout",
                }
                output_failure = failure.code in {
                    "provider_output_invalid",
                    "malformed_json",
                    "malformed_response",
                    "truncated_output",
                    "incomplete_output",
                    "invalid_judgment",
                    "invalid_evidence",
                    "incomplete_assessment",
                    "insufficient_subject_evidence",
                }
                wait = max(0.5 * 2 ** (attempt - 1), failure.retry_after_seconds or 0)
                if output_failure and not repaired:
                    repaired = True
                    active = replace(
                        request,
                        payload={
                            **request.payload,
                            "instructions": str(request.payload.get("instructions", ""))
                            + "\nPrevious generation failed output validation ("
                            + failure.code
                            + "). Generate a fresh complete JSON object obeying the schema. "
                            "Copy short exact source excerpts for EVERY citation and field "
                            "assessment. Preserve source spelling, script and punctuation. "
                            "Never paraphrase quotes "
                            "or invent missing evidence. Return unsure if evidence cannot support "
                            "a decision.",
                        },
                    )
                    wait = 0
                elif not transient:
                    wait = float("inf")
                if attempt == request.max_attempts or wait + 0.05 >= deadline - monotonic():
                    raise ModelFailure(
                        failure.code,
                        http_status=failure.http_status,
                        retry_after_seconds=failure.retry_after_seconds,
                        metadata=self._metadata(request, responses, attempt, errors),
                        failure_stage=failure.failure_stage,
                        failure_detail=failure.failure_detail,
                    ) from None
                sleep(wait)
            else:
                responses.append(response)
                return replace(
                    self._metadata(request, responses, attempt, errors), output=response.output
                )
        raise AssertionError("bounded provider loop exhausted")

    @staticmethod
    def _metadata(
        request: ModelRequest, responses: list[ModelResponse], attempts: int, errors: list[str]
    ) -> ModelResponse:
        def total(field: str) -> int | float | None:
            values = [getattr(response, field) for response in responses]
            return sum(values) if values and all(v is not None for v in values) else None

        return ModelResponse(
            output={},
            model_version=responses[-1].model_version if responses else request.model,
            input_tokens=cast(int | None, total("input_tokens")),
            output_tokens=cast(int | None, total("output_tokens")),
            cost=cast(float | None, total("cost")),
            attempts=attempts,
            attempt_errors=tuple(errors),
        )

    def parse_output(self, content: str) -> object:
        content = content.strip()
        if content.startswith("```json\n") or content.startswith("```\n"):
            if content.endswith("\n```"):
                content = content[content.index("\n") + 1 : -4].strip()
        return super().parse_output(content)


class GroqModelClient(RetryingChatCompletionsModelClient):
    endpoint = "https://api.groq.com/openai/v1/chat/completions"

    def parameters(self, model: str) -> dict[str, JsonValue]:
        # Instruct mode keeps Qwen's reasoning out of the output/token budget.
        return {"reasoning_effort": "none"} if model == "qwen/qwen3.8-27b" else {}


class GemmaModelClient(RetryingChatCompletionsModelClient):
    endpoint = "https://llm.buildx.uz/v1/chat/completions"
    aliases = {"openai/gemma4-e4b": "gemma4-e4b"}


def model_client(settings: ModelSettings) -> ModelClient:
    providers = {
        "openrouter": OpenRouterModelClient,
        "groq": GroqModelClient,
        "gemma": GemmaModelClient,
    }
    if settings.provider not in providers:
        raise ValueError("model provider is not registered")
    key = settings.resolve_api_key()
    if key is None:
        raise ValueError("model provider needs a credential environment variable")
    client = providers[settings.provider](key)
    client.settings = settings
    return client


def nonnegative_number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    return None


def token_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except ValueError:
        try:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                return None
            return max(0, (instant - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")
