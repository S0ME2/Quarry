"""Approval interprets the validated scope, including delayed recipe-local totals."""

import json
import multiprocessing
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from threading import Event, current_thread
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.core.configuration import load_configuration
from tender_system.core.domain import SourceRecipe
from tender_system.operator import manual_cli as cli
from tender_system.operator.operations import (
    OperationRestriction,
    OperatorService,
    recover_operations,
)
from tender_system.operator.operator_contracts import SourceWrite
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    OperationRow,
    SourceRow,
    build_engine,
    session_factory,
)
from tender_system.testing.fakes import FakeBrowserAdapter, FakePage
from tender_system.testing.local_server import local_http_server, server_url
from tender_system.testing.operator import fixture_recipe, onboarding_site

pytestmark = pytest.mark.database


def paused_activation_worker(database_url: str, operation_id: UUID, ready: Any) -> None:
    """Pause after actual ownership claim, before an obsolete duplicate can execute."""
    engine = build_engine(SecretStr(database_url))

    class PausedRuntime(OperatorRuntime):
        def source_settings(self, *args: Any, **kwargs: Any) -> Any:
            ready.put(str(operation_id))
            Event().wait(30)
            return super().source_settings(*args, **kwargs)

    runtime = PausedRuntime(
        session_factory(engine),
        load_configuration(environ={}, validate_models=False),
        evidence=MemoryEvidenceStore(),
    )
    try:
        runtime.tick(operation_id)
    finally:
        runtime.close()
        engine.dispose()


def delayed_count_recipe(source: str, url: str) -> SourceRecipe:
    """Count becomes available after emission, as in the historical pilot."""
    return SourceRecipe.model_validate(
        {
            "source_id": source,
            "version": "1",
            "coverage": "incremental",
            "expected_count": {"$ref": "page_count"},
            "steps": [
                {"type": "http_request", "parameters": {"url": url + "/api"}},
                {
                    "type": "extract",
                    "parameters": {
                        "format": "json",
                        "input": {"$ref": "response.body"},
                        "selector": "items",
                        "output": "rows",
                        "emit": True,
                    },
                },
                {
                    "type": "transform",
                    "parameters": {
                        "name": "length",
                        "input": {"$ref": "rows"},
                        "output": "page_count",
                    },
                },
            ],
        }
    )


@pytest.mark.parametrize("historical", [False, True], ids=["minimal", "captured-pilot-100"])
def test_approval_does_not_compare_unresolved_sample_total_to_incremental_count(
    migrated_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    historical: bool,
) -> None:
    requests: list[str] = []
    fixture = json.loads(Path("tests/fixtures/uztender/captured-sample.json").read_text())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            requests.append(self.path)
            url = server_url(self.server)  # type: ignore[arg-type]
            payload: dict[str, Any]
            if historical:
                path = urlsplit(self.path).path
                if path == "/api/catalog":
                    rows = []
                    for i in range(100):
                        row = deepcopy(fixture["listing"]["data"][0])
                        row.update(id=str(i), slug="fixture", title=f"Tender {i}")
                        rows.append(row)
                    payload = {
                        "data": rows,
                        "meta": {"pagination": {"total": 3087, "offset": 0, "limit": 100}},
                    }
                else:
                    identifier = path.rsplit("/", 1)[-1]
                    detail = deepcopy(fixture["detail"]["data"])
                    detail.update(id=identifier, slug="fixture", title=f"Tender {identifier}")
                    payload = {"data": detail}
            else:
                payload = {
                    "items": [
                        {
                            "external_tender_id": str(i),
                            "title": f"Tender {i}",
                            "source_url": f"{url}/detail/{i}",
                        }
                        for i in range(5)
                    ],
                    "total": 3000,
                }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    sessions = session_factory(migrated_engine)
    config = load_configuration(environ={}, validate_models=False)
    with local_http_server(Handler) as server:
        url = server_url(server)
        if historical:
            # Rebase only external transport origins. Preserve the actual pilot's
            # coverage, completion, iteration caps and late page_count expression.
            saved = Path("tests/fixtures/uztender/pilot-100.json").read_text()
            saved = saved.replace("https://api.uztender.com", url)
            saved = saved.replace("https://uztender.com", url)
            recipe = SourceRecipe.model_validate_json(saved)
        else:
            recipe = delayed_count_recipe("delayed-incremental", url)
        path = tmp_path / "recipe.json"
        path.write_text(recipe.model_dump_json())
        runtime = OperatorRuntime(
            sessions,
            cli.manual_config(config),
            evidence=MemoryEvidenceStore(),
            browsers=lambda: FakeBrowserAdapter(
                {
                    f"{url}/tender/fixture-{i}" if historical else f"{url}/detail/{i}": FakePage(
                        f"<h1>Tender {i}</h1>"
                    )
                    for i in range(100 if historical else 5)
                }
            ),
        )

        def run(words: list[str]) -> tuple[int, Any]:
            code = cli.dispatch(
                cli.parser().parse_args(words),
                sessions,
                config,
                runtime=runtime,
            )
            output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            return code, output[-1]

        try:
            code, imported = run(["recipe-import", str(path)])
            assert code == 0
            code, validated = run(["recipe-validate", imported["id"]])
            assert code == 0, validated
            assert validated["result"]["expected_count"] == (100 if historical else 5)
            assert validated["result"]["validation_passed"] is True
            code, approved = run(["approve", imported["id"]])
            assert code == 0, approved
            assert approved["status"] == "activated"
            assert approved["result"]["approval_fresh_check"] == "passed"
            if historical:
                assert len(requests) == 105  # 101 validation + 4 fresh; replay is offline.
                assert requests.count("/api/catalog?limit=100&offset=0") == 2
            else:
                assert requests == ["/api", "/api"]
            with sessions() as session:
                assert session.get(SourceRow, recipe.source_id) is not None
                assert session.get(ActiveRecipeRow, recipe.source_id) is not None
        finally:
            runtime.close()


