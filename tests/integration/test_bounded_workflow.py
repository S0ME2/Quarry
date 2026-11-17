"""Bounded onboarding uses real HTTP/executor/publication and isolated PostgreSQL."""

import json
import multiprocessing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue, SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.documents import DocumentProcessor
from tender_system.core.configuration import Configuration, load_configuration
from tender_system.core.domain import ExecutionLimits, SourceRecipe
from tender_system.core.ports import ModelRequest, ModelResponse
from tender_system.operator import manual_cli as cli
from tender_system.operator.operations import OperatorService, operation_lock, recover_operations
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    OperationRow,
    SqlTenderRepository,
    TransactionalRunStore,
    build_engine,
    session_factory,
)
from tender_system.testing.fakes import FakeBrowserAdapter, FakePage, FunctionModelClient
from tender_system.testing.local_server import local_http_server, server_url
from tender_system.testing.operator import fixture_recipe, onboarding_site

pytestmark = pytest.mark.database


def interrupted_worker(url: str, operation_id: UUID, ready: Any) -> None:
    """Spawned child owns its own database connections, never inherits the test pool."""
    engine = build_engine(SecretStr(url))

    class BlockingRuntime(OperatorRuntime):
        def execute(
            self,
            key: UUID,
            kind: str,
            source_id: str | None,
            payload: dict[str, JsonValue],
            config: Configuration,
        ) -> tuple[dict[str, JsonValue], str]:
            ready.put(str(key))
            Event().wait(30)
            return {}, "completed"

    runtime = BlockingRuntime(
        session_factory(engine),
        load_configuration(environ={}, validate_models=False),
        evidence=MemoryEvidenceStore(),
    )
    try:
        runtime.tick(operation_id)
    finally:
        runtime.close()
        engine.dispose()


