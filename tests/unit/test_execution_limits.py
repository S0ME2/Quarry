"""Budgets fail closed; a partial record set never implies inventory completeness."""

from uuid import uuid4

import pytest

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.recipes import RecipeExecutor
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    CollectionRun,
    ExecutionContext,
    ExecutionLimits,
    SourceFailure,
    SourceRecipe,
)
from tender_system.core.execution import (
    ExecutionDeadlineExceeded,
    current_resource_limits,
    execution_budget,
    remaining_seconds,
)
from tender_system.core.ports import HttpResponse
from tender_system.testing.fakes import ScriptedHttpAdapter


def recipe() -> SourceRecipe:
    return SourceRecipe.model_validate(
        {
            "source_id": "fixture",
            "version": "1",
            "expected_count": 10,
            "steps": [
                {"type": "http_request", "parameters": {"url": "https://fixture.test/list"}},
                {"type": "http_request", "parameters": {"url": "https://fixture.test/detail"}},
                {
                    "type": "transform",
                    "parameters": {
                        "name": "identity",
                        "input": [
                            {"external_tender_id": str(i), "title": f"Supply {i}"}
                            for i in range(10)
                        ],
                        "emit": True,
                    },
                },
            ],
        }
    )


def test_request_budget_stops_before_extra_network_work_and_retains_evidence() -> None:
    http = ScriptedHttpAdapter((HttpResponse("https://fixture.test/list", 200, b"ok"),))
    evidence = MemoryEvidenceStore()
    result = RecipeExecutor(http, evidence, SourceSettings()).execute(
        recipe(),
        ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_tenders=1, max_requests=1)),
    )
    assert isinstance(result, SourceFailure)
    assert len(http.requests) == 1 and "request budget exceeded" in result.error_detail
    assert result.failed_step == "1:http_request"
    assert evidence.get(result.failed_evidence_reference)["http_exchanges"]


def test_duration_budget_rejects_late_response(monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = [0.0]
    monkeypatch.setattr("tender_system.collection.recipes.time.monotonic", lambda: elapsed[0])

    class LateResponse:
        def request(self, request: object) -> HttpResponse:
            elapsed[0] = 2
            return HttpResponse("https://fixture.test/list", 200, b"ok")

    result = RecipeExecutor(LateResponse(), MemoryEvidenceStore(), SourceSettings()).execute(
        recipe(), ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_duration_seconds=1))
    )
    assert isinstance(result, SourceFailure)
    assert result.error_code == "operation_timeout"
    assert "recipe_execution" in result.error_detail


def test_record_budget_uses_sampled_contract_without_mutating_recipe() -> None:
    original = recipe()
    digest = original.model_dump_json()
    http = ScriptedHttpAdapter((HttpResponse("https://fixture.test/list", 200, b"ok"),) * 2)
    result = RecipeExecutor(http, MemoryEvidenceStore(), SourceSettings()).execute(
        original, ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_tenders=3))
    )
    assert isinstance(result, CollectionRun)
    assert result.coverage == "sampled" and not result.complete and result.checkpoint is None
    assert len(result.raw_observations) == 3 and result.expected_count == 10
    assert original.model_dump_json() == digest


def test_nested_budgets_preserve_outer_deadline_and_restore_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = [0.0]
    monkeypatch.setattr("tender_system.core.execution.time.monotonic", lambda: elapsed[0])
    with execution_budget(1):
        with execution_budget(100):
            elapsed[0] = 2
            with pytest.raises(ExecutionDeadlineExceeded, match="independent_verification"):
                remaining_seconds("independent_verification")
    assert remaining_seconds("next_operation") is None


def test_nested_browser_resource_limits_cannot_widen_or_leak_between_operations() -> None:
    before = current_resource_limits()
    with execution_budget(5, limits=ExecutionLimits(max_requests=3, max_response_bytes=100)):
        with execution_budget(5, limits=ExecutionLimits(max_requests=50, max_response_bytes=500)):
            assert current_resource_limits().max_requests == 3
            assert current_resource_limits().max_response_bytes == 100
        assert current_resource_limits().max_requests == 3
    assert current_resource_limits() == before
