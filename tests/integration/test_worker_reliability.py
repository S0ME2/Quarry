"""Operation health checks against ownership locks in isolated PostgreSQL schemas."""

import json
import multiprocessing
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue, SecretStr
from sqlalchemy import Engine, event, func, select, text
from sqlalchemy.orm import Session

from tender_system.collection.adapters import FileEvidenceStore, HttpxAdapter, MemorySessionStore
from tender_system.collection.browser import PlaywrightBrowserAdapter
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import Configuration, load_configuration
from tender_system.core.domain import CampaignCriteria, ExecutionLimits
from tender_system.core.ports import (
    HttpRequest,
    HttpResponse,
    MatcherFactory,
    ModelRequest,
    ModelResponse,
)
from tender_system.core.registry import Registry
from tender_system.matching.intelligent_matching import IntelligentMatcher
from tender_system.matching.matching import BatchMatchingWorker, CampaignService
from tender_system.operator import manual_cli as cli
from tender_system.operator.operations import OperatorService, operation_lock, recover_operations
from tender_system.operator.operator_contracts import SourceWrite
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.matching_storage import (
    SqlCampaignRepository,
    TransactionalMatchingStore,
    matching_planner,
)
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    MatchJobRow,
    MatchResultRow,
    OperationRow,
    SourceRow,
    SourceRunRow,
    SqlTenderRepository,
    TenderObservationRow,
    TenderRow,
    TenderVersionRow,
    build_engine,
    session_factory,
    validate_test_database,
)
from tender_system.testing.acceptance_site import AcceptanceSite, acceptance_site
from tender_system.testing.fakes import FakeClock, FunctionModelClient

pytestmark = pytest.mark.database


