import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import Engine

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.diagnostics import receipt_report, record_report, source_report
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.recipes import RecipeExecutor
from tender_system.collection.source_runs import RunReceipt, SourceRunService
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import SourceRecipe, SourceRecord
from tender_system.core.ports import HttpResponse
from tender_system.persistence.storage import (
    SqlSourceRepository,
    SqlTenderRepository,
    TransactionalRunStore,
    session_factory,
)
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.fakes import FakeClock, ScriptedHttpAdapter

pytestmark = pytest.mark.database
SOURCE = SourceRecord(id="fixture", name="Local evidence-calibration fixture")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def row(identifier: str, title: str = "Equipment") -> dict[str, JsonValue]:
    return {"external_tender_id": identifier, "title": title}


def definition(
    rows: list[dict[str, JsonValue]],
    *,
    proof: bool | None = True,
    complete: bool = True,
    mode: str = "full",
    as_of: str | None = None,
) -> SourceRecipe:
    return SourceRecipe.model_validate(
        {
            "source_id": SOURCE.id,
            "version": "1",
            "coverage": mode,
            "completion": complete,
            "expected_count": len(rows),
            "data_as_of": as_of,
            "completion_signal": None
            if proof is None
            else {"kind": "total_reconciled", "value": len(rows) if proof else -1},
            "checkpoint": "new-cursor",
            "steps": [
                {
                    "type": "transform",
                    "parameters": {"name": "identity", "input": rows, "emit": True},
                }
            ],
        }
    )


class Collector:
    def __init__(self, engine: Engine) -> None:
        self.sessions = session_factory(engine)
        self.clock = FakeClock(NOW)
        self.store = TransactionalRunStore(self.sessions, clock=self.clock)
        self.evidence = MemoryEvidenceStore()
        with self.sessions.begin() as session:
            SqlSourceRepository(session).add(SOURCE)

    def collect(
        self,
        recipe: SourceRecipe,
        settings: SourceSettings | None = None,
        responses: tuple[HttpResponse | Exception, ...] = (),
        run_id: UUID | None = None,
    ) -> RunReceipt:
        self.clock.instant += timedelta(minutes=1)
        policy = settings or fixture_source_settings()
        service = SourceRunService(
            RecipeExecutor(
                ScriptedHttpAdapter(responses),
                self.evidence,
                policy,
                clock=self.clock,
                sleep=lambda _: None,
            ),
            ConfigurableTenderNormalizer(),
            self.store,
            clock=self.clock,
        )
        return service.run(run_id or uuid4(), recipe, SOURCE, policy)

    def missing(self, identifier: str) -> int:
        with self.sessions() as session:
            tender = next(
                t
                for t in SqlTenderRepository(session).for_source(SOURCE.id)
                if t.external_tender_id == identifier
            )
            return tender.presence.missing_successful_runs


@pytest.fixture
def collector(migrated_engine: Engine) -> Collector:
    return Collector(migrated_engine)


