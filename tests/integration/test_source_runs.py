from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import Engine, func, select

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.recipes import RecipeExecutor
from tender_system.collection.source_runs import (
    RunReceipt,
    SourceRunService,
    SourceSnapshot,
    request_digest,
)
from tender_system.core.configuration import Configuration, SourceSettings
from tender_system.core.domain import (
    CollectionRun,
    ExecutionContext,
    ExecutionLimits,
    SourceRecipe,
    SourceRecord,
)
from tender_system.core.execution import execution_budget
from tender_system.core.tenders import ConfigurableTenderValidity
from tender_system.persistence.storage import (
    SourceRunRow,
    SourceStateRow,
    SqlRunStore,
    SqlSourceRepository,
    SqlTenderRepository,
    TenderObservationRow,
    TenderRow,
    TenderVersionRow,
    TransactionalRunStore,
    session_factory,
)
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.fakes import FakeClock, ScriptedHttpAdapter

pytestmark = pytest.mark.database
SOURCE = SourceRecord(id="s", name="Fixture source")


def definition(rows: list[dict[str, JsonValue]], **options: JsonValue) -> SourceRecipe:
    return SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": "1",
            "completion_signal": {"kind": "total_reconciled", "value": len(rows)},
            "expected_count": len(rows),
            "steps": [
                {
                    "type": "transform",
                    "parameters": {"name": "identity", "input": rows, "emit": True},
                }
            ],
            **options,
        }
    )


def row(identifier: str, title: str = "Equipment") -> dict[str, JsonValue]:
    return {"external_tender_id": identifier, "title": title}


@pytest.fixture
def registered_engine(migrated_engine: Engine) -> Engine:
    with session_factory(migrated_engine).begin() as session:
        SqlSourceRepository(session).add(SOURCE)
    return migrated_engine


def collect(
    engine: Engine,
    rows: list[dict[str, JsonValue]],
    *,
    index: int = 0,
    settings: SourceSettings | None = None,
    run_id: UUID | None = None,
    options: dict[str, JsonValue] | None = None,
    **recipe_options: JsonValue,
) -> RunReceipt:
    settings = fixture_source_settings(settings)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index))
    executor = RecipeExecutor(ScriptedHttpAdapter(()), MemoryEvidenceStore(), settings, clock=clock)
    service = SourceRunService(
        executor,
        ConfigurableTenderNormalizer(),
        TransactionalRunStore(session_factory(engine), clock=clock),
    )
    return service.run(
        run_id or uuid4(), definition(rows, **(options or {}), **recipe_options), SOURCE, settings
    )


def counts(engine: Engine) -> tuple[int, int, int, int]:
    with session_factory(engine)() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model)) or 0
            for model in (TenderRow, TenderVersionRow, TenderObservationRow, SourceRunRow)
        )  # type: ignore[return-value]


def test_accepted_run_updates_tenders_history_and_checkpoint(registered_engine: Engine) -> None:
    result = collect(registered_engine, [row("1"), row("2")], checkpoint="cursor-2")
    assert result.status == "accepted" and result.accepted_sequence == 1
    assert result.recipe_version == "1"
    assert len(result.tender_ids) == len(result.rematch_tender_ids) == 2
    assert counts(registered_engine) == (2, 2, 2, 1)
    snapshot = TransactionalRunStore(session_factory(registered_engine)).snapshot("s")
    assert snapshot.checkpoint == "cursor-2" and snapshot.last_full_count == 2


def test_inherited_scope_is_bound_to_persisted_run_and_retry_identity(
    registered_engine: Engine,
) -> None:
    rows = [row(str(i)) for i in range(3)]
    key = uuid4()
    limits = ExecutionLimits(max_tenders=2)
    with execution_budget(10, limits=limits):
        receipt = collect(registered_engine, rows, run_id=key)
        repeated = collect(registered_engine, rows, run_id=key)
    assert receipt == repeated
    assert len(receipt.tender_ids) == 2
    assert receipt.request_hash == request_digest(
        definition(rows), fixture_source_settings(), limits
    )
    assert counts(registered_engine) == (2, 2, 2, 1)
    snapshot = TransactionalRunStore(session_factory(registered_engine)).snapshot("s")
    assert snapshot.last_full_count is None
    with pytest.raises(ValueError, match="run ID reused"):
        collect(registered_engine, rows, run_id=key)