def test_doctor_distinguishes_running_abandoned_and_overdue_without_writes(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sessions = session_factory(migrated_engine)
    config = load_configuration(environ={}, validate_models=False)
    now = datetime.now(UTC)
    healthy, abandoned, overdue = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        for key in (healthy, abandoned, overdue):
            session.add(
                OperationRow(
                    id=key,
                    kind="collection",
                    source_id="fixture",
                    status="running",
                    payload={
                        "limits": ExecutionLimits(max_duration_seconds=1).model_dump(mode="json")
                    }
                    if key == overdue
                    else {},
                    result={},
                    created_at=now,
                    started_at=now - timedelta(seconds=10) if key == overdue else now,
                )
            )
    monkeypatch.setattr(
        cli,
        "database_url_from_environment",
        lambda _: SecretStr(migrated_engine.url.render_as_string(hide_password=False)),
    )
    with sessions.begin() as ownership:
        assert operation_lock(ownership, healthy)
        assert operation_lock(ownership, overdue)
        assert cli.doctor(cli.parser().parse_args(["doctor"]), config) == 1
        report = json.loads(capsys.readouterr().out)
        check = next(item for item in report["checks"] if item["check"] == "operations")
        assert check["passed"] is False
        assert check["healthy_running"] == 1
        assert check["recoverable_abandoned"] == [str(abandoned)]
        assert check["overdue_running"] == [str(overdue)]
        assert "operations" in check["action"]
    with sessions() as session:
        assert list(session.scalars(select(OperationRow.status))) == ["running"] * 3


@pytest.mark.browser
@pytest.mark.parametrize("broken_installation", [False, True])
def test_doctor_browser_requires_actual_local_launch(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    broken_installation: bool,
) -> None:
    monkeypatch.setattr(
        cli,
        "database_url_from_environment",
        lambda _: SecretStr(migrated_engine.url.render_as_string(hide_password=False)),
    )
    if broken_installation:
        from playwright.sync_api import sync_playwright

        # A file at the advertised executable path satisfies the old check, but
        # cannot launch Chromium. Exercise real Playwright against this owned
        # broken installation rather than mocking its implementation.
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
        executable.parent.mkdir(parents=True)
        executable.write_text("Not a working Chromium executable\n")
        assert executable.is_file()
    config = load_configuration(environ={}, validate_models=False)
    assert cli.doctor(cli.parser().parse_args(["doctor", "--browser"]), config) == int(
        broken_installation
    )
    report = json.loads(capsys.readouterr().out)
    check = next(item for item in report["checks"] if item["check"] == "browser")
    assert check["passed"] is not broken_installation
    assert check["launch_verified"] is not broken_installation
    if broken_installation:
        assert check["error_code"] == "browser_unavailable"
        assert "system dependencies" in check["action"]


def _pause(ready: Any, stage: str) -> None:
    ready.put(stage)
    Event().wait(60)
    raise TimeoutError("Parent did not terminate the fixture worker")


def _interrupted_application_worker(
    database_url: str, operation_id: UUID, evidence_root: str, stage: str, ready: Any
) -> None:
    """Instrument external boundaries/SQL events while executing the actual runtime."""
    validate_test_database(database_url)
    engine = build_engine(SecretStr(database_url))
    with engine.connect() as connection:
        assert connection.scalar(text("select current_database()")) == engine.url.database
    sessions = session_factory(engine)

    class InterruptedHttp(HttpxAdapter):
        def request(self, request: HttpRequest) -> HttpResponse:
            result = super().request(request)
            if stage in {"http", "approval"}:
                _pause(ready, stage)
            return result

    class InterruptedBrowser(PlaywrightBrowserAdapter):
        def navigate(self, url: str) -> None:
            super().navigate(url)
            if stage == "browser":
                _pause(ready, stage)

    class InterruptedEvidence(FileEvidenceStore):
        def put(self, run_id: UUID, evidence: Mapping[str, JsonValue]) -> str:
            if stage != "evidence":
                return super().put(run_id, evidence)

            def before_atomic_replace(_source: Any, _destination: Any) -> None:
                # Bytes were written and fsynced, but are not yet a published
                # evidence reference. Interrupt the actual filesystem boundary.
                _pause(ready, stage)

            with patch(
                "tender_system.collection.adapters.os.replace", side_effect=before_atomic_replace
            ):
                return super().put(run_id, evidence)

    def after_flush(session: Session, _: Any) -> None:
        if stage == "publication_transaction" and any(
            isinstance(row, TenderObservationRow) for row in session.new
        ):
            _pause(ready, stage)
        if stage == "activation_transaction" and any(
            isinstance(row, ActiveRecipeRow) for row in session.new
        ):
            _pause(ready, stage)
        if stage == "publication_commit" and any(
            isinstance(row, SourceRunRow) and row.id == operation_id for row in session.new
        ):
            session.info["pause_after_commit"] = True
        if stage == "activation_commit" and any(
            isinstance(row, ActiveRecipeRow) for row in session.new
        ):
            session.info["pause_after_commit"] = True

    def after_commit(session: Session) -> None:
        if not session.in_nested_transaction() and session.info.get("pause_after_commit"):
            _pause(ready, stage)

    event.listen(sessions, "after_flush", after_flush)
    event.listen(sessions, "after_commit", after_commit)
    runtime = OperatorRuntime(
        sessions,
        cli.manual_config(load_configuration(environ={}, validate_models=False)),
        evidence=InterruptedEvidence(Path(evidence_root)),
        http=InterruptedHttp(),
        browsers=lambda: InterruptedBrowser(MemorySessionStore(), timeout_seconds=3),
    )
    try:
        runtime.tick(operation_id)
        ready.put("unexpected_completion")
    finally:
        runtime.close()
        engine.dispose()


@pytest.fixture
def crash_application(
    migrated_engine: Engine,
    tmp_path: Path,
) -> Iterator[tuple[AcceptanceSite, OperatorRuntime, Path]]:
    evidence_root = tmp_path / "failure-evidence"
    with acceptance_site(count=5) as site:
        runtime = OperatorRuntime(
            session_factory(migrated_engine),
            cli.manual_config(load_configuration(environ={}, validate_models=False)),
            evidence=FileEvidenceStore(evidence_root),
            browsers=lambda: PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=3),
        )
        try:
            yield site, runtime, evidence_root
        finally:
            runtime.close()


def _import_and_validate(site: AcceptanceSite, runtime: OperatorRuntime) -> UUID:
    with runtime.sessions.begin() as session:
        svc = OperatorService(session, runtime.config, datetime.now(UTC))
        imported = svc.import_recipe(site.recipe(), site.origin)
        validation = svc.enqueue(
            "recipe_validation",
            "acceptance",
            {
                "operation_id": str(imported.id),
                "limits": ExecutionLimits(max_tenders=3).model_dump(mode="json"),
            },
        )
    assert runtime.tick(validation.id)
    with runtime.sessions() as session:
        origin = session.get(OperationRow, imported.id)
        assert origin and origin.result.get("validation_passed"), origin.result if origin else None
    return imported.id