@pytest.mark.parametrize(
    "variant",
    [
        "unknown",
        "incomplete",
        "no_terminal",
        "incremental",
        "bounded_incremental",
        "suspicious_empty",
    ],
)
def test_false_absence_families_leave_unseen_tender_counters_unchanged(
    collector: Collector, variant: str
) -> None:
    first = collector.collect(definition([row("a"), row("b")]))
    assert (
        first.status == "accepted"
        and first.health is not None
        and first.health.absence_evidence_allowed
    )
    options: dict[str, Any] = {}
    if variant == "unknown":
        options["proof"] = None
    if variant == "incomplete":
        options["complete"] = False
    if variant == "no_terminal":
        options["proof"] = False
    if variant == "incremental":
        options["mode"] = "incremental"
    if variant == "bounded_incremental":
        options.update(mode="incremental", proof=None)
    recipe = definition([] if variant == "suspicious_empty" else [row("a", "Changed")], **options)
    policy = fixture_source_settings()
    if variant == "bounded_incremental":
        policy = policy.model_copy(
            update={
                "calibration": policy.calibration.model_copy(
                    update={
                        "expected_mode": "incremental",
                        "full_inventory_supported": False,
                        "freshness": policy.calibration.freshness.model_copy(
                            update={"basis": "unknown", "unknown_policy": "observe_only"}
                        ),
                    }
                )
            }
        )
    receipt = collector.collect(recipe, settings=policy)
    assert receipt.health is not None and not receipt.health.absence_evidence_allowed
    assert receipt.missing_incremented == 0 and collector.missing("b") == 0
    accepted_incremental = variant in {"incremental", "bounded_incremental"}
    assert receipt.status == ("accepted" if accepted_incremental else "rejected")
    if variant == "bounded_incremental":
        assert receipt.health.publication_allowed
        assert receipt.health.freshness.status == "unknown"
        assert receipt.health.dimensions["inventory"] == "unknown"
        assert len(receipt.tender_ids) == 1
        with collector.sessions() as session:
            tender = next(
                item
                for item in SqlTenderRepository(session).for_source(SOURCE.id)
                if item.external_tender_id == "a"
            )
            assert tender.title == "Changed"
            assert len(SqlTenderRepository(session).history(tender.internal_id)) == 2
    if not accepted_incremental:
        with collector.sessions() as session:
            stored = SqlTenderRepository(session).get(first.tender_ids[0])
            assert stored is not None and stored.title == "Equipment"
            assert len(SqlTenderRepository(session).history(stored.internal_id)) == 1


def test_legitimate_missing_then_reappearance_and_run_replay(collector: Collector) -> None:
    collector.collect(definition([row("a"), row("b")]))
    recipe = definition([row("a")])
    run_id = uuid4()
    b = collector.collect(recipe, run_id=run_id)
    assert b.missing_incremented == 1 and collector.missing("b") == 1
    assert collector.collect(recipe, run_id=run_id) == b and collector.missing("b") == 1
    c = collector.collect(recipe)
    assert c.missing_incremented == 1 and collector.missing("b") == 2
    d = collector.collect(definition([row("a"), row("b")], mode="incremental"))
    assert d.status == "accepted" and not d.health.absence_evidence_allowed  # type: ignore[union-attr]
    assert collector.missing("b") == 0


def test_legitimate_zero_uses_reported_zero_plus_completion(collector: Collector) -> None:
    collector.collect(definition([row("a")]))
    values = fixture_source_settings().model_dump()
    values["health"]["allow_empty"] = True
    empty = collector.collect(definition([]), SourceSettings.model_validate(values))
    assert empty.status == "accepted" and empty.health is not None
    assert (
        empty.health.empty_classification == "valid_empty" and empty.health.absence_evidence_allowed
    )
    assert collector.missing("a") == 1


@pytest.mark.parametrize(
    "kind", ["malformed", "duplicates", "stale", "timestamp_missing", "collapse"]
)
def test_adversarial_publication_rejection_has_no_false_missing(
    collector: Collector, kind: str
) -> None:
    rows = [row(str(i)) for i in range(10)]
    collector.collect(definition(rows))
    policy = fixture_source_settings()
    incoming = [row("0", "Questionable")]
    as_of = None
    if kind == "malformed":
        incoming = [{**item, "value": "bad"} for item in rows]
    if kind == "duplicates":
        incoming = [row("0")] * 10
    if kind in {"stale", "timestamp_missing"}:
        values = policy.model_dump()
        values["calibration"]["freshness"] = {
            "basis": "inventory_timestamp",
            "timestamp_meaning": "Fixture inventory cutoff",
            "max_age_seconds": 3600,
        }
        policy = SourceSettings.model_validate(values)
        as_of = "2025-12-01T00:00:00Z" if kind == "stale" else None
        incoming = rows
    receipt = collector.collect(definition(incoming, as_of=as_of), policy)
    assert receipt.status == "rejected" and receipt.health is not None
    assert not receipt.health.absence_evidence_allowed and collector.missing("9") == 0
    assert collector.store.snapshot(SOURCE.id).sequence == 1