def test_optional_title_persists_null_raw_object_text_and_replays_without_fabrication(
    registered_engine: Engine,
) -> None:
    settings = SourceSettings.model_validate(
        {
            "health": {
                "required_fields": ["external_tender_id"],
                "required_any_of": [["title", "description"]],
            },
            "normalization": {"field_mapping": {"description": "object_text"}},
        }
    )
    rows: list[dict[str, JsonValue]] = [
        {"external_tender_id": "1", "title": None, "object_text": "Equipment specification"}
    ]
    run_id = uuid4()
    first = collect(registered_engine, rows, settings=settings, run_id=run_id)
    assert first.status == "accepted"
    assert collect(registered_engine, rows, settings=settings, run_id=run_id, index=1) == first
    assert counts(registered_engine) == (1, 1, 1, 1)
    with session_factory(registered_engine)() as session:
        repository = SqlTenderRepository(session)
        tender = repository.for_source(SOURCE.id)[0]
        assert tender.title is None and tender.description == "Equipment specification"
        assert tender.raw_data == rows[0]
        assert repository.observations(tender.internal_id)[0].evidence_reference
    rows[0]["object_text"] = "Updated equipment specification"
    changed = collect(registered_engine, rows, settings=settings, index=2)
    assert changed.status == "accepted" and len(changed.rematch_tender_ids) == 1
    with session_factory(registered_engine)() as session:
        repository = SqlTenderRepository(session)
        tender = repository.for_source(SOURCE.id)[0]
        assert tender.title is None and tender.description == rows[0]["object_text"]
        assert len(repository.history(tender.internal_id)) == 2


@pytest.mark.parametrize(
    "options,expected",
    [({"completion": False}, "incomplete_run"), ({"expected_count": 5}, "expected_count_mismatch")],
)
def test_incomplete_or_wrong_count_run_does_not_mutate_accepted_state(
    registered_engine: Engine,
    options: dict[str, JsonValue],
    expected: str,
) -> None:
    first = collect(registered_engine, [row("1")], checkpoint="good")
    rejected = collect(
        registered_engine, [row("1", "Questionable update")], index=1, options=options
    )
    assert rejected.status == "rejected" and expected in rejected.reasons
    assert rejected.failure is not None
    assert rejected.failure.last_good_evidence_reference == first.evidence_reference
    assert counts(registered_engine) == (1, 1, 1, 2)
    with session_factory(registered_engine)() as session:
        tender = SqlTenderRepository(session).get(first.tender_ids[0])
        assert tender is not None and tender.title == "Equipment"
        assert tender.presence.missing_successful_runs == 0
        assert SqlRunStore(session).snapshot("s").checkpoint == "good"


def test_suspicious_zero_result_run_rejected(registered_engine: Engine) -> None:
    collect(registered_engine, [row("1")])
    result = collect(registered_engine, [], index=1)
    assert result.status == "rejected" and "unexpected_empty_run" in result.reasons
    with session_factory(registered_engine)() as session:
        tender = SqlTenderRepository(session).for_source("s")[0]
        assert tender.presence.missing_successful_runs == 0


def test_explicitly_allowed_empty_full_run_establishes_absence(registered_engine: Engine) -> None:
    collect(registered_engine, [row("1")])
    settings = SourceSettings.model_validate({"health": {"allow_empty": True}})
    result = collect(registered_engine, [], index=1, settings=settings)
    assert result.status == "accepted"
    with session_factory(registered_engine)() as session:
        assert SqlTenderRepository(session).for_source("s")[0].presence.missing_successful_runs == 1


def test_disappearance_threshold_and_reappearance(registered_engine: Engine) -> None:
    first = collect(registered_engine, [row("1"), row("2")])
    settings = SourceSettings.model_validate(
        {"health": {"allow_empty": True}, "validity": {"missing_successful_runs": 2}}
    )
    for index in (1, 2):
        assert (
            collect(registered_engine, [row("2")], index=index, settings=settings).status
            == "accepted"
        )
    with session_factory(registered_engine)() as session:
        tender = SqlTenderRepository(session).get(first.tender_ids[0])
        assert tender is not None and tender.presence.missing_successful_runs == 2
        assert (
            not ConfigurableTenderValidity()
            .evaluate(tender, settings, datetime(2026, 1, 2, tzinfo=UTC))
            .matchable
        )
        assert len(SqlTenderRepository(session).history(tender.internal_id)) == 1
    collect(registered_engine, [row("1"), row("2")], index=3, settings=settings)
    with session_factory(registered_engine)() as session:
        tender = SqlTenderRepository(session).get(first.tender_ids[0])
        assert tender is not None and tender.presence.missing_successful_runs == 0
        assert (
            ConfigurableTenderValidity()
            .evaluate(tender, settings, datetime(2026, 1, 2, tzinfo=UTC))
            .matchable
        )
        assert len(SqlTenderRepository(session).history(tender.internal_id)) == 1


