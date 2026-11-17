from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tender_system.core.configuration import Configuration, ModelSettings, load_configuration
from tender_system.core.domain import RepairActivationMode


def test_defaults_and_unknown_scope() -> None:
    config = Configuration()
    assert config.for_source("unknown") == config.global_settings.source
    assert config.for_campaign("unknown") == config.global_settings.campaign


def test_field_requirements_and_alternative_content_groups_are_source_configurable() -> None:
    config = Configuration.model_validate(
        {
            "global": {
                "source": {
                    "health": {
                        "required_fields": ["title"],
                        "required_any_of": [["title", "description"]],
                    }
                }
            },
            "sources": {
                "optional": {"health": {"required_fields": ["buyer"]}},
                "different": {"health": {"required_any_of": [["description"]]}},
            },
        }
    )
    for value in (
        config,
        Configuration.model_validate(config.model_dump(by_alias=True, exclude_unset=True)),
    ):
        assert value.for_source("required").health.required_fields == ("title",)
        assert value.for_source("optional").health.required_fields == ("buyer",)
        assert value.for_source("optional").health.required_any_of == (("title", "description"),)
        assert value.for_source("different").health.required_any_of == (("description",),)


@pytest.mark.parametrize("groups", [[[]], [["unrecognized"]]])
def test_invalid_required_alternative_groups_fail_at_configuration_load(
    groups: list[list[str]],
) -> None:
    with pytest.raises(ValidationError):
        Configuration.model_validate(
            {"sources": {"s": {"health": {"required_any_of": groups}}}}
        ).for_source("s")


def test_unused_field_requirement_extension_preserves_legacy_run_fingerprint() -> None:
    import hashlib
    import json

    from tender_system.collection.source_runs import request_digest
    from tender_system.core.domain import SourceRecipe

    root = Path(__file__).resolve().parents[2]
    settings = Configuration().for_source("generic-example")
    recipe = SourceRecipe.model_validate_json((root / "recipes" / "api.example.json").read_text())
    old_settings = settings.model_dump(mode="json")
    assert "required_any_of" not in old_settings["health"]
    legacy_digest = hashlib.sha256(
        json.dumps([recipe.model_dump(mode="json"), old_settings], sort_keys=True).encode()
    ).hexdigest()
    assert request_digest(recipe, settings) == legacy_digest
    configured = settings.model_copy(
        update={
            "health": settings.health.model_copy(
                update={"required_any_of": (("title", "description"),)}
            )
        }
    )
    assert request_digest(recipe, configured) != legacy_digest


def test_field_policy_overrides_merge_and_explicit_policy_wins() -> None:
    config = Configuration.model_validate(
        {
            "global": {"source": {"field_change_policy": {"buyer": "REMATCH", "title": "TRACK"}}},
            "sources": {"s": {"field_change_policy": {"buyer": "VOLATILE", "time_left": "IGNORE"}}},
        }
    )
    settings = config.for_source("s")
    assert settings.field_change_policy == {
        "buyer": "VOLATILE",
        "title": "TRACK",
        "time_left": "IGNORE",
    }
    assert "buyer" not in settings.effective_rematch_fields
    assert "title" not in settings.effective_rematch_fields
    assert "deadline" in settings.effective_rematch_fields
    assert "buyer" in config.for_source("other").effective_rematch_fields


@pytest.mark.parametrize("policy", ["TRACK", "REMATCH"])
@pytest.mark.parametrize("field", ["time_left", "source_id", "raw_data"])
def test_raw_or_identity_fields_cannot_be_tracked(field: str, policy: str) -> None:
    with pytest.raises(ValidationError, match="normalized business fields"):
        Configuration.model_validate({"sources": {"s": {"field_change_policy": {field: policy}}}})


def test_campaign_validity_precedence_and_round_trip_preserve_omitted_and_null() -> None:
    config = Configuration.model_validate(
        {
            "global": {
                "source": {"validity": {"max_age_days": 30}},
                "campaign": {"validity": {"exclude_expired": False}},
            },
            "sources": {"s": {"validity": {"max_age_days": 10, "excluded_statuses": ["blocked"]}}},
            "campaigns": {"c": {"validity": {"max_age_days": None, "excluded_statuses": []}}},
        }
    )
    for value in (
        config,
        Configuration.model_validate(config.model_dump(by_alias=True, exclude_unset=True)),
    ):
        settings = value.for_matching_source("s", "c")
        assert not settings.validity.exclude_expired
        assert settings.validity.max_age_days is None and settings.validity.excluded_statuses == ()
        assert value.for_matching_source("s", "other").validity.max_age_days == 10
        assert value.for_source("s").validity.exclude_expired
        assert value.for_source("s").validity.excluded_statuses == ("blocked",)


def test_invalid_campaign_validity_fails_at_configuration_load() -> None:
    with pytest.raises(ValidationError):
        Configuration.model_validate({"campaigns": {"c": {"validity": {"exclude_expired": None}}}})