@pytest.mark.browser
@pytest.mark.parametrize(
    "stage",
    [
        "http",
        "browser",
        "evidence",
        "approval",
        "activation_transaction",
        "activation_commit",
        "publication_transaction",
        "publication_commit",
    ],
)
def test_sigkill_real_application_boundary_recovers_truthful_effects(
    migrated_engine: Engine,
    crash_application: tuple[AcceptanceSite, OperatorRuntime, Path],
    stage: str,
) -> None:
    site, runtime, evidence_root = crash_application
    sessions = runtime.sessions
    imported_id = _import_and_validate(site, runtime)
    with sessions.begin() as session:
        svc = OperatorService(session, runtime.config, datetime.now(UTC))
        if stage in {"browser", "evidence"}:
            operation = svc.enqueue(
                "recipe_validation",
                "acceptance",
                {
                    "operation_id": str(imported_id),
                    "limits": ExecutionLimits(max_tenders=3).model_dump(mode="json"),
                },
            )
        else:
            operation = svc.act(imported_id, "approve")
    if stage.startswith("publication") or stage == "http":
        assert runtime.tick(operation.id)
        with sessions.begin() as session:
            svc = OperatorService(
                session,
                runtime.config,
                datetime.now(UTC),
            )
            svc.configure_source("acceptance", SourceWrite(enabled=True, manual_only=True))
            operation = svc.collect("acceptance", ExecutionLimits(max_tenders=3))
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    process = context.Process(
        target=_interrupted_application_worker,
        args=(
            migrated_engine.url.render_as_string(hide_password=False),
            operation.id,
            str(evidence_root),
            stage,
            ready,
        ),
    )
    process.start()
    try:
        assert ready.get(timeout=25) == stage
        with sessions() as session:
            row = session.get(OperationRow, operation.id)
            assert row and row.status == "running"
        process.kill()
        process.join(timeout=10)
        assert not process.is_alive()
        # Process exit precedes PostgreSQL observing its disconnected socket.
        # Wait for actual ownership release; still require exactly one recovery.
        deadline = monotonic() + 5
        while True:
            with sessions.begin() as session:
                if operation_lock(session, operation.id):
                    assert recover_operations(session, datetime.now(UTC)) == 1
                    break
            assert monotonic() < deadline, "PostgreSQL retained the terminated worker's lock"
            sleep(0.01)
        with sessions() as session:
            row = session.get(OperationRow, operation.id)
            assert row and row.finished_at and row.result["safe_recovery_action"]
            if stage == "publication_commit":
                assert row.status == "completed" and row.error_code is None
                assert row.result["publication_persisted"] is True
                assert row.result["records_processed"] == 3
            elif stage == "activation_commit":
                assert row.status == "activated" and row.error_code is None
                assert row.result["source_activation_persisted"] is True
            else:
                assert row.status == "failed" and row.error_code == "worker_interrupted"
                assert row.result["publication_persisted"] is False
            expected = 3 if stage == "publication_commit" else 0
            for model in (TenderRow, TenderVersionRow, TenderObservationRow):
                assert session.scalar(select(func.count()).select_from(model)) == expected
            source_exists = stage.startswith("publication") or stage in {
                "http",
                "activation_commit",
            }
            assert (session.get(SourceRow, "acceptance") is not None) is source_exists
            assert (session.get(ActiveRecipeRow, "acceptance") is not None) is source_exists
        with sessions.begin() as session:
            assert recover_operations(session, datetime.now(UTC)) == 0
        # Captured files remain checksum-valid after hard termination; no partial
        # evidence payload may become visible as a valid reference.
        for path in evidence_root.glob("*.json"):
            assert runtime.evidence.get(path.stem)
        if stage == "evidence":
            assert list(evidence_root.glob(".evidence-*"))
        # Recovery records effects; the operator explicitly resumes the normal
        # workflow. Previously committed rows must remain idempotent on retry.
        if stage in {"browser", "evidence"}:
            with sessions.begin() as session:
                validation = OperatorService(
                    session,
                    runtime.config,
                    datetime.now(UTC),
                ).enqueue(
                    "recipe_validation",
                    "acceptance",
                    {
                        "operation_id": str(imported_id),
                        "limits": ExecutionLimits(max_tenders=3).model_dump(mode="json"),
                    },
                )
            assert runtime.tick(validation.id)
        with sessions() as session:
            activated = session.get(ActiveRecipeRow, "acceptance") is not None
        if not activated:
            with sessions.begin() as session:
                approval = OperatorService(
                    session,
                    runtime.config,
                    datetime.now(UTC),
                ).act(imported_id, "approve")
            assert runtime.tick(approval.id)
        with sessions.begin() as session:
            svc = OperatorService(
                session,
                runtime.config,
                datetime.now(UTC),
            )
            svc.configure_source("acceptance", SourceWrite(enabled=True, manual_only=True))
            retry = svc.collect("acceptance", ExecutionLimits(max_tenders=3))
        assert runtime.tick(retry.id)
        with sessions() as session:
            row = session.get(OperationRow, retry.id)
            assert row and row.status == "completed" and row.result["records_processed"] == 3
            for model in (TenderRow, TenderVersionRow):
                assert session.scalar(select(func.count()).select_from(model)) == 3
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        ready.close()
        ready.join_thread()