def test_sigkill_worker_is_recoverable_without_reexecution(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    config = load_configuration(environ={}, validate_models=False)

    with sessions.begin() as session:
        operation = OperatorService(session, config, datetime.now(UTC)).enqueue(
            "recipe_validation", "fixture", {}
        )
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    process = context.Process(
        target=interrupted_worker,
        args=(
            migrated_engine.url.render_as_string(hide_password=False),
            operation.id,
            ready,
        ),
    )
    process.start()
    try:
        assert ready.get(timeout=15) == str(operation.id)
        process.kill()
        process.join(timeout=10)
        assert not process.is_alive()
        with sessions.begin() as session:
            assert recover_operations(session, datetime.now(UTC)) == 1
            row = session.get(OperationRow, operation.id)
            assert row and row.status == "failed" and row.error_code == "worker_interrupted"
        with sessions.begin() as session:
            assert recover_operations(session, datetime.now(UTC)) == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        ready.close()
        ready.join_thread()


def test_100_record_pilot_preserves_products_documents_and_unseen_inventory(
    migrated_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []
    original_init = DocumentProcessor.__init__

    def document_init(self: DocumentProcessor, sessions: sessionmaker[Session]) -> None:
        # This fixture tests collection budgets and matching, not attachment transport.
        # Document HTTP/OCR and failure isolation have their own connected integration test.
        original_init(
            self,
            sessions,
            fetch=lambda *args: (200, b"Network router", {"content-type": "text/plain"}),
        )

    monkeypatch.setattr(DocumentProcessor, "__init__", document_init)
    product_revision = 1
    mass_change = False
    broken = False
    model_requests: list[ModelRequest] = []
    monkeypatch.setenv("GROQ_API_KEY", "fixture-only-unused-by-network")

    def semantic(request: ModelRequest) -> ModelResponse:
        model_requests.append(request)
        return ModelResponse(
            {
                "decision": "match",
                "confidence": 0.95,
                "explanation": "Requested network equipment is explicitly present",
                "evidence": [{"field": "product_items.0.name", "quote": "Network router"}],
            },
            "deterministic-fixture",
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            requests.append(self.path)
            origin = server_url(self.server)  # type: ignore[arg-type]
            if self.path == "/api":
                payload: Any = [{"url": f"{origin}/detail/{i}"} for i in range(120)]
            else:
                identifier = self.path.rsplit("/", 1)[-1]
                payload = {
                    "external_tender_id": identifier,
                    "title": f"Office supplies {identifier}",
                    "source_url": f"{origin}{self.path}",
                    "currency": "USD",
                    "product_items": [
                        {
                            "id": "router-1",
                            "name": "Network router",
                            "quantity": product_revision if mass_change or identifier == "0" else 1,
                        }
                    ],
                    "document_urls": [f"{origin}/documents/{identifier}.pdf"],
                }
            if broken:
                payload = {"unexpected": "website structure changed"}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    sessions = session_factory(migrated_engine)
    from tender_system.operator.matching_budget import MatchingBudget
    from tender_system.persistence.storage import OperatorConfigRow

    with sessions.begin() as session:
        session.add(
            OperatorConfigRow(
                key="matching_budget",
                value=MatchingBudget(
                    global_limit_usd=10,
                    provider="groq",
                    model="qwen/qwen3.8-27b",
                    input_usd_per_million=1,
                    output_usd_per_million=1,
                ).model_dump(mode="json"),
                updated_at=datetime.now(UTC),
            )
        )
    config = load_configuration(environ={}, validate_models=False)

    with local_http_server(Handler) as server:
        url = server_url(server)
        recipe = SourceRecipe.model_validate(
            {
                "source_id": "generic-api-pilot",
                "version": "1",
                "expected_count": 120,
                "completion_signal": {"kind": "total_reconciled", "value": 120},
                "steps": [
                    {
                        "type": "http_request",
                        "parameters": {
                            "url": url + "/api",
                            "output": "listing",
                            "inventory_page": True,
                        },
                    },
                    {
                        "type": "extract",
                        "parameters": {
                            "input": {"$ref": "listing.body"},
                            "format": "json",
                            "output": "rows",
                        },
                    },
                    {
                        "type": "loop",
                        "parameters": {"items": {"$ref": "rows"}, "max_iterations": 200},
                        "steps": [
                            {
                                "type": "http_request",
                                "parameters": {"url": {"$ref": "item.url"}, "output": "detail"},
                            },
                            {
                                "type": "extract",
                                "parameters": {
                                    "input": {"$ref": "detail.body"},
                                    "format": "json",
                                    "emit": True,
                                },
                            },
                        ],
                    },
                ],
            }
        )
        original = recipe.model_dump_json()
        path = tmp_path / "recipe.json"
        path.write_text(original)
        runtime = OperatorRuntime(
            sessions,
            cli.manual_config(config),
            evidence=MemoryEvidenceStore(),
            browsers=lambda: FakeBrowserAdapter(
                {f"{url}/detail/{i}": FakePage(f"<h1>Office supplies {i}</h1>") for i in range(120)}
            ),
        )

        def run(words: list[str]) -> tuple[int, Any]:
            code = cli.dispatch(
                cli.parser().parse_args(words),
                sessions,
                config,
                runtime=runtime,
                providers=lambda _: FunctionModelClient(semantic),
                qwen_confirmed=True,
            )
            output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            return code, output[-1]

        try:
            _, imported = run(["recipe-import", str(path)])
            code, validation = run(
                ["recipe-validate", imported["id"], "--max-tenders", "100", "--max-requests", "101"]
            )
            assert code == 0, validation
            assert len(requests) == validation["result"]["http_requests"] == 101
            code, approved = run(["approve", imported["id"]])
            assert code == 0, approved
            assert approved["result"]["http_requests"] == 4
            assert approved["result"]["records_processed"] == 3
            assert approved["result"]["validation_http_requests"] == 101
            assert len(requests) == 105
            run(["source-enable", recipe.source_id])
            for _ in range(2):
                code, collected = run(
                    ["collect", recipe.source_id, "--max-tenders", "100", "--max-requests", "101"]
                )
                assert code == 0, collected
                assert collected["result"]["records_processed"] == 100
            assert len(requests) == 307 and path.read_text() == original
            with sessions() as session:
                repository = SqlTenderRepository(session)
                tenders = repository.for_source(recipe.source_id)
                assert len(tenders) == 100
                assert len({t.external_tender_id for t in tenders}) == 100
                for tender in tenders:
                    assert tender.published_at is None and tender.document_urls
                    assert tender.raw_data["product_items"]
                    assert len(repository.history(tender.internal_id)) == 1
                tender_id = str(next(t.internal_id for t in tenders if t.external_tender_id == "0"))
            # Smaller subsequent samples must leave previously observed records present.
            code, subset = run(["collect", recipe.source_id, "--max-tenders", "1"])
            assert code == 0 and not subset["result"]["health"]["absence_evidence_allowed"]
            with sessions() as session:
                assert all(
                    t.presence.missing_successful_runs == 0
                    for t in SqlTenderRepository(session).for_source(recipe.source_id)
                )
            campaign_file = tmp_path / "campaign.json"
            campaign_file.write_text(
                json.dumps(
                    {
                        "criteria": {
                            "name": "Nested router",
                            "ai_budget_usd": 10,
                            "include_keywords": ["router"],
                        }
                    }
                )
            )
            _, campaign = run(["campaign-create", str(campaign_file)])
            code, result = run(["rules", "--pair", f"{tender_id}:{campaign['id']}"])
            assert code == 0 and result[0]["outcome"]["result"] == "match"
            assert result[0]["outcome"]["model_calls"] == []
            assert model_requests == []
            code, result = run(["qwen", "--pair", f"{tender_id}:{campaign['id']}"])
            assert code == 0 and result[0]["outcome"]["result"] == "match", result
            assert len(model_requests) == 1
            original_version = result[0]["tender_version_id"]
            _, persisted = run(["results", campaign["id"]])
            assert any(r["outcome"]["model_calls"] for r in persisted)

            _, original_jobs = run(["jobs", campaign["id"]])
            # A mass change is suspicious and must fail before any publication.
            product_revision = 2
            mass_change = True
            code, suspicious = run(["collect", recipe.source_id, "--max-tenders", "100"])
            assert code == 1 and suspicious["result"]["publication"] == "rejected"
            assert any(
                r["code"] == "suspicious_mass_field_change"
                for r in suspicious["result"]["health"]["reasons"]
            )
            mass_change = False
            # One nested product change creates one version.
            # Disabled automatic matching queues nothing.
            code, changed = run(["collect", recipe.source_id, "--max-tenders", "100"])
            assert code == 0, json.dumps(changed)
            with sessions() as session:
                repository = SqlTenderRepository(session)
                tenders = repository.for_source(recipe.source_id)
                assert len(tenders) == 100
                updated = next(t for t in tenders if t.external_tender_id == "0")
                assert len(repository.history(updated.internal_id)) == 2
                assert updated.product_items and updated.product_items[0]["quantity"] == 2
                assert set(repository.history(updated.internal_id)[-1].changed_fields) == {
                    "product_items"
                }
                assert sum(len(repository.history(t.internal_id)) for t in tenders) == 101
            _, jobs = run(["jobs", campaign["id"]])
            assert len(jobs) == len(original_jobs)
            assert sum(j["status"] == "pending" for j in jobs) == sum(
                j["status"] == "pending" for j in original_jobs
            )
            code, rematched = run(["qwen", "--pair", f"{tender_id}:{campaign['id']}"])
            assert code == 0 and len(model_requests) == 2, rematched
            assert rematched[0]["tender_version_id"] != original_version
            code, repeated_match = run(["qwen", "--pair", f"{tender_id}:{campaign['id']}"])
            assert code == 0 and repeated_match[0]["id"] == rematched[0]["id"]
            assert len(model_requests) == 2
            _, persisted = run(["results", campaign["id"]])
            assert len(persisted) == 3  # rules, old semantic, new semantic: audit history retained

            # Hard exclusions remain authoritative even with a provider eager to match.
            campaign_file.write_text(
                json.dumps(
                    {
                        "criteria": {
                            "name": "Exclude network",
                            "exclude_keywords": ["router"],
                        }
                    }
                )
            )
            _, excluded = run(["campaign-create", str(campaign_file)])
            code, rejected = run(["qwen", "--pair", f"{tender_id}:{excluded['id']}"])
            assert code == 0 and rejected[0]["outcome"]["result"] == "no_match"
            assert len(model_requests) == 2

            # A broken API must preserve accepted records, versions, products and presence.
            broken = True
            code, failed = run(["collect", recipe.source_id, "--max-tenders", "100"])
            assert code == 1 and failed["status"] == "failed", failed
            with sessions() as session:
                repository = SqlTenderRepository(session)
                tenders = repository.for_source(recipe.source_id)
                assert len(tenders) == 100
                assert sum(len(repository.history(t.internal_id)) for t in tenders) == 101
                assert all(t.presence.missing_successful_runs == 0 for t in tenders)
            broken = False
            code, recovered = run(["collect", recipe.source_id, "--max-tenders", "100"])
            assert code == 0, recovered
            with sessions() as session:
                repository = SqlTenderRepository(session)
                assert (
                    sum(
                        len(repository.history(t.internal_id))
                        for t in repository.for_source(recipe.source_id)
                    )
                    == 101
                )
            assert len(model_requests) == 2
        finally:
            runtime.close()


def test_bounded_import_approval_collection_idempotency_and_matching(
    migrated_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sessions = session_factory(migrated_engine)
    config = load_configuration(environ={}, validate_models=False)

    requests: list[str] = []
    with onboarding_site(requests) as url:
        recipe = fixture_recipe("bounded-fixture", url)
        original = recipe.model_dump_json()
        path = tmp_path / "unchanged-recipe.json"
        path.write_text(original)
        runtime = OperatorRuntime(
            sessions,
            cli.manual_config(config),
            evidence=MemoryEvidenceStore(),
            browsers=lambda: FakeBrowserAdapter(
                {f"{url}/detail/{i}": FakePage(f"<h1>Network equipment {i}</h1>") for i in range(2)}
            ),
        )

        def run(words: list[str]) -> tuple[int, Any]:
            code = cli.dispatch(cli.parser().parse_args(words), sessions, config, runtime=runtime)
            output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            return code, output[-1]

        try:
            _, imported = run(["recipe-import", str(path)])
            key = imported["id"]
            code, validation = run(
                ["recipe-validate", key, "--max-tenders", "1", "--max-requests", "2"]
            )
            assert code == 0, validation
            report = validation["result"]
            assert report["validation_passed"] and report["replay_passed"]
            assert report["observations_collected"] == report["http_requests"] == 1
            assert report["health"]["inventory_mode"] == "sampled"
            assert report["health"]["completeness"] == "incomplete"
            assert report["health"]["freshness"]["status"] == "unknown"
            assert report["health"]["freshness"]["timestamp"] is None
            assert not report["health"]["absence_evidence_allowed"]
            assert requests == ["/api"]  # replay made zero live requests
            code, approved = run(["approve", key])
            assert code == 0, approved
            assert requests == ["/api", "/api"]  # bounded fresh approval check only
            run(["source-enable", recipe.source_id])
            code, denied = run(["collect", recipe.source_id])
            assert code == 1 and denied["error_code"] is not None
            assert requests == ["/api", "/api"]
            for _ in range(2):
                code, collected = run(["collect", recipe.source_id, "--max-tenders", "1"])
                assert code == 0, collected
                assert collected["result"]["records_processed"] == 1
                with sessions() as session:
                    receipt = TransactionalRunStore(sessions).receipt(UUID(collected["id"]))
                    assert receipt and receipt.status == "accepted"
                    assert receipt.missing_incremented == 0 and not receipt.checkpoint_advanced
                    repository = SqlTenderRepository(session)
                    stored = repository.for_source(recipe.source_id)
                    assert len(stored) == 1 and stored[0].published_at is None
                    assert len(repository.history(stored[0].internal_id)) == 1
                    tender_id = str(stored[0].internal_id)
            assert path.read_text() == original
            _, catalog = run(["tenders", "--source", recipe.source_id, "--limit", "100"])
            assert len(catalog["items"]) == 1
            campaign_path = tmp_path / "campaign.json"
            campaign_path.write_text(
                json.dumps(
                    {
                        "criteria": {
                            "name": "Pilot campaign",
                            "include_keywords": ["network"],
                            "currencies": ["USD"],
                        }
                    }
                )
            )
            code, campaign = run(["campaign-create", str(campaign_path)])
            assert code == 0
            code, matched = run(["rules", "--pair", f"{tender_id}:{campaign['id']}"])
            assert code == 0 and matched[0]["outcome"]["model_calls"] == []
            _, results = run(["results", campaign["id"]])
            assert results and results[0]["tender_id"] == tender_id
        finally:
            runtime.close()


def test_worker_ownership_recovery_and_interruption(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = session_factory(migrated_engine)
    config = load_configuration(environ={}, validate_models=False)

    now = datetime.now(UTC)
    key = uuid4()
    with sessions.begin() as session:
        session.add(
            OperationRow(
                id=key,
                kind="recipe_validation",
                source_id="fixture",
                status="running",
                payload={},
                result={},
                created_at=now,
                started_at=now,
            )
        )
    with sessions.begin() as ownership:
        assert operation_lock(ownership, key)
        with sessions.begin() as recovery:
            assert recover_operations(recovery, now) == 0
    with sessions.begin() as recovery:
        assert recover_operations(recovery, now) == 1
    with sessions.begin() as recovery:
        assert recover_operations(recovery, now) == 0
        row = recovery.get(OperationRow, key)
        assert row and row.error_code == "worker_interrupted"
        queued = OperatorService(recovery, config, now).enqueue("recipe_validation", "fixture", {})
    runtime = OperatorRuntime(sessions, config, evidence=MemoryEvidenceStore())

    def interrupt(*_: object) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "execute", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            runtime.tick(queued.id)
        with sessions() as session:
            row = session.get(OperationRow, queued.id)
            assert row and row.status == "failed" and row.error_code == "worker_interrupted"
    finally:
        runtime.close()


def test_failed_revalidation_revokes_previous_approval(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = session_factory(migrated_engine)
    config = load_configuration(environ={}, validate_models=False)

    now = datetime.now(UTC)
    with sessions.begin() as session:
        service = OperatorService(session, config, now)
        imported = service.import_recipe(
            fixture_recipe("fixture", "https://fixture.test"), "https://fixture.test"
        )
        row = session.get(OperationRow, imported.id)
        assert row
        row.result = {
            **row.result,
            "validation_passed": True,
            "validation_evidence_reference": "old-evidence",
        }
        operation = service.enqueue(
            "recipe_validation",
            "fixture",
            {
                "operation_id": str(imported.id),
                "limits": ExecutionLimits(max_tenders=1).model_dump(mode="json"),
            },
        )
    runtime = OperatorRuntime(sessions, config, evidence=MemoryEvidenceStore())

    def fail(*_: object) -> Any:
        raise RuntimeError("private-adapter-input")

    monkeypatch.setattr(runtime, "test_proposal", fail)
    try:
        runtime.tick(operation.id)
        with sessions() as session:
            row = session.get(OperationRow, imported.id)
            assert row and row.status == "failed" and row.result["validation_passed"] is False
            assert "validation_evidence_reference" not in row.result
            failure = session.get(OperationRow, operation.id)
            assert failure and "private-adapter-input" not in json.dumps(failure.result)
    finally:
        runtime.close()


def test_reimport_replaces_pending_recipe_and_revokes_validation(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    config = load_configuration(environ={}, validate_models=False)

    with sessions.begin() as session:
        service = OperatorService(session, config, datetime.now(UTC))
        recipe = fixture_recipe("fixture", "https://fixture.test")
        imported = service.import_recipe(recipe, "https://fixture.test")
        row = session.get(OperationRow, imported.id)
        assert row
        row.result = {
            **row.result,
            "validation_passed": True,
            "validation_evidence_reference": "old",
        }
        changed = recipe.model_copy(update={"version": "2"})
        replaced = service.replace_recipe(imported.id, changed, "https://fixture.test")
        assert replaced.id == imported.id and not replaced.result["validation_passed"]
        assert "validation_evidence_reference" not in replaced.result
        assert service.generated_recipe(imported.id) == changed
        with pytest.raises(ValueError, match="validated"):
            service.act(imported.id, "approve")


@pytest.mark.parametrize("mismatch", [False, True])
def test_replacement_sample_verifier_cannot_bypass_validation_requirements(
    migrated_engine: Engine, mismatch: bool
) -> None:
    from tender_system.core.configuration import SourceSettings
    from tender_system.core.domain import (
        CollectionRun,
        FixResult,
        Reason,
        SampleVerification,
        SourceRecord,
    )

    calls: list[CollectionRun] = []

    class RejectingVerifier:
        def verify(
            self, run: CollectionRun, source: SourceRecord, settings: SourceSettings
        ) -> SampleVerification:
            calls.append(run)
            return SampleVerification(
                checked=len(run.candidates) if mismatch else 0,
                mismatches=(Reason(code="fixture_mismatch", detail="Independent mismatch"),)
                if mismatch
                else (),
            )

    requests: list[str] = []
    with onboarding_site(requests) as url:
        recipe = fixture_recipe("replacement-verifier", url)
        config = load_configuration(environ={}, validate_models=False)
        runtime = OperatorRuntime(
            session_factory(migrated_engine),
            cli.manual_config(config),
            evidence=MemoryEvidenceStore(),
            sample_verifier=RejectingVerifier(),
        )
        try:
            report = runtime.test_proposal(
                uuid4(),
                recipe.source_id,
                FixResult(
                    status="fixed",
                    confidence=1,
                    explanation="Controlled verifier fixture",
                    updated_recipe=recipe,
                ),
                config.for_source(recipe.source_id),
                ExecutionLimits(max_tenders=2),
            )
            assert len(calls) == 1 and requests == ["/api"]
            assert report["replay_passed"] is True
            assert report["validation_passed"] is False
            assert report["error_code"] == "independent_verification_failed"
        finally:
            runtime.close()
