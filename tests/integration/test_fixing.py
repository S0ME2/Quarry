from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tender_system.core.configuration import Configuration, SourceSettings
from tender_system.core.domain import FixResult, RepairValidation, SourceFailure, SourceRecipe
from tender_system.repair.fixing import GuardedBrowser, activation_decision, change_categories
from tender_system.testing.fakes import FakeBrowserAdapter, FakePage

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def recipe(selector: str = "article", version: str = "1") -> SourceRecipe:
    return SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": version,
            "steps": [
                {
                    "type": "extract",
                    "parameters": {
                        "format": "html",
                        "input": "<article>A</article>",
                        "selector": selector,
                        "fields": {"title": {}},
                        "emit": True,
                    },
                }
            ],
        }
    )


def failure() -> SourceFailure:
    return SourceFailure(
        run_id=uuid4(),
        source_id="s",
        recipe=recipe(),
        error_code="selector_missing",
        error_detail="Source changed",
        failed_evidence_reference="failed",
        started_at=NOW,
        finished_at=NOW,
    )


def proposal() -> FixResult:
    return FixResult(
        status="fixed",
        updated_recipe=recipe("section", "2"),
        explanation="Changed selector",
        confidence=1,
    )


@pytest.mark.parametrize(
    "mode,code,expected",
    [
        ("automatic", False, "activated"),
        ("human", False, "awaiting_approval"),
        ("limited", False, "activated"),
        ("automatic", True, "awaiting_approval"),
    ],
)
def test_activation_is_configurable(mode: str, code: bool, expected: str) -> None:
    proposed = (
        proposal().model_copy(update={"custom_code_reference": "artifact"}) if code else proposal()
    )
    settings = SourceSettings.model_validate({"repair": {"mode": mode}})
    validation = RepairValidation(passed=True, checks=(), confidence=1, evidence_references=())
    assert activation_decision(failure(), proposed, validation, settings, False)[0] == expected


def test_validation_failure_never_activates_even_with_human_approval() -> None:
    validation = RepairValidation(passed=False, checks=(), confidence=1, evidence_references=())
    assert (
        activation_decision(failure(), proposal(), validation, SourceSettings(), True)[0]
        == "rejected"
    )


def test_generated_code_approval_can_be_disabled_by_policy() -> None:
    settings = SourceSettings.model_validate(
        {"repair": {"mode": "automatic", "generated_code_requires_approval": False}}
    )
    validation = RepairValidation(passed=True, checks=(), confidence=1, evidence_references=())
    assert (
        activation_decision(
            failure(),
            proposal().model_copy(update={"custom_code_reference": "artifact"}),
            validation,
            settings,
            False,
        )[0]
        == "activated"
    )


def test_limited_policy_uses_actual_recipe_diff_not_model_claims() -> None:
    assert change_categories(recipe(), proposal().updated_recipe, proposal()) == {"selectors"}  # type: ignore[arg-type]
    changed = proposal().model_copy(
        update={
            "updated_recipe": recipe().model_copy(
                update={"version": "2", "coverage": "incremental"}
            )
        }
    )
    validation = RepairValidation(passed=True, checks=(), confidence=1, evidence_references=())
    assert (
        activation_decision(
            failure(),
            changed,
            validation,
            SourceSettings.model_validate({"repair": {"mode": "limited"}}),
            False,
        )[0]
        == "awaiting_approval"
    )


def test_source_overrides_are_isolated_and_repair_settings_typed() -> None:
    config = Configuration.model_validate(
        {
            "sources": {
                "s": {
                    "investigation": {"max_actions": 2},
                    "repair_checks": {"require_samples": False},
                    "repair": {"mode": "automatic"},
                    "fallback_max_runs": 1,
                }
            }
        }
    )
    assert config.for_source("s").investigation.max_actions == 2
    assert config.for_source("other").investigation.max_actions == 30
    assert config.for_source("other").repair_checks.require_samples
    assert config.for_campaign("s").matcher_adapter == "rules"
    with pytest.raises(ValidationError):
        SourceSettings.model_validate({"fallback_max_runs": 0})


def test_browser_authorization_is_revocable_and_session_scoped() -> None:
    browser = FakeBrowserAdapter({"https://local": FakePage(html="Hello")})
    revoked = Event()
    guarded = GuardedBrowser(browser, failure(), SourceSettings(), revoked)
    guarded.navigate("https://local")
    assert guarded.content() == "Hello"
    with pytest.raises(PermissionError):
        guarded.session_state()
    with pytest.raises(PermissionError):
        guarded.restore_session("someone-else")
    revoked.set()
    with pytest.raises(TimeoutError):
        guarded.click("a")


def test_human_approval_can_override_fixer_confidence_but_not_validation() -> None:
    validation = RepairValidation(passed=True, checks=(), confidence=1, evidence_references=())
    proposed = proposal().model_copy(update={"confidence": 0.5})
    assert (
        activation_decision(failure(), proposed, validation, SourceSettings(), True)[0]
        == "activated"
    )