def _interrupted_matching_worker(database_url: str, config_json: str, ready: Any) -> None:
    validate_test_database(database_url)
    engine = build_engine(SecretStr(database_url))
    sessions = session_factory(engine)
    clock = SystemClock()
    config = Configuration.model_validate_json(config_json)

    def provider(_: ModelRequest) -> ModelResponse:
        _pause(ready, "provider")
        raise AssertionError("Fixture provider returned after interruption point")

    registry: Registry[MatcherFactory] = Registry()
    registry.register(
        "intelligent",
        lambda settings: IntelligentMatcher(
            settings, clock, lambda _: FunctionModelClient(provider)
        ),
    )
    try:
        BatchMatchingWorker(
            TransactionalMatchingStore(sessions, config), registry, clock
        ).run_batch(1)
        ready.put("unexpected_completion")
    finally:
        engine.dispose()


@pytest.mark.browser
def test_sigkill_during_semantic_provider_reclaims_lease_without_duplicate_result(
    migrated_engine: Engine,
    crash_application: tuple[AcceptanceSite, OperatorRuntime, Path],
) -> None:
    site, runtime, _ = crash_application
    # This test interrupts the provider, so source responses must not first block
    # matching on attachment downloads disallowed by production loopback guards.
    site.omit_optional = True
    imported_id = _import_and_validate(site, runtime)
    with runtime.sessions.begin() as session:
        svc = OperatorService(session, runtime.config, datetime.now(UTC))
        approval = svc.act(imported_id, "approve")
    assert runtime.tick(approval.id)
    with runtime.sessions.begin() as session:
        svc = OperatorService(session, runtime.config, datetime.now(UTC))
        svc.configure_source("acceptance", SourceWrite(enabled=True, manual_only=True))
        collection = svc.collect("acceptance", ExecutionLimits(max_tenders=3))
    assert runtime.tick(collection.id)
    with runtime.sessions() as session:
        tenders = SqlTenderRepository(session).for_source("acceptance")
        assert len(tenders) == 3
        assert all(
            not tender.document_urls and not tender.documents_incomplete for tender in tenders
        )
    config = Configuration.model_validate(
        {
            "runtime": {"ai_semantic_enabled": True},
            "global": {
                "campaign": {
                    "batch_size": 1,
                    "matcher_adapter": "intelligent",
                    "matcher_version": "1",
                    "model": {
                        "provider": "fixture",
                        "model": "owned-fixture",
                        "timeout_seconds": 10,
                    },
                    "timeout_seconds": 10,
                    "lease_seconds": 30,
                }
            },
        }
    )
    with runtime.sessions.begin() as session:
        planner = matching_planner(session, config)
        campaign = CampaignService(SqlCampaignRepository(session), planner, SystemClock()).create(
            uuid4(), uuid4(), CampaignCriteria(name="Networking", description="Network routers")
        )
        planner.for_campaign(campaign, datetime.now(UTC))
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    process = context.Process(
        target=_interrupted_matching_worker,
        args=(
            migrated_engine.url.render_as_string(hide_password=False),
            config.model_dump_json(),
            ready,
        ),
    )
    process.start()
    try:
        assert ready.get(timeout=20) == "provider"
        process.kill()
        process.join(timeout=10)
        assert not process.is_alive()
        with runtime.sessions() as session:
            job = session.scalar(select(MatchJobRow).where(MatchJobRow.status == "running"))
            assert job and job.lease_expires_at and job.lease_token and job.attempts == 1
            job_id, first_token = job.id, job.lease_token
            resumed_at = job.lease_expires_at + timedelta(seconds=1)
            assert session.scalar(select(func.count()).select_from(MatchResultRow)) == 0
        clock = FakeClock(resumed_at)
        calls: list[ModelRequest] = []

        def provider(request: ModelRequest) -> ModelResponse:
            calls.append(request)
            return ModelResponse(
                {
                    "result": "match",
                    "score": 0.97,
                    "confidence": 0.95,
                    "reasons": ["Network router is present in supplied products"],
                    "uncertainties": [],
                    "evidence": [{"field": "product_items.0.name", "quote": "Network router"}],
                },
                "owned-fixture",
            )

        registry: Registry[MatcherFactory] = Registry()
        registry.register(
            "intelligent",
            lambda settings: IntelligentMatcher(
                settings, clock, lambda _: FunctionModelClient(provider)
            ),
        )
        worker = BatchMatchingWorker(
            TransactionalMatchingStore(runtime.sessions, config, job_ids=(job_id,)), registry, clock
        )
        report = worker.run_batch(1)
        assert report.completed == 1 and report.failed == report.lost_leases == 0
        assert len(calls) == 1
        assert worker.run_batch(1).claimed == 0
        with runtime.sessions() as session:
            stored = session.get(MatchJobRow, job_id)
            assert stored and stored.status == "done" and stored.attempts == 2
            results = list(session.scalars(select(MatchResultRow)))
            assert len(results) == 1 and results[0].job_id == job_id
            assert results[0].completion_token != first_token
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        ready.close()
        ready.join_thread()


