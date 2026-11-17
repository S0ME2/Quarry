"""Nested executors cannot widen explicit enclosing operational budgets or scope."""

from uuid import uuid4

import pytest

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.recipes import RecipeExecutor
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    BoundedCollectionResult,
    CollectionRun,
    ExecutionContext,
    ExecutionLimits,
    RecipeStep,
    SourceFailure,
    SourceRecipe,
)
from tender_system.core.execution import (
    current_resource_limits,
    effective_resource_limits,
    execution_budget,
    remaining_seconds,
)
from tender_system.core.ports import HttpRequest, HttpResponse, StepExecutor
from tender_system.core.registry import Registry
from tender_system.testing.fakes import FunctionHttpAdapter


def two_requests() -> SourceRecipe:
    return SourceRecipe.model_validate(
        {
            "source_id": "nested",
            "version": "1",
            "steps": [
                {"type": "http_request", "parameters": {"url": f"http://fixture.test/{i}"}}
                for i in range(2)
            ],
        }
    )


def records() -> SourceRecipe:
    return SourceRecipe.model_validate(
        {
            "source_id": "nested",
            "version": "1",
            "steps": [
                {
                    "type": "transform",
                    "parameters": {
                        "name": "identity",
                        "input": [{"external_tender_id": str(i)} for i in range(5)],
                        "emit": True,
                    },
                }
            ],
        }
    )


@pytest.mark.parametrize("explicit", [False, True])
def test_enclosing_request_budget_stops_http_before_second_dispatch(explicit: bool) -> None:
    sent: list[str] = []

    def respond(request: HttpRequest) -> HttpResponse:
        sent.append(request.url)
        return HttpResponse(request.url, 200, b"{}")

    with execution_budget(10, limits=ExecutionLimits(max_requests=1)):
        result = RecipeExecutor(
            FunctionHttpAdapter(respond), MemoryEvidenceStore(), SourceSettings()
        ).execute(
            two_requests(),
            ExecutionContext(
                run_id=uuid4(), limits=ExecutionLimits(max_requests=2) if explicit else None
            ),
        )
    assert isinstance(result, SourceFailure) and result.error_code == "request_budget_exceeded"
    assert len(sent) == result.diagnostics.transport_attempts == 1


@pytest.mark.parametrize("per_response,total", [(1, 100), (100, 3)])
def test_enclosing_byte_caps_apply_to_actual_http(per_response: int, total: int) -> None:
    received_limits: list[int] = []

    def respond(request: HttpRequest) -> HttpResponse:
        received_limits.append(request.max_response_bytes)
        return HttpResponse(request.url, 200, b"{}")

    with execution_budget(
        10,
        limits=ExecutionLimits(
            max_response_bytes=per_response,
            max_total_response_bytes=total,
        ),
    ):
        result = RecipeExecutor(
            FunctionHttpAdapter(respond), MemoryEvidenceStore(), SourceSettings()
        ).execute(
            two_requests(),
            ExecutionContext(
                run_id=uuid4(),
                limits=ExecutionLimits(
                    max_response_bytes=1000,
                    max_total_response_bytes=1000,
                ),
            ),
        )
    assert isinstance(result, SourceFailure) and result.error_code == "response_budget_exceeded"
    assert received_limits == ([1] if per_response == 1 else [3, 1])


@pytest.mark.parametrize("explicit,legacy,expected", [(None, None, 2), (4, None, 2), (4, 1, 1)])
def test_inherited_record_scope_returns_sampled_executor_contract(
    explicit: int | None,
    legacy: int | None,
    expected: int,
) -> None:
    executor = RecipeExecutor(
        FunctionHttpAdapter(lambda _: pytest.fail("unexpected HTTP")),
        MemoryEvidenceStore(),
        SourceSettings(),
    )
    with execution_budget(10, limits=ExecutionLimits(max_tenders=2)):
        result = executor.execute(
            records(),
            ExecutionContext(
                run_id=uuid4(),
                limits=ExecutionLimits(max_tenders=explicit) if explicit else None,
            ),
            max_observations=legacy,
        )
    assert isinstance(result, CollectionRun)
    assert len(result.raw_observations) == expected
    assert result.coverage == "sampled" and not result.complete and result.checkpoint is None