def test_tolerated_malformed_run_only_publishes_valid_seen_records_and_holds_cursor(
    collector: Collector,
) -> None:
    collector.collect(definition([row("a"), row("b"), row("c")]))
    values = fixture_source_settings().model_dump()
    values["calibration"]["maximum_malformed_ratio"] = 0.5
    policy = SourceSettings.model_validate(values)
    receipt = collector.collect(
        definition([row("a", "Updated"), row("b"), {**row("c"), "value": "broken"}]).model_copy(
            update={"checkpoint": "must-not-advance"}
        ),
        policy,
    )
    assert (
        receipt.status == "accepted"
        and receipt.health is not None
        and receipt.health.overall_status == "degraded"
    )
    assert not receipt.health.absence_evidence_allowed and not receipt.checkpoint_advanced
    assert (
        collector.missing("c") == 0
        and collector.store.snapshot(SOURCE.id).checkpoint == "new-cursor"
    )
    assert collector.store.snapshot(SOURCE.id).accepted_full_runs == 1


def test_incremental_does_not_refresh_full_inventory_or_unseen_record(collector: Collector) -> None:
    first = collector.collect(definition([row("a"), row("b")]))
    full = collector.store.snapshot(SOURCE.id)
    with collector.sessions() as session:
        before = SqlTenderRepository(session).get(first.tender_ids[1])
        assert before is not None
    collector.clock.instant += timedelta(days=2)
    receipt = collector.collect(definition([row("a")], mode="incremental"))
    snapshot = collector.store.snapshot(SOURCE.id)
    assert snapshot.last_full_verified_at == full.last_full_verified_at
    assert snapshot.last_full_evidence_reference == full.last_full_evidence_reference
    report = source_report(SOURCE.id, snapshot, fixture_source_settings(), collector.clock.now())
    current = report["freshness"]
    assert (
        isinstance(current, dict)
        and isinstance(current["full_inventory"], dict)
        and isinstance(current["incremental_liveness"], dict)
    )
    assert (
        current["full_inventory"]["status"] == "stale"
        and current["incremental_liveness"]["status"] == "fresh"
    )
    with collector.sessions() as session:
        after = SqlTenderRepository(session).get(before.internal_id)
        assert after is not None and after.last_seen_at == before.last_seen_at
        old_record = record_report(after, first, collector.clock.now())
        refreshed = SqlTenderRepository(session).get(receipt.tender_ids[0])
        assert refreshed is not None
        new_record = record_report(refreshed, receipt, collector.clock.now())
        assert (
            isinstance(old_record["freshness"], dict)
            and old_record["freshness"]["status"] == "stale"
        )
        assert (
            isinstance(new_record["freshness"], dict)
            and new_record["freshness"]["status"] == "fresh"
        )
    assert receipt.health is not None and not receipt.health.absence_evidence_allowed