@pytest.mark.browser
def test_database_disconnect_during_publication_rolls_back_and_reports_actual_stage(
    migrated_engine: Engine,
    crash_application: tuple[AcceptanceSite, OperatorRuntime, Path],
) -> None:
    site, runtime, _ = crash_application
    imported_id = _import_and_validate(site, runtime)
    with runtime.sessions.begin() as session:
        svc = OperatorService(session, runtime.config, datetime.now(UTC))
        approval = svc.act(imported_id, "approve")
    assert runtime.tick(approval.id)
    with runtime.sessions.begin() as session:
        svc = OperatorService(session, runtime.config, datetime.now(UTC))
        svc.configure_source("acceptance", SourceWrite(enabled=True, manual_only=True))
        operation = svc.collect("acceptance", ExecutionLimits(max_tenders=3))
    disconnected = False

    def disconnect_after_observation(
        connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _many: bool,
    ) -> None:
        nonlocal disconnected
        if disconnected or not statement.lstrip().startswith("INSERT INTO tender_observations"):
            return
        disconnected = True
        # The target PID comes from the test's own publication connection. No
        # other backend or database is selected for termination.
        pid = connection.scalar(text("select pg_backend_pid()"))
        with migrated_engine.connect() as controller:
            assert controller.scalar(text("select pg_terminate_backend(:pid)"), {"pid": pid})

    event.listen(migrated_engine, "after_cursor_execute", disconnect_after_observation)
    try:
        assert runtime.tick(operation.id)
    finally:
        event.remove(migrated_engine, "after_cursor_execute", disconnect_after_observation)
    assert disconnected
    with runtime.sessions() as session:
        row = session.get(OperationRow, operation.id)
        assert row and row.status == "failed" and row.error_code == "database_unavailable"
        assert row.result["stage"] == "database_publication"
        assert row.result["evidence_references"]
        for model in (TenderRow, TenderVersionRow, TenderObservationRow, SourceRunRow):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    # A new explicit operation succeeds after reconnecting, without duplicate writes.
    with runtime.sessions.begin() as session:
        retry = OperatorService(session, runtime.config, datetime.now(UTC)).collect(
            "acceptance", ExecutionLimits(max_tenders=3)
        )
    assert runtime.tick(retry.id)
    with runtime.sessions() as session:
        row = session.get(OperationRow, retry.id)
        assert row and row.status == "completed" and row.result["records_processed"] == 3
        for model in (TenderRow, TenderVersionRow, TenderObservationRow):
            assert session.scalar(select(func.count()).select_from(model)) == 3


@pytest.mark.parametrize("kind", ["onboarding", "repair", "repair_approval"])
def test_retired_queued_work_fails_without_browser_or_model_access(
    migrated_engine: Engine,
    kind: str,
) -> None:
    sessions = session_factory(migrated_engine)
    config = load_configuration(environ={})

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("retired operation reached external work")

    runtime = OperatorRuntime(sessions, config, browsers=forbidden, models=forbidden)
    try:
        with sessions.begin() as session:
            operation = OperatorService(session, config, runtime.clock.now()).enqueue(
                kind,
                "historical-source",
                {"url": "https://unused.invalid"},
            )
        assert runtime.tick(operation.id)
        with sessions() as session:
            row = session.get(OperationRow, operation.id)
            assert row is not None and row.status == "failed"
            assert row.result["message"] == "unsupported operation"
            assert session.scalar(select(func.count()).select_from(SourceRunRow)) == 0
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 0
    finally:
        runtime.close()