def test_incremental_runs_never_establish_absence(registered_engine: Engine) -> None:
    collect(registered_engine, [row("1"), row("2")], checkpoint="A")
    result = collect(
        registered_engine, [row("1", "Updated")], index=1, coverage="incremental", checkpoint="B"
    )
    assert result.status == "accepted"
    with session_factory(registered_engine)() as session:
        tenders = SqlTenderRepository(session).for_source("s")
        assert all(tender.presence.missing_successful_runs == 0 for tender in tenders)
        snapshot = SqlRunStore(session).snapshot("s")
        assert snapshot.checkpoint == "B" and snapshot.last_full_count == 2


def test_sampled_run_cannot_advance_checkpoint_even_with_permissive_health(
    registered_engine: Engine,
) -> None:
    first = collect(registered_engine, [row("1"), row("2")], checkpoint="A")
    assert first.health is not None
    settings = fixture_source_settings()
    recipe = definition([row("1")], checkpoint="filtered")
    clock = FakeClock(datetime(2026, 1, 1, 1, tzinfo=UTC))
    execution = RecipeExecutor(
        ScriptedHttpAdapter(()), MemoryEvidenceStore(), settings, clock=clock
    ).execute(recipe, ExecutionContext(run_id=uuid4()))
    assert isinstance(execution, CollectionRun)
    candidate = ConfigurableTenderNormalizer().normalize(row("1"), SOURCE, settings).candidate
    assert candidate is not None
    execution = execution.model_copy(
        update={"candidates": (candidate,), "coverage": "sampled", "complete": False}
    )

    class PermissiveHealth:
        def evaluate(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            return first.health

    with session_factory(registered_engine).begin() as session:
        store = SqlRunStore(session, health=PermissiveHealth(), clock=clock)
        receipt = store.publish(execution, recipe, settings, 1, request_digest(recipe, settings))
        assert receipt.status == "accepted"
        assert not receipt.checkpoint_advanced
        assert store.snapshot("s").checkpoint == "A"
        assert all(
            tender.presence.missing_successful_runs == 0
            for tender in SqlTenderRepository(session).for_source("s")
        )


def test_suspicious_count_drop_and_configurable_policy(registered_engine: Engine) -> None:
    collect(registered_engine, [row(str(i)) for i in range(10)])
    result = collect(registered_engine, [row("0")], index=1)
    assert result.status == "rejected" and "suspicious_count_drop" in result.reasons
    settings = SourceSettings.model_validate({"health": {"minimum_retained_ratio": 0}})
    assert collect(registered_engine, [row("0")], index=2, settings=settings).status == "accepted"


@pytest.mark.parametrize(
    "rows,reason",
    [
        ([row("1"), row("1")], "duplicate_identity"),
        ([{"external_tender_id": "1"}], "missing_required_fields"),
        ([{**row("1"), "deadline": "not a date"}], "normalization_failed"),
    ],
)
def test_quality_rejection_preserves_raw_evidence_and_no_tenders(
    registered_engine: Engine,
    rows: list[dict[str, JsonValue]],
    reason: str,
) -> None:
    result = collect(registered_engine, rows)
    assert result.status == "rejected" and reason in result.reasons
    assert counts(registered_engine) == (0, 0, 0, 1)
    assert result.failure is not None
    if reason == "normalization_failed":
        assert result.failure.issues[0].field == "deadline"


def test_warning_normalization_requires_explicit_health_policy(registered_engine: Engine) -> None:
    settings = SourceSettings.model_validate(
        {"normalization": {"parse_failure_policy": "null_with_issue"}}
    )
    result = collect(registered_engine, [{**row("1"), "deadline": "bad"}], settings=settings)
    assert result.status == "rejected" and "data_quality_issues" in result.reasons
    permissive = settings.model_dump()
    permissive["health"]["reject_quality_issues"] = False
    assert (
        collect(
            registered_engine,
            [{**row("1"), "deadline": "bad"}],
            settings=SourceSettings.model_validate(permissive),
            index=1,
        ).status
        == "accepted"
    )


def test_same_run_id_replays_receipt_without_reexecuting(registered_engine: Engine) -> None:
    definition_ = definition([row("1")])
    settings = fixture_source_settings()
    http = ScriptedHttpAdapter(())
    executor = RecipeExecutor(http, MemoryEvidenceStore(), settings)
    store = TransactionalRunStore(session_factory(registered_engine))
    service = SourceRunService(executor, ConfigurableTenderNormalizer(), store)
    run_id = uuid4()
    first = service.run(run_id, definition_, SOURCE, settings)
    assert service.run(run_id, definition_, SOURCE, settings) == first
    assert counts(registered_engine) == (1, 1, 1, 1)
    with pytest.raises(ValueError, match="reused"):
        service.run(run_id, definition([row("1", "other")]), SOURCE, settings)


def test_distinct_repeated_runs_create_no_fake_versions(registered_engine: Engine) -> None:
    first = collect(registered_engine, [row("1")])
    second = collect(registered_engine, [row("1")], index=1)
    assert first.tender_ids == second.tender_ids and second.rematch_tender_ids == ()
    assert counts(registered_engine) == (1, 1, 2, 2)


def test_outer_transaction_rollback_leaves_no_partial_run_or_tender(
    registered_engine: Engine,
) -> None:
    settings = fixture_source_settings()
    recipe = definition([row("1")], checkpoint="new")
    execution = RecipeExecutor(ScriptedHttpAdapter(()), MemoryEvidenceStore(), settings).execute(
        recipe, ExecutionContext(run_id=uuid4())
    )
    assert isinstance(execution, CollectionRun)
    candidate = ConfigurableTenderNormalizer().normalize(row("1"), SOURCE, settings).candidate
    assert candidate is not None
    execution = execution.model_copy(update={"candidates": (candidate,)})
    with pytest.raises(RuntimeError), session_factory(registered_engine).begin() as session:
        assert (
            SqlRunStore(session)
            .publish(execution, recipe, settings, 0, request_digest(recipe, settings))
            .status
            == "accepted"
        )
        raise RuntimeError("simulate process crash before commit")
    assert counts(registered_engine) == (0, 0, 0, 0)
    assert (
        TransactionalRunStore(session_factory(registered_engine)).snapshot("s") == SourceSnapshot()
    )
    result = collect(registered_engine, [row("1")], run_id=execution.id, checkpoint="new")
    assert result.status == "accepted" and counts(registered_engine) == (1, 1, 1, 1)


def test_mid_batch_crash_savepoint_rolls_back_even_when_caller_catches(
    registered_engine: Engine,
) -> None:
    class BrokenRepository(SqlTenderRepository):
        def account_presence(
            self, source_id: str, seen: tuple[UUID, ...], sequence: int, full: bool
        ) -> None:
            super().account_presence(source_id, seen, sequence, full)
            raise RuntimeError("failure after writes and absence accounting")

    settings = fixture_source_settings()
    recipe = definition([row("1"), row("2")])
    execution = RecipeExecutor(ScriptedHttpAdapter(()), MemoryEvidenceStore(), settings).execute(
        recipe, ExecutionContext(run_id=uuid4())
    )
    assert isinstance(execution, CollectionRun)
    candidates = tuple(
        ConfigurableTenderNormalizer().normalize(raw, SOURCE, settings).candidate
        for raw in execution.raw_observations
    )
    execution = CollectionRun.model_validate({**execution.model_dump(), "candidates": candidates})
    with session_factory(registered_engine).begin() as session:
        with pytest.raises(RuntimeError):
            SqlRunStore(session, BrokenRepository(session)).publish(
                execution, recipe, settings, 0, request_digest(recipe, settings)
            )
    assert counts(registered_engine) == (0, 0, 0, 0)


def test_stale_snapshot_rejected_without_losing_checkpoint(registered_engine: Engine) -> None:
    settings = fixture_source_settings()
    recipe = definition([row("1", "Stale")], checkpoint="stale")
    execution = RecipeExecutor(ScriptedHttpAdapter(()), MemoryEvidenceStore(), settings).execute(
        recipe, ExecutionContext(run_id=uuid4())
    )
    assert isinstance(execution, CollectionRun)
    candidate = (
        ConfigurableTenderNormalizer().normalize(row("1", "Stale"), SOURCE, settings).candidate
    )
    execution = CollectionRun.model_validate({**execution.model_dump(), "candidates": [candidate]})
    collect(registered_engine, [row("1", "Current")], checkpoint="current")
    with session_factory(registered_engine).begin() as session:
        receipt = SqlRunStore(session).publish(
            execution, recipe, settings, 0, request_digest(recipe, settings)
        )
        assert receipt.status == "rejected" and "stale_baseline" in receipt.reasons
        assert SqlRunStore(session).snapshot("s").checkpoint == "current"
        assert SqlTenderRepository(session).for_source("s")[0].title == "Current"


def test_failure_is_durable_and_never_establishes_absence(registered_engine: Engine) -> None:
    good = collect(registered_engine, [row("1")])
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": "1",
            "steps": [{"type": "http_request", "parameters": {"url": "https://fixture.test"}}],
        }
    )
    service = SourceRunService(
        RecipeExecutor(
            ScriptedHttpAdapter((TimeoutError(),) * 3),
            MemoryEvidenceStore(),
            SourceSettings(),
            sleep=lambda _: None,
        ),
        ConfigurableTenderNormalizer(),
        TransactionalRunStore(session_factory(registered_engine)),
    )
    receipt = service.run(uuid4(), recipe, SOURCE, SourceSettings())
    assert receipt.status == "failed" and receipt.failure is not None
    assert receipt.failure.last_good_evidence_reference == good.evidence_reference
    with session_factory(registered_engine)() as session:
        assert SqlTenderRepository(session).for_source("s")[0].presence.missing_successful_runs == 0
        assert session.get(SourceStateRow, "s").accepted_sequence == 1  # type: ignore[union-attr]