def page_recipe() -> SourceRecipe:
    return SourceRecipe.model_validate(
        {
            "source_id": SOURCE.id,
            "version": "1",
            "completion": {"$ref": "done"},
            "completion_signal": {"kind": "next_cursor_absent", "value": {"$ref": "document.next"}},
            "expected_count": {"$ref": "document.total"},
            "steps": [
                {
                    "type": "transform",
                    "parameters": {
                        "name": "identity",
                        "input": "https://fixture.invalid/1",
                        "output": "url",
                    },
                },
                {
                    "type": "loop",
                    "parameters": {"until": {"$ref": "done"}, "max_iterations": 3},
                    "steps": [
                        {
                            "type": "http_request",
                            "parameters": {"url": {"$ref": "url"}, "inventory_page": True},
                        },
                        {
                            "type": "extract",
                            "parameters": {
                                "format": "json",
                                "input": {"$ref": "response.body"},
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
                        {
                            "type": "transform",
                            "parameters": {
                                "name": "get",
                                "input": {"$ref": "document"},
                                "arguments": {"path": "next"},
                                "output": "url",
                            },
                        },
                        {
                            "type": "transform",
                            "parameters": {
                                "name": "equals",
                                "input": {"$ref": "url"},
                                "arguments": {"other": None},
                                "output": "done",
                            },
                        },
                    ],
                },
            ],
        }
    )


def page(payload: JsonValue) -> HttpResponse:
    return HttpResponse("https://fixture.invalid/page", 200, json.dumps(payload).encode())


def paginated_policy() -> SourceSettings:
    values = fixture_source_settings().model_dump()
    values["calibration"].update(
        completion_signals=["next_cursor_absent"], minimum_pages=1, require_reported_total=True
    )
    return SourceSettings.model_validate(values)


@pytest.mark.parametrize(
    "case",
    [
        "later_failure",
        "repeats_forever",
        "repeats_then_terminates",
        "cursor_disappears",
        "wrong_total",
    ],
)
def test_pagination_adversaries_never_advance_absence(collector: Collector, case: str) -> None:
    collector.collect(definition([row("a"), row("b")]))
    next_page = "https://fixture.invalid/2"
    first = page({"items": [row("a")], "next": next_page, "total": 2})
    responses: tuple[HttpResponse | Exception, ...]
    if case == "later_failure":
        responses = (first, TimeoutError(), TimeoutError(), TimeoutError())
    elif case == "cursor_disappears":
        responses = (page({"items": [row("a")], "total": 2}),)
    elif case == "wrong_total":
        responses = (page({"items": [row("a")], "next": None, "total": 2}),)
    elif case == "repeats_then_terminates":
        responses = (
            first,
            page({"items": [], "next": next_page, "total": 2}),
            page({"items": [row("b")], "next": None, "total": 2}),
        )
    else:
        responses = (
            first,
            page({"items": [], "next": next_page, "total": 2}),
            page({"items": [], "next": next_page, "total": 2}),
        )
    receipt = collector.collect(page_recipe(), paginated_policy(), responses)
    assert receipt.status in {"rejected", "failed"} and receipt.health is not None
    assert not receipt.health.absence_evidence_allowed and receipt.missing_incremented == 0
    assert collector.missing("b") == 0
    assert collector.evidence.get(receipt.evidence_reference)["observations"]


def test_positive_paginated_api_signal_reconciles_total_and_pages(collector: Collector) -> None:
    # Cursor exhaustion proves traversal; a source snapshot additionally proves consistency.
    recipe = page_recipe().model_copy(update={"inventory_snapshot": {"$ref": "document.snapshot"}})
    receipt = collector.collect(
        recipe,
        paginated_policy(),
        (
            page(
                {
                    "items": [row("a")],
                    "next": "https://fixture.invalid/2",
                    "total": 2,
                    "snapshot": "inventory-revision-1",
                }
            ),
            page(
                {"items": [row("b")], "next": None, "total": 2, "snapshot": "inventory-revision-1"}
            ),
        ),
    )
    assert (
        receipt.status == "accepted"
        and receipt.health is not None
        and receipt.health.absence_evidence_allowed
    )
    assert len(receipt.health.diagnostics.pages) == 2
    assert (
        receipt.health.diagnostics.completion is not None
        and receipt.health.diagnostics.completion.kind == "next_cursor_absent"
    )


def test_unsnapshotted_cursor_traversal_preserves_records_without_authorizing_absence(
    collector: Collector,
) -> None:
    receipt = collector.collect(
        page_recipe(),
        paginated_policy(),
        (
            page({"items": [row("a")], "next": "https://fixture.invalid/2", "total": 2}),
            page({"items": [row("b")], "next": None, "total": 2}),
        ),
    )
    assert receipt.status == "accepted" and receipt.health is not None
    assert not receipt.health.absence_evidence_allowed
    assert receipt.missing_incremented == 0
    assert "pagination_consistency_unproven" in {r.code for r in receipt.health.reasons}
    assert receipt_report(receipt)["missing_incremented"] == 0


def test_old_source_timestamp_replay_under_new_run_id_never_counts_absence(
    collector: Collector,
) -> None:
    values = fixture_source_settings().model_dump()
    values["calibration"]["freshness"] = {
        "basis": "inventory_timestamp",
        "timestamp_meaning": "Inventory snapshot",
        "max_age_seconds": 3600,
    }
    policy = SourceSettings.model_validate(values)
    collector.collect(definition([row("a"), row("b")], as_of=NOW.isoformat()), policy)
    receipt = collector.collect(definition([row("a")], as_of=NOW.isoformat()), policy)
    assert receipt.status == "rejected" and "source_timestamp_not_advanced" in receipt.reasons
    assert collector.missing("b") == 0


def markup_recipe(format: str) -> SourceRecipe:
    fields: dict[str, JsonValue] = {
        "external_tender_id": {
            "attribute": "data-id" if format == "html" else "id",
            "required": True,
        },
        "title": {"selector": "h1" if format == "html" else "name", "required": True},
    }
    return SourceRecipe.model_validate(
        {
            "source_id": SOURCE.id,
            "version": "markup-1",
            "coverage": "full",
            "completion_signal": {"kind": "end_marker", "value": {"$ref": "terminal"}},
            "steps": [
                {
                    "type": "http_request",
                    "parameters": {"url": "https://fixture.invalid/list", "inventory_page": True},
                },
                {
                    "type": "extract",
                    "parameters": {
                        "format": format,
                        "input": {"$ref": "response.body"},
                        "selector": "footer" if format == "html" else "end",
                        "fields": {"marker": {"attribute": "complete", "required": True}},
                        "output": "boundary",
                    },
                },
                {
                    "type": "transform",
                    "parameters": {
                        "name": "equals",
                        "input": {"$ref": "boundary.0.marker"},
                        "arguments": {"other": "yes"},
                        "output": "terminal",
                    },
                },
                {
                    "type": "extract",
                    "parameters": {
                        "format": format,
                        "input": {"$ref": "response.body"},
                        "selector": "article" if format == "html" else "t",
                        "fields": fields,
                        "require_match": False,
                        "emit": True,
                    },
                },
            ],
        }
    )


def markup(
    format: str, *, item: bool = True, terminal: bool = True, extra: str = ""
) -> HttpResponse:
    marker = "yes" if terminal else "no"
    if format == "html":
        content = f'<footer complete="{marker}"></footer>'
        if item:
            content += '<article data-id="a"><h1>Equipment</h1></article>'
        content += extra
    else:
        content = f'<feed><end complete="{marker}"/>'
        if item:
            content += '<t id="a"><name>Equipment</name></t>'
        content += extra + "</feed>"
    return HttpResponse("https://fixture.invalid/list", 200, content.encode())


@pytest.mark.parametrize("format", ["html", "xml"])
@pytest.mark.parametrize(
    "scenario",
    ["healthy", "terminal_missing", "valid_empty", "unknown_empty", "session", "maintenance"],
)
def test_markup_source_calibration(collector: Collector, format: str, scenario: str) -> None:
    collector.collect(definition([row("a"), row("b")]))
    values = fixture_source_settings().model_dump()
    values["health"]["allow_empty"] = True
    values["calibration"].update(
        minimum_pages=1,
        empty_policy="signature",
        empty_signatures=["NO_ACTIVE_TENDERS"],
        session_expiry_signatures=["SESSION_EXPIRED"],
        maintenance_signatures=["MAINTENANCE"],
    )
    policy = SourceSettings.model_validate(values)
    extra = {
        "valid_empty": "NO_ACTIVE_TENDERS",
        "session": "SESSION_EXPIRED",
        "maintenance": "MAINTENANCE",
    }.get(scenario, "")
    response = markup(
        format,
        item=scenario in {"healthy", "terminal_missing"},
        terminal=scenario != "terminal_missing",
        extra=extra,
    )
    result = collector.collect(markup_recipe(format), policy, (response,))
    assert result.health is not None
    if scenario in {"healthy", "valid_empty"}:
        assert result.status == "accepted" and result.health.absence_evidence_allowed
        assert collector.missing("b") == 1
        assert result.health.freshness.basis == "observed"
        if scenario == "valid_empty":
            assert result.health.empty_classification == "valid_empty"
    else:
        assert result.status == "rejected" and not result.health.absence_evidence_allowed
        assert collector.missing("b") == 0
        if scenario == "terminal_missing":
            assert result.health.completeness == "incomplete"
        if scenario == "session":
            assert result.health.dimensions["session"] == "fail"
        if scenario == "maintenance":
            assert result.health.dimensions["content"] == "fail"


def test_session_signature_blocks_even_plausible_parsed_inventory(collector: Collector) -> None:
    collector.collect(definition([row("a"), row("b")]))
    values = fixture_source_settings().model_dump()
    values["calibration"]["session_expiry_signatures"] = ["SESSION_EXPIRED"]
    result = collector.collect(
        markup_recipe("html"),
        SourceSettings.model_validate(values),
        (markup("html", extra="SESSION_EXPIRED"),),
    )
    assert result.status == "rejected" and result.health is not None
    assert result.health.normalized_count == 1 and collector.missing("b") == 0


def test_operator_reports_receipt_and_current_source_freshness(
    collector: Collector,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tender_system.collection import diagnostics

    result = collector.collect(definition([row("a")]))
    monkeypatch.setattr(diagnostics, "build_engine", lambda _: collector.sessions.kw["bind"])
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg:///unused_local_fixture")
    monkeypatch.setattr("sys.argv", ["tender-health", "run", str(result.id)])
    diagnostics.main()
    output = json.loads(capsys.readouterr().out)
    assert output["health"]["overall_status"] == "healthy"
    assert output["health"]["diagnostics"]["completion"]["observed"]
    assert output["health"]["absence_evidence_allowed"] and output["checkpoint_advanced"]
    monkeypatch.setattr("sys.argv", ["tender-health", "source", SOURCE.id])
    diagnostics.main()
    output = json.loads(capsys.readouterr().out)
    # No config supplied: never reinterpret fixture evidence as current production truth.
    assert output["freshness"]["full_inventory"]["status"] == "unknown"
    assert output["verified_full_runs"] == 1


@pytest.mark.parametrize("variant", ["unknown", "incomplete", "incremental"])
def test_replaceable_health_policy_cannot_bypass_storage_absence_fence(
    collector: Collector, variant: str
) -> None:
    from tender_system.collection.health import ConservativeRunHealth
    from tender_system.collection.source_runs import RunHealthPolicy, request_digest
    from tender_system.core.domain import (
        CollectionRun,
        ExecutionContext,
        HealthDecision,
        SourceSnapshot,
        Tender,
    )
    from tender_system.persistence.storage import SqlRunStore

    class PermissivePolicy:
        def evaluate(
            self,
            run: CollectionRun,
            settings: SourceSettings,
            snapshot: SourceSnapshot,
            previous: tuple[Tender, ...],
            *,
            now: datetime,
        ) -> HealthDecision:
            result = ConservativeRunHealth().evaluate(run, settings, snapshot, previous, now=now)
            # Simulates an incorrectly generous replacement; storage owns the absence invariant.
            return result.model_copy(
                update={
                    "overall_status": "healthy",
                    "completeness": "complete",
                    "publication_allowed": True,
                    "absence_evidence_allowed": True,
                }
            )

    collector.collect(definition([row("a"), row("b")]))
    collector.clock.instant += timedelta(minutes=1)
    recipe = definition(
        [row("a")],
        proof=None if variant == "unknown" else True,
        complete=variant != "incomplete",
        mode="incremental" if variant == "incremental" else "full",
    )
    policy = fixture_source_settings()
    result = RecipeExecutor(
        ScriptedHttpAdapter(()), collector.evidence, policy, clock=collector.clock
    ).execute(recipe, ExecutionContext(run_id=uuid4()))
    assert isinstance(result, CollectionRun)
    candidate = ConfigurableTenderNormalizer().normalize(row("a"), SOURCE, policy).candidate
    assert candidate is not None
    result = result.model_copy(update={"candidates": (candidate,)})
    replacement: RunHealthPolicy = PermissivePolicy()
    with collector.sessions.begin() as session:
        receipt = SqlRunStore(session, health=replacement, clock=collector.clock).publish(
            result, recipe, policy, 1, request_digest(recipe, policy)
        )
    assert receipt.status == "accepted" and receipt.health is not None
    assert not receipt.health.absence_evidence_allowed and receipt.missing_incremented == 0
    assert collector.missing("b") == 0
