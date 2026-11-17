"""Central dotenv and typed startup configuration; no external services."""

from pathlib import Path

import pytest

from tender_system.core.configuration import load_configuration
from tender_system.core.environment import load_environment, startup_settings
from tender_system.persistence.storage import validate_test_database


def test_dotenv_is_data_shell_wins_and_empty_values_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "QUARRY_TEST_LITERAL='$(false)'\nQUARRY_TEST_EMPTY=\nQUARRY_TEST_OVERRIDE=file\n"
    )
    monkeypatch.setenv("QUARRY_TEST_OVERRIDE", "shell")
    monkeypatch.delenv("QUARRY_TEST_LITERAL", raising=False)
    monkeypatch.delenv("QUARRY_TEST_EMPTY", raising=False)
    values = load_environment(path)
    assert values["QUARRY_TEST_LITERAL"] == "$(false)"
    assert values["QUARRY_TEST_OVERRIDE"] == "shell"
    assert values["QUARRY_TEST_EMPTY"] == ""
    monkeypatch.delenv("QUARRY_TEST_LITERAL")
    monkeypatch.delenv("QUARRY_TEST_EMPTY")


def test_safe_defaults_need_no_credentials() -> None:
    config = load_configuration(environ={})
    assert config.runtime.manual_only
    assert not config.runtime.operator_worker_enabled
    assert config.runtime.matching_enabled
    assert config.for_campaign("x").matcher_adapter == "rules"
    assert config.for_source("x").repair.mode == "human"


def test_all_model_slots_and_nested_settings_resolve() -> None:
    env = {
        "QUARRY_SOURCE__RETRY__MAX_ATTEMPTS": "2",
        "QUARRY_SOURCE__INVESTIGATION__MAX_ACTIONS": "5",
        "QUARRY_SOURCE__HEALTH__REQUIRED_FIELDS": '["title", "buyer"]',
        "QUARRY_CAMPAIGN__BATCH_SIZE": "2",
    }
    for slot in ("MATCHER", "STRONGER_MATCHER"):
        env.update(
            {
                f"QUARRY_{slot}__PROVIDER": "groq",
                f"QUARRY_{slot}__MODEL": slot,
                f"QUARRY_{slot}__MAX_OUTPUT_TOKENS": "123",
                f"QUARRY_{slot}__MAX_ATTEMPTS": "1",
                f"QUARRY_{slot}__REASONING_EFFORT": "none",
                f"QUARRY_{slot}__REQUEST_INTERVAL_SECONDS": "5",
            }
        )
    config = load_configuration(environ=env)
    source, campaign = config.for_source("x"), config.for_campaign("x")
    assert source.retry.max_attempts == 2
    assert source.investigation.max_actions == 5
    assert source.health.required_fields == ("title", "buyer")
    assert campaign.batch_size == 2
    for model in (
        campaign.model,
        campaign.routing.stronger_model,
    ):
        assert model is not None and model.provider == "groq"
        assert model.max_output_tokens == 123 and model.max_attempts == 1
        assert model.reasoning_effort == "none" and model.request_interval_seconds == 5


def test_invalid_configuration_does_not_echo_input() -> None:
    private = "private-value-that-must-never-appear"
    with pytest.raises(ValueError) as caught:
        load_configuration(environ={"QUARRY_SOURCE__TIMEOUT_SECONDS": private})
    assert "timeout_seconds" in str(caught.value)
    assert private not in str(caught.value)
    with pytest.raises(ValueError) as caught:
        startup_settings({"QUARRY_API_PORT": private})
    assert private not in str(caught.value)


def test_retired_ai_flags_and_model_slots_are_inert() -> None:
    config = load_configuration(
        environ={
            "QUARRY_AI_REPAIR_ENABLED": "true",
            "QUARRY_AUTOMATIC_REPAIR_ENABLED": "true",
            "QUARRY_AI_INVESTIGATION_ENABLED": "true",
            "QUARRY_FIXER__PROVIDER": "groq",
        }
    )
    assert config.global_settings.source.fixer_model.provider == "unconfigured"
    assert not config.runtime.ai_semantic_enabled


@pytest.mark.parametrize(
    "test,dev",
    [
        ("postgresql:///quarry_clean_dev", "postgresql:///quarry_clean_dev"),
        ("postgresql:///quarry_clean_test", "postgresql:///quarry_clean_test"),
        ("sqlite:///quarry_test", None),
        ("private-malformed-url", None),
    ],
)
def test_test_database_guard_rejects_unsafe_targets(test: str, dev: str | None) -> None:
    with pytest.raises(ValueError) as caught:
        validate_test_database(test, dev)
    assert "private-malformed-url" not in str(caught.value)


def test_toml_compatibility_and_environment_precedence(tmp_path: Path) -> None:
    path = tmp_path / "legacy.toml"
    path.write_text("[global.source]\ntimeout_seconds=10\n[sources.special]\ntimeout_seconds=40\n")
    config = load_configuration(path, environ={"QUARRY_SOURCE__TIMEOUT_SECONDS": "20"})
    assert config.for_source("unknown").timeout_seconds == 20
    assert config.for_source("special").timeout_seconds == 40


def test_configured_provider_limits_reasoning_and_retries_without_network() -> None:
    import json

    import httpx
    from pydantic import SecretStr

    from tender_system.core.configuration import ModelSettings
    from tender_system.core.ports import ModelRequest
    from tender_system.matching.model_providers import OpenRouterModelClient

    calls: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        )

    client = OpenRouterModelClient(SecretStr("offline-fixture"), httpx.MockTransport(handle))
    client.settings = ModelSettings(
        provider="openrouter",
        model="fixture",
        max_attempts=2,
        max_output_tokens=99,
        reasoning_effort="low",
    )
    response = client.generate(ModelRequest("fixture", "test", {}, max_attempts=5))
    assert response.attempts == 2 and len(calls) == 2
    assert all(body["max_tokens"] == 99 and body["reasoning_effort"] == "low" for body in calls)


@pytest.mark.parametrize(
    "provider,key", [("groq", "GROQ_API_KEY"), ("openrouter", "OPENROUTER_API_KEY")]
)
def test_provider_selects_default_credential(provider: str, key: str) -> None:
    from tender_system.core.configuration import ModelSettings

    model = ModelSettings(provider=provider, model="fixture")
    assert model.credential_env == key
    secret = model.resolve_api_key({key: "offline-key"})
    assert secret is not None and secret.get_secret_value() == "offline-key"
    custom = model.model_copy(update={"api_key_env": "CUSTOM_KEY"})
    assert custom.credential_env == "CUSTOM_KEY"
    with pytest.raises(ValueError, match="CUSTOM_KEY"):
        custom.resolve_api_key({key: "offline-key"})