def test_health_overrides_are_scoped_and_isolated() -> None:
    config = Configuration.model_validate(
        {
            "global": {"source": {"health": {"allow_empty": False, "minimum_count": 2}}},
            "sources": {"a": {"health": {"allow_empty": True, "minimum_count": 0}}},
        }
    )
    a, b = config.for_source("a"), config.for_source("b")
    assert a.health.allow_empty and a.health.minimum_count == 0
    assert not b.health.allow_empty and b.health.minimum_count == 2


@pytest.mark.parametrize("corruption", ["deadline", "title", "value", "nullification"])
def test_plausible_mass_scraper_corruption_is_rejected(
    registered_engine: Engine, corruption: str
) -> None:
    rows = [
        {
            **row(str(i)),
            "buyer": "Buyer",
            "published_at": "2026-01-01",
            "deadline": "2026-02-01",
            "value": "100",
        }
        for i in range(6)
    ]
    assert collect(registered_engine, rows).status == "accepted"
    damaged = [item.copy() for item in rows]
    for item in damaged:
        if corruption == "deadline":
            item["deadline"] = item["published_at"]
        elif corruption == "title":
            item["title"] = item["buyer"]
        elif corruption == "value":
            item["value"] = "0"
        else:
            item["deadline"] = None
    result = collect(registered_engine, damaged, index=1)
    assert result.status == "rejected" and "suspicious_mass_field_change" in result.reasons
    assert counts(registered_engine) == (6, 6, 6, 2)