@pytest.mark.parametrize("first_writer", ["approval", "configuration"])
def test_scope_upgrade_serializes_with_source_configuration(
    migrated_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    first_writer: str,
) -> None:
    sessions = session_factory(migrated_engine)
    config = load_configuration(environ={}, validate_models=False)

    with onboarding_site() as url:
        recipe = fixture_recipe("scope-lock", url)
        path = tmp_path / "recipe.json"
        path.write_text(recipe.model_dump_json())
        runtime = OperatorRuntime(
            sessions,
            cli.manual_config(config),
            evidence=MemoryEvidenceStore(),
            browsers=lambda: FakeBrowserAdapter(
                {f"{url}/detail/{i}": FakePage(f"<h1>Network equipment {i}</h1>") for i in range(2)}
            ),
        )

        def run(words: list[str]) -> Any:
            code = cli.dispatch(
                cli.parser().parse_args(words),
                sessions,
                config,
                runtime=runtime,
            )
            rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            assert code == 0, rows
            return rows[-1]

        try:
            imported = run(["recipe-import", str(path)])
            run(["recipe-validate", imported["id"], "--max-tenders", "1"])
            run(["approve", imported["id"]])
            run(["source-enable", recipe.source_id])
            upgrade = run(["recipe-import", str(path), "--revalidate", recipe.source_id])
            run(["recipe-validate", upgrade["id"]])
            with sessions.begin() as session:
                svc = OperatorService(session, config, datetime.now(UTC))
                activation = svc.act(UUID(upgrade["id"]), "approve")
                with pytest.raises(OperationRestriction, match="already queued or running"):
                    svc.act(UUID(upgrade["id"]), "approve")
            entered = Event()
            release = Event()
            original = runtime.source_settings
            calls = 0

            def settings(*args: Any, **kwargs: Any) -> Any:
                nonlocal calls
                if current_thread().name.startswith("approval"):
                    calls += 1
                    if calls == 2 and first_writer == "approval":
                        # Second source_settings call is inside activation's row lock.
                        entered.set()
                        assert release.wait(5)
                return original(*args, **kwargs)

            monkeypatch.setattr(runtime, "source_settings", settings)

            def configure() -> None:
                with sessions.begin() as session:
                    svc = OperatorService(session, config, datetime.now(UTC))
                    svc.configure_source(recipe.source_id, SourceWrite(enabled=False))
                    if first_writer == "configuration":
                        entered.set()
                        assert release.wait(5)

            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="approval") as approval_pool:
                with ThreadPoolExecutor(max_workers=1) as config_pool:
                    blocked: Future[Any]
                    if first_writer == "approval":
                        approval = approval_pool.submit(runtime.tick, activation.id)
                        assert entered.wait(5)
                        configured = config_pool.submit(configure)
                        blocked = configured
                    else:
                        configured = config_pool.submit(configure)
                        assert entered.wait(5)
                        approval = approval_pool.submit(runtime.tick, activation.id)
                        blocked = approval
                    try:
                        with pytest.raises(FutureTimeout):
                            blocked.result(timeout=0.2)
                    finally:
                        release.set()
                    assert approval.result(timeout=10)
                    configured.result(timeout=10)
            with sessions() as session:
                svc = OperatorService(session, config, datetime.now(UTC))
                assert svc.control(recipe.source_id)["enabled"] is False
                approved = svc.control(recipe.source_id)["approved_execution_limits"]
                assert isinstance(approved, dict) and approved["max_tenders"] is None
                operation = svc.operation(activation.id)
                assert operation.status == "activated", operation
            # Simulate a duplicate queue row from a pre-fix release. Kill its real
            # owner after claim; another operation's committed approval is not its effect.
            with sessions.begin() as session:
                legacy = OperatorService(session, config, datetime.now(UTC)).enqueue(
                    "activation", recipe.source_id, {"operation_id": upgrade["id"]}
                )
            context = multiprocessing.get_context("spawn")
            ready = context.Queue()
            process = context.Process(
                target=paused_activation_worker,
                args=(migrated_engine.url.render_as_string(hide_password=False), legacy.id, ready),
            )
            process.start()
            try:
                assert ready.get(timeout=15) == str(legacy.id)
                process.kill()
                process.join(timeout=10)
                assert not process.is_alive()
                with sessions.begin() as session:
                    assert recover_operations(session, datetime.now(UTC)) == 1
                    recovered = session.get(OperationRow, legacy.id)
                    assert recovered and recovered.status == "failed"
                    assert recovered.error_code == "worker_interrupted"
                    assert recovered.result["source_activation_persisted"] is False
                    origin = session.get(OperationRow, UUID(upgrade["id"]))
                    assert origin and origin.result["activation_operation_id"] == str(activation.id)
            finally:
                if process.is_alive():
                    process.kill()
                    process.join(timeout=10)
                ready.close()
                ready.join_thread()
        finally:
            runtime.close()