def test_nested_overrides_inherit_without_cross_scope_leakage() -> None:
    config = Configuration.model_validate(
        {
            "global": {
                "source": {"timeout_seconds": 45, "retry": {"max_attempts": 5}},
                "campaign": {"batch_size": 25, "model": {"provider": "local", "model": "small"}},
            },
            "sources": {
                "one": {"timeout_seconds": 90, "retry": {"backoff_seconds": 4}},
                "two": {"batch_size": 7},
            },
            "campaigns": {"one": {"model": {"model": "large"}, "batch_size": 10}},
        }
    )
    one = config.for_source("one")
    assert one.timeout_seconds == 90
    assert one.retry.max_attempts == 5
    assert one.retry.backoff_seconds == 4
    assert config.for_source("two").timeout_seconds == 45
    assert config.for_source("two").retry.backoff_seconds == 1
    assert config.global_settings.source.timeout_seconds == 45
    campaign = config.for_campaign("one")
    assert campaign.batch_size == 10
    assert campaign.model.provider == "local"
    assert campaign.model.model == "large"
    assert config.for_campaign("other").model.model == "small"
    assert config.for_source("one").batch_size == 100


def test_false_zero_empty_and_null_overrides_are_preserved() -> None:
    config = Configuration.model_validate(
        {
            "global": {
                "source": {"browser_fallback_enabled": True, "validity": {"max_age_days": 30}}
            },
            "sources": {
                "one": {
                    "browser_fallback_enabled": False,
                    "budget_limit": 0,
                    "rematch_fields": [],
                    "validity": {"max_age_days": None},
                }
            },
        }
    )
    source = config.for_source("one")
    assert source.browser_fallback_enabled is False
    assert source.budget_limit == 0
    assert source.rematch_fields == ()
    assert source.validity.max_age_days is None
    assert config.global_settings.source.validity.max_age_days == 30


@pytest.mark.parametrize("mode", list(RepairActivationMode))
@pytest.mark.parametrize("requires_approval", [True, False])
def test_all_repair_modes_allow_configurable_custom_code_policy(
    mode: RepairActivationMode, requires_approval: bool
) -> None:
    config = Configuration.model_validate(
        {
            "sources": {
                "one": {
                    "repair": {
                        "mode": mode,
                        "generated_code_requires_approval": requires_approval,
                        "limited_allowed_changes": ["custom_code"],
                    }
                }
            }
        }
    )
    repair = config.for_source("one").repair
    assert repair.mode == mode
    assert repair.generated_code_requires_approval is requires_approval
    assert repair.limited_allowed_changes == ("custom_code",)


@pytest.mark.parametrize(
    "invalid",
    [
        {"unknown": True},
        {"schema_version": 2},
        {"sources": {"x": {"matcher_adapter": "foo"}}},
        {"campaigns": {"x": {"browser_fallback_enabled": True}}},
        {"sources": {"x": {"timeout_seconds": -1}}},
        {"sources": {"x": {"timeout_seconds": None}}},
        {"sources": {"x": {"timezone": "No/SuchTimezone"}}},
        {"sources": {"x": {"retry": {"unknown": 1}}}},
        {"sources": {"x": {"fixer_model": {"api_key": "not-accepted"}}}},
        {"sources": {"x": {"repair": {"minimum_confidence": 2}}}},
        {"sources": {"x": {"validity": {"missing_successful_runs": 1}}}},
        {"sources": {"x": {"rematch_fields": ["raw_data"]}}},
        {"sources": {"x": {"rematch_fields": ["title", "title"]}}},
        {"sources": {"": {}}},
        {"campaigns": {"x": {"match_threshold": 0.2}}},
        {"campaigns": {"x": {"retry": {"max_attempts": 0}}}},
        {"campaigns": {"x": {"budget_limit": float("nan")}}},
    ],
)
def test_invalid_overrides_fail_when_loading(invalid: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Configuration.model_validate(invalid)


def test_toml_example_loads() -> None:
    config = load_configuration(
        Path(__file__).resolve().parents[2] / "config.example.toml",
        environ={},
    )
    assert config.for_source("example").timezone == "Asia/Tashkent"
    assert config.for_campaign("example").matching_mode == "strict"


def test_invalid_toml_does_not_silently_default(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[sources.x]\ntimeout_seconds = "bad"\n')
    with pytest.raises(ValidationError):
        load_configuration(path)


def test_secrets_remain_external_and_redacted() -> None:
    settings = ModelSettings(api_key_env="TEST_MODEL_KEY")
    key = settings.resolve_api_key({"TEST_MODEL_KEY": "test-secret-value"})
    assert key is not None
    assert key.get_secret_value() == "test-secret-value"
    assert "test-secret-value" not in repr(key)
    assert "test-secret-value" not in settings.model_dump_json()
    with pytest.raises(ValueError, match="missing secret environment variable"):
        settings.resolve_api_key({})
    assert ModelSettings().resolve_api_key({}) is None


@given(st.integers(min_value=1, max_value=10000), st.integers(min_value=1, max_value=10000))
def test_overrides_do_not_mutate_defaults(default: int, override: int) -> None:
    config = Configuration.model_validate(
        {"global": {"source": {"batch_size": default}}, "sources": {"x": {"batch_size": override}}}
    )
    assert config.for_source("x").batch_size == override
    assert config.for_source("other").batch_size == default
    assert config.global_settings.source.batch_size == default


def test_resolved_settings_cannot_be_reassigned() -> None:
    settings = Configuration().for_source("x")
    with pytest.raises(ValidationError, match="frozen"):
        settings.timeout_seconds = 1  # type: ignore[misc]