def test_legacy_bound_remains_diagnostic_without_explicit_or_inherited_scope() -> None:
    result = RecipeExecutor(
        FunctionHttpAdapter(lambda _: pytest.fail("unexpected HTTP")),
        MemoryEvidenceStore(),
        SourceSettings(),
    ).execute(
        records(),
        ExecutionContext(run_id=uuid4()),
        max_observations=1,
    )
    assert isinstance(result, BoundedCollectionResult) and len(result.observations) == 1


def test_resource_caps_and_scope_reset_without_default_capping_explicit_limits() -> None:
    explicit = ExecutionLimits(max_requests=20000, max_tenders=20000, max_duration_seconds=2000)
    assert effective_resource_limits(explicit) == explicit
    with execution_budget(10):
        assert effective_resource_limits(explicit) == explicit  # Deadline-only parent.
    with execution_budget(10, limits=ExecutionLimits(max_tenders=2, max_duration_seconds=1)):
        inherited = effective_resource_limits(explicit)
        assert inherited.max_tenders == 2 and inherited.max_duration_seconds == 1
        assert inherited.max_requests == 10000
        with execution_budget(10, limits=explicit):
            assert current_resource_limits() == inherited
            remaining = remaining_seconds("nested-test")
            assert remaining is not None and remaining <= 1
    assert effective_resource_limits(explicit) == explicit


def test_enclosing_duration_caps_executor_with_wider_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instant = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: instant[0])

    def respond(request: HttpRequest) -> HttpResponse:
        instant[0] = 2
        return HttpResponse(request.url, 200, b"{}")

    with execution_budget(10, limits=ExecutionLimits(max_duration_seconds=1)):
        result = RecipeExecutor(
            FunctionHttpAdapter(respond), MemoryEvidenceStore(), SourceSettings()
        ).execute(
            two_requests(),
            ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_duration_seconds=10)),
        )
    assert isinstance(result, SourceFailure) and result.error_code == "operation_timeout"
    assert result.diagnostics.transport_attempts == 1


@pytest.mark.parametrize("widen", [False, True])
def test_registered_handler_receives_and_preserves_effective_limits(widen: bool) -> None:
    seen: list[ExecutionLimits] = []

    class Handler:
        def execute(self, step: RecipeStep, context: ExecutionContext) -> ExecutionContext:
            assert context.limits is not None
            seen.append(context.limits)
            return context.model_copy(
                update={
                    "values": {"rows": [{"external_tender_id": str(i)} for i in range(5)]},
                    "limits": context.limits.model_copy(update={"max_requests": 20})
                    if widen
                    else context.limits,
                }
            )

    handlers: Registry[StepExecutor] = Registry()
    handlers.register("extract", Handler())
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "nested",
            "version": "1",
            "steps": [
                {
                    "type": "extract",
                    "parameters": {
                        "format": "json",
                        "input": [],
                        "output": "rows",
                        "emit": True,
                    },
                }
            ],
        }
    )
    with execution_budget(10, limits=ExecutionLimits(max_requests=1, max_tenders=2)):
        result = RecipeExecutor(
            FunctionHttpAdapter(lambda _: pytest.fail("unexpected HTTP")),
            MemoryEvidenceStore(),
            SourceSettings(),
            handlers=handlers,
        ).execute(
            recipe, ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_requests=100))
        )
    assert len(seen) == 1 and seen[0].max_requests == 1 and seen[0].max_tenders == 2
    if widen:
        assert (
            isinstance(result, SourceFailure) and "changed execution limits" in result.error_detail
        )
    else:
        assert isinstance(result, CollectionRun) and len(result.raw_observations) == 2
        assert result.coverage == "sampled" and not result.complete