def test_login_page_text_is_rejected(registered_engine: Engine) -> None:
    result = collect(registered_engine, [{**row("1"), "description": "Please log in to continue"}])
    assert result.status == "rejected" and "suspected_login_content" in result.reasons


@pytest.mark.parametrize(
    "as_of,reason",
    [
        (None, "missing_freshness_evidence"),
        ("2025-12-01T00:00:00Z", "stale_or_future_source_data"),
        ("2026-01-02T00:00:00Z", "stale_or_future_source_data"),
    ],
)
def test_configured_freshness_gate_refuses_stale_cached_data(
    registered_engine: Engine,
    as_of: str | None,
    reason: str,
) -> None:
    settings = SourceSettings.model_validate({"health": {"max_data_age_seconds": 3600}})
    receipt = collect(registered_engine, [row("1")], settings=settings, data_as_of=as_of)
    assert receipt.status == "rejected" and reason in receipt.reasons


def test_configured_fresh_snapshot_accepted(registered_engine: Engine) -> None:
    settings = SourceSettings.model_validate({"health": {"max_data_age_seconds": 3600}})
    assert (
        collect(
            registered_engine, [row("1")], settings=settings, data_as_of="2026-01-01T00:00:00Z"
        ).status
        == "accepted"
    )


def test_incremental_cursor_with_initial_full_comparison_fallback(
    registered_engine: Engine,
) -> None:
    import json

    from tender_system.core.ports import HttpRequest, HttpResponse
    from tender_system.testing.fakes import FunctionHttpAdapter

    requests: list[str] = []

    def respond(request: HttpRequest) -> HttpResponse:
        requests.append(request.url)
        payload = (
            {"items": [row("1"), row("2")], "cursor": "A"}
            if request.url.endswith("/full")
            else {"items": [row("1", "Updated")], "cursor": "B"}
        )
        return HttpResponse(request.url, 200, json.dumps(payload).encode())

    recipe = SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": "1",
            "completion_signal": {"kind": "snapshot", "value": True},
            "coverage": {"$ref": "coverage"},
            "checkpoint": {"$ref": "document.cursor"},
            "steps": [
                {
                    "type": "transform",
                    "parameters": {
                        "name": "equals",
                        "input": {"$ref": "checkpoint"},
                        "arguments": {"other": None},
                        "output": "initial",
                    },
                },
                {
                    "type": "condition",
                    "parameters": {"predicate": {"$ref": "initial"}},
                    "steps": [
                        {
                            "type": "http_request",
                            "parameters": {"url": "https://fixture.test/full"},
                        },
                        {
                            "type": "transform",
                            "parameters": {
                                "name": "identity",
                                "input": "full",
                                "output": "coverage",
                            },
                        },
                    ],
                    "else_steps": [
                        {
                            "type": "http_request",
                            "parameters": {
                                "url": "https://fixture.test/changes",
                                "query": {"since": {"$ref": "checkpoint"}},
                            },
                        },
                        {
                            "type": "transform",
                            "parameters": {
                                "name": "identity",
                                "input": "incremental",
                                "output": "coverage",
                            },
                        },
                    ],
                },
                {
                    "type": "extract",
                    "parameters": {
                        "input": {"$ref": "response.body"},
                        "format": "json",
                        "output": "document",
                    },
                },
                {
                    "type": "transform",
                    "parameters": {
                        "name": "get",
                        "input": {"$ref": "document"},
                        "arguments": {"path": "items"},
                        "emit": True,
                    },
                },
            ],
        }
    )
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = TransactionalRunStore(session_factory(registered_engine), clock=clock)
    service = SourceRunService(
        RecipeExecutor(
            FunctionHttpAdapter(respond),
            MemoryEvidenceStore(),
            fixture_source_settings(),
            clock=clock,
        ),
        ConfigurableTenderNormalizer(),
        store,
    )
    assert service.run(uuid4(), recipe, SOURCE, fixture_source_settings()).status == "accepted"
    clock.instant += timedelta(hours=1)
    assert service.run(uuid4(), recipe, SOURCE, fixture_source_settings()).status == "accepted"
    assert requests == ["https://fixture.test/full", "https://fixture.test/changes?since=A"]
    assert store.snapshot("s").checkpoint == "B" and store.snapshot("s").last_full_count == 2
    with session_factory(registered_engine)() as session:
        assert all(
            t.presence.missing_successful_runs == 0
            for t in SqlTenderRepository(session).for_source("s")
        )


def test_normalizer_adapter_failure_is_structured_and_durable(registered_engine: Engine) -> None:
    from collections.abc import Mapping

    from tender_system.core.domain import NormalizationResult

    class BrokenNormalizer:
        def normalize(
            self, raw: Mapping[str, JsonValue], source: SourceRecord, settings: SourceSettings
        ) -> NormalizationResult:
            raise RuntimeError("plugin broken")

    service = SourceRunService(
        RecipeExecutor(ScriptedHttpAdapter(()), MemoryEvidenceStore(), SourceSettings()),
        BrokenNormalizer(),
        TransactionalRunStore(session_factory(registered_engine)),
    )
    result = service.run(uuid4(), definition([row("1")]), SOURCE, SourceSettings())
    assert result.status == "failed" and result.failure is not None
    assert result.failure.failed_step == "normalize" and counts(registered_engine) == (0, 0, 0, 1)
