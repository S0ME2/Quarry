"""CLI application acceptance using owned HTTP/JS, Chromium and real PostgreSQL."""

import json
import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select

from tender_system.collection.adapters import FileEvidenceStore, MemorySessionStore
from tender_system.collection.browser import PlaywrightBrowserAdapter
from tender_system.core.configuration import FreshnessPolicy, SourceCalibration, load_configuration
from tender_system.core.ports import ModelRequest, ModelResponse
from tender_system.core.tenders import ConfigurableTenderValidity
from tender_system.operator import manual_cli as cli
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    OperationRow,
    SourceRow,
    SqlTenderRepository,
    session_factory,
)
from tender_system.testing.acceptance_site import AcceptanceSite, acceptance_site
from tender_system.testing.fakes import FunctionModelClient

pytestmark = [pytest.mark.database, pytest.mark.browser]


class Application:
    def __init__(
        self,
        engine: Engine,
        site: AcceptanceSite,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tender_system.collection import documents

        class OwnedLoopback:
            is_global = True

            def __str__(self) -> str:
                return "127.0.0.1"

        def owned_address(value: str) -> OwnedLoopback:
            assert value == "127.0.0.1", "Acceptance attachments must stay on the owned server"
            return OwnedLoopback()

        # Exercise real HTTP/PDF extraction on this owned server only. Production SSRF
        # checks remain unchanged; the fixture previously advertised nonexistent PDFs.
        monkeypatch.setattr(documents, "ipaddress", SimpleNamespace(ip_address=owned_address))
        self.sessions = session_factory(engine)
        self.database_name = engine.url.database
        self.site, self.capsys = site, capsys
        monkeypatch.setenv("TEST_DATABASE_URL", engine.url.render_as_string(hide_password=False))
        self.config = load_configuration(
            environ={"QUARRY_DATABASE_URL_ENV": "TEST_DATABASE_URL"}, validate_models=False
        )
        self.config = self.config.model_copy(
            update={
                "global_settings": self.config.global_settings.model_copy(
                    update={"database_url_env": "TEST_DATABASE_URL"}
                ),
            }
        )
        self.evidence = FileEvidenceStore(
            Path("artifacts/tests/reliability-audit/acceptance") / uuid4().hex
        )
        self.runtime = OperatorRuntime(
            self.sessions,
            cli.manual_config(self.config),
            evidence=self.evidence,
            browsers=lambda: PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=2),
        )
        from tender_system.core.clock import SystemClock
        from tender_system.operator.matching_budget import MatchingBudget
        from tender_system.persistence.storage import OperatorConfigRow

        with self.sessions.begin() as session:
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
                    updated_at=SystemClock().now(),
                )
            )
        self.model_requests: list[ModelRequest] = []
        monkeypatch.setenv("GROQ_API_KEY", "fixture-provider-never-sent-to-network")

    def semantic(self, request: ModelRequest) -> ModelResponse:
        self.model_requests.append(request)
        return ModelResponse(
            {
                "decision": "match",
                "confidence": 0.95,
                "explanation": "Requested network router is present",
                "evidence": [{"field": "product_items.0.name", "quote": "Network router"}],
            },
            "acceptance-fixture",
        )

    def run(self, words: list[str]) -> tuple[int, Any]:
        code = cli.dispatch(
            cli.parser().parse_args(["--json", *words]),
            self.sessions,
            self.config,
            runtime=self.runtime,
            providers=lambda _: FunctionModelClient(self.semantic),
            qwen_confirmed=True,
        )
        rows = [json.loads(line) for line in self.capsys.readouterr().out.splitlines()]
        assert rows, words
        return code, rows[-1]

    def ok(self, words: list[str]) -> Any:
        code, result = self.run(words)
        assert code == 0, json.dumps({"command": words, "result": result}, ensure_ascii=False)
        return result


@pytest.fixture
def application(
    migrated_engine: Engine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Application]:
    with acceptance_site() as site:
        app = Application(migrated_engine, site, capsys, monkeypatch)
        try:
            yield app
        finally:
            app.runtime.close()


@pytest.mark.parametrize("limit", [100, None], ids=["bounded100", "full250"])
def test_manual_application_acceptance(
    application: Application, tmp_path: Path, limit: int | None
) -> None:
    app = application
    started = monotonic()
    doctor_args = cli.parser().parse_args(
        [
            "doctor",
            "--expected-database",
            str(app.database_name),
            "--browser",
        ]
    )
    # Real doctor opens its own connection in the already identity-checked isolated schema.
    assert cli.doctor(doctor_args, app.config) == 0
    readiness = json.loads(app.capsys.readouterr().out.splitlines()[-1])
    assert readiness["doctor"] == "PASS" and readiness["provider_requests"] == 0
    recipe = app.site.recipe()
    recipe_path = tmp_path / "recipe.json"
    original = recipe.model_dump_json()
    recipe_path.write_text(original)
    assert cli.main(["validate", str(recipe_path)]) == 0
    app.capsys.readouterr()
    imported = app.ok(["recipe-import", str(recipe_path)])
    args = ["--max-tenders", str(limit)] if limit else []
    validated = app.ok(["recipe-validate", imported["id"], *args, "--max-requests", "600"])
    report = validated["result"]
    assert report["validation_passed"] and report["replay_passed"]
    assert report["observations_collected"] == (limit or 250)
    assert report["health"]["inventory_mode"] == ("sampled" if limit else "full")
    assert report["inventory_authority"] is (limit is None)
    assert report["sample_evidence"]
    # Replay must consume captured bytes; only acquisition and JS witnesses touch localhost.
    expected_requests = (limit or 250) + (2 if limit else 5)
    assert (
        sum(path.startswith(("/api", "/detail/")) for path in app.site.requests)
        == expected_requests
    )
    assert report["http_requests"] == expected_requests
    assert sum(path.startswith("/notice/") for path in app.site.requests) == 3
    assert sum(path.startswith("/witness/") for path in app.site.requests) == 3
    evidence = app.evidence.get(report["validation_evidence_reference"])
    assert evidence["validation_passed"] is True
    app.ok(["approve", imported["id"]])
    app.ok(["source-enable", recipe.source_id])
    for _ in range(2):
        collected = app.ok(["collect", recipe.source_id, *args, "--max-requests", "600"])
        assert collected["result"]["records_processed"] == (limit or 250)
        assert collected["result"]["publication"] == "accepted"
        assert not collected["result"]["health"]["absence_evidence_allowed"]
    with app.sessions() as session:
        repository = SqlTenderRepository(session)
        tenders = repository.for_source(recipe.source_id)
        assert len(tenders) == (limit or 250)
        assert len({t.external_tender_id for t in tenders}) == len(tenders)
        for tender in tenders:
            assert len(repository.history(tender.internal_id)) == 1
            assert tender.published_at is None and len(tender.document_urls) == 1
            assert tender.product_items is not None and len(tender.product_items) == 2
            assert tender.presence.missing_successful_runs == 0
        target = next(t for t in tenders if t.external_tender_id == "0")
        target_id = str(target.internal_id)
    campaign_file = tmp_path / "campaign.json"
    campaign_file.write_text(
        json.dumps(
            {
                "criteria": {
                    "name": "Network router",
                    "ai_budget_usd": 10,
                    "include_keywords": ["router"],
                }
            }
        )
    )
    campaign = app.ok(["campaign-create", str(campaign_file)])
    pair = f"{target_id}:{campaign['id']}"
    deterministic = app.ok(["rules", "--pair", pair])
    assert deterministic[0]["outcome"]["result"] == "match"
    assert not app.model_requests
    semantic = app.ok(["qwen", "--pair", pair])
    assert semantic[0]["outcome"]["result"] == "match" and len(app.model_requests) == 1
    previous_version = semantic[0]["tender_version_id"]
    app.site.product_revision = 2
    app.ok(["collect", recipe.source_id, *args, "--max-requests", "600"])
    with app.sessions() as session:
        repository = SqlTenderRepository(session)
        tenders = repository.for_source(recipe.source_id)
        assert sum(len(repository.history(t.internal_id)) for t in tenders) == len(tenders) + 1
        current = next(t for t in tenders if t.external_tender_id == "0")
        assert current.product_items is not None and current.product_items[0]["quantity"] == 2
        assert set(repository.history(current.internal_id)[-1].changed_fields) == {"product_items"}
    rematched = app.ok(["qwen", "--pair", pair])
    assert rematched[0]["tender_version_id"] != previous_version
    assert len(app.model_requests) == 2
    repeated = app.ok(["qwen", "--pair", pair])
    assert repeated[0]["id"] == rematched[0]["id"] and len(app.model_requests) == 2
    results = app.ok(["results", campaign["id"]])
    assert len(results) == 3
    campaign_file.write_text(
        json.dumps({"criteria": {"name": "Hard exclusion", "exclude_keywords": ["router"]}})
    )
    excluded = app.ok(["campaign-create", str(campaign_file)])
    rejected = app.ok(["qwen", "--pair", f"{target_id}:{excluded['id']}"])
    assert rejected[0]["outcome"]["result"] == "no_match"
    assert len(app.model_requests) == 2
    subset = app.ok(["collect", recipe.source_id, "--max-tenders", "1", "--max-requests", "3"])
    assert subset["result"]["records_processed"] == 1
    assert not subset["result"]["health"]["absence_evidence_allowed"]
    with app.sessions() as session:
        stored = SqlTenderRepository(session).for_source(recipe.source_id)
        assert len(stored) == (limit or 250)
        assert all(t.presence.missing_successful_runs == 0 for t in stored)
    assert recipe_path.read_text() == original
    with app.sessions() as session:
        operations = list(session.scalars(select(OperationRow)))
        assert operations and all(row.status in {"completed", "activated"} for row in operations)
        assert session.get(SourceRow, recipe.source_id) is not None
        assert session.get(ActiveRecipeRow, recipe.source_id) is not None
    print(
        json.dumps(
            {
                "acceptance_scope": limit or "full",
                "records": limit or 250,
                "elapsed_seconds": round(monotonic() - started, 3),
            }
        )
    )


@pytest.mark.parametrize("bounded", [True, False], ids=["sampled", "full"])
@pytest.mark.parametrize("drift", ["total", "snapshot"])
def test_approval_evaluates_global_count_in_validated_scope(
    application: Application,
    tmp_path: Path,
    bounded: bool,
    drift: str,
) -> None:
    app = application
    # Resolve global total before emitting records: fresh three can observe a changed total.
    recipe = app.site.recipe(delayed_total=False)
    path = tmp_path / "recipe.json"
    path.write_text(recipe.model_dump_json())
    imported = app.ok(["recipe-import", str(path)])
    limits = ["--max-tenders", "100"] if bounded else []
    app.ok(["recipe-validate", imported["id"], *limits, "--max-requests", "300"])
    if drift == "total":
        app.site.count = 260
        app.site.reported_total = 260  # New records appended; checked first witnesses unchanged.
    else:
        # Later-page revisions change inventory evidence without changing first witnesses.
        app.site.snapshot_revision += 1
    code, result = app.run(["approve", imported["id"]])
    assert code == (0 if bounded else 1), json.dumps(result)
    with app.sessions() as session:
        assert (session.get(SourceRow, recipe.source_id) is not None) is bounded
        assert (session.get(ActiveRecipeRow, recipe.source_id) is not None) is bounded
        assert not SqlTenderRepository(session).for_source(recipe.source_id)
    if not bounded:
        assert result["status"] == "failed" and result["error_code"]


@pytest.mark.parametrize("damage", ["changed-record", "corrupt-evidence", "invalid-witness"])
def test_invalid_evidence_cannot_partially_activate(
    application: Application,
    tmp_path: Path,
    damage: str,
) -> None:
    app = application
    recipe = app.site.recipe()
    path = tmp_path / "recipe.json"
    path.write_text(recipe.model_dump_json())
    imported = app.ok(["recipe-import", str(path)])
    if damage == "invalid-witness":
        app.site.bad_witness = True
    code, validation = app.run(
        [
            "recipe-validate",
            imported["id"],
            "--max-tenders",
            "100",
            "--max-requests",
            "300",
        ]
    )
    if damage == "invalid-witness":
        assert code == 1 and validation["result"]["validation_passed"] is False
    else:
        assert code == 0, json.dumps(validation)
        if damage == "changed-record":
            app.site.product_revision = 2
        else:
            reference = validation["result"]["validation_evidence_reference"]
            (app.evidence.root / (reference + ".json")).write_text('{"validation_passed":true}')
    if damage == "invalid-witness":
        # CLI main translates this application rejection to nonzero exit before enqueue.
        with pytest.raises(ValueError, match="only validated proposals"):
            app.run(["approve", imported["id"]])
    else:
        code, result = app.run(["approve", imported["id"]])
        assert code == 1 and result["status"] == "failed", json.dumps(result)
    with app.sessions() as session:
        assert session.get(SourceRow, recipe.source_id) is None
        assert session.get(ActiveRecipeRow, recipe.source_id) is None
        assert not SqlTenderRepository(session).for_source(recipe.source_id)


def test_historic_incremental_pilot_delayed_count_approves(
    application: Application,
    tmp_path: Path,
) -> None:
    app = application
    app.site.count = app.site.reported_total = app.site.page_size = 100
    recipe = app.site.recipe(incremental=True)
    path = tmp_path / "incremental.json"
    path.write_text(recipe.model_dump_json())
    imported = app.ok(["recipe-import", str(path)])
    validation = app.ok(["recipe-validate", imported["id"], "--max-requests", "150"])
    assert validation["result"]["expected_count"] == 100
    assert validation["result"]["observations_collected"] == 100
    # Approval's three-record run exits before assigning expected_total, yielding None.
    approved = app.ok(["approve", imported["id"]])
    assert approved["status"] == "activated"


def test_same_recipe_explicitly_upgrades_bounded_authorization(
    application: Application,
    tmp_path: Path,
) -> None:
    app = application
    recipe = app.site.recipe()
    path = tmp_path / "recipe.json"
    original = recipe.model_dump_json()
    path.write_text(original)
    imported = app.ok(["recipe-import", str(path)])
    app.ok(["recipe-validate", imported["id"], "--max-tenders", "100", "--max-requests", "300"])
    app.ok(["approve", imported["id"]])
    app.ok(["source-enable", recipe.source_id])
    app.ok(["collect", recipe.source_id, "--max-tenders", "100", "--max-requests", "300"])
    requests_before = len(app.site.requests)
    code, denied = app.run(["collect", recipe.source_id, "--max-requests", "300"])
    assert code == 1 and denied["status"] == "failed"
    assert len(app.site.requests) == requests_before
    upgrade = app.ok(["recipe-import", str(path), "--revalidate", recipe.source_id])
    app.ok(["recipe-validate", upgrade["id"], "--max-requests", "300"])
    app.ok(["approve", upgrade["id"]])
    app.ok(["source-enable", recipe.source_id])
    full = app.ok(["collect", recipe.source_id, "--max-requests", "300"])
    assert full["result"]["records_processed"] == 250
    with app.sessions() as session:
        repository = SqlTenderRepository(session)
        tenders = repository.for_source(recipe.source_id)
        assert len(tenders) == 250
        assert all(len(repository.history(t.internal_id)) == 1 for t in tenders)
        assert all(t.presence.missing_successful_runs == 0 for t in tenders)
    assert path.read_text() == original


@pytest.mark.parametrize("failure", ["detail-schema", "repeated-page", "request-budget"])
def test_failed_collection_preserves_published_records(
    application: Application,
    tmp_path: Path,
    failure: str,
) -> None:
    app = application
    recipe = app.site.recipe()
    path = tmp_path / "recipe.json"
    path.write_text(recipe.model_dump_json())
    imported = app.ok(["recipe-import", str(path)])
    limits = ["--max-tenders", "100", "--max-requests", "110"]
    app.ok(["recipe-validate", imported["id"], *limits])
    app.ok(["approve", imported["id"]])
    app.ok(["source-enable", recipe.source_id])
    app.ok(["collect", recipe.source_id, *limits])
    if failure == "detail-schema":
        app.site.broken_detail = True
    elif failure == "repeated-page":
        app.site.repeated_page = True
    else:
        limits = ["--max-tenders", "100", "--max-requests", "2"]
    code, failed = app.run(["collect", recipe.source_id, *limits])
    assert code == 1 and failed["status"] == "failed", json.dumps(failed)
    assert failed["result"]["records_processed"] == 0
    assert failed["result"]["error_code"]
    with app.sessions() as session:
        repository = SqlTenderRepository(session)
        stored = repository.for_source(recipe.source_id)
        assert len(stored) == 100
        assert all(len(repository.history(t.internal_id)) == 1 for t in stored)
        assert all(t.presence.missing_successful_runs == 0 for t in stored)


def test_rate_limit_retry_and_missing_optional_fields_use_real_application(
    application: Application,
    tmp_path: Path,
) -> None:
    app = application
    app.site.omit_optional = True
    app.site.rate_limit_once = True
    recipe = app.site.recipe()
    path = tmp_path / "recipe.json"
    path.write_text(recipe.model_dump_json())
    imported = app.ok(["recipe-import", str(path)])
    validation = app.ok(
        [
            "recipe-validate",
            imported["id"],
            "--max-tenders",
            "5",
            "--max-requests",
            "10",
        ]
    )
    # One transient429 plus listing and5details. Replay repeats no source requests.
    assert validation["result"]["http_requests"] == 7
    app.ok(["approve", imported["id"]])
    app.ok(["source-enable", recipe.source_id])
    collected = app.ok(["collect", recipe.source_id, "--max-tenders", "5"])
    assert collected["result"]["records_processed"] == 5
    with app.sessions() as session:
        stored = SqlTenderRepository(session).for_source(recipe.source_id)
        assert len(stored) == 5
        assert all(t.published_at is None and not t.document_urls for t in stored)
        assert all(t.product_items and len(t.product_items) == 2 for t in stored)


def test_authoritative_fresh_inventory_advances_only_legitimate_absence(
    application: Application,
    tmp_path: Path,
) -> None:
    app = application
    calibration = SourceCalibration(
        version="acceptance-source-timestamp",
        status="calibrated",
        evidence_references=("owned-website-inventory-contract",),
        full_inventory_supported=True,
        completion_signals=("final_page",),
        freshness=FreshnessPolicy(
            basis="inventory_timestamp",
            timestamp_meaning="Owned website inventory snapshot time",
            max_age_seconds=3600,
        ),
    )
    source_settings = app.config.global_settings.source.model_copy(
        update={"calibration": calibration}
    )
    app.config = app.config.model_copy(
        update={
            "global_settings": app.config.global_settings.model_copy(
                update={"source": source_settings}
            ),
        }
    )
    app.runtime.config = cli.manual_config(app.config)
    recipe = app.site.recipe(inventory_timestamp=True)
    path = tmp_path / "recipe.json"
    path.write_text(recipe.model_dump_json())
    imported = app.ok(["recipe-import", str(path)])
    validated = app.ok(["recipe-validate", imported["id"], "--max-requests", "300"])
    assert validated["result"]["inventory_authority"] is True
    assert validated["result"]["health"]["freshness"]["status"] == "fresh"
    app.ok(["approve", imported["id"]])
    app.ok(["source-enable", recipe.source_id])
    first = app.ok(["collect", recipe.source_id, "--max-requests", "300"])
    assert first["result"]["health"]["absence_evidence_allowed"]
    app.site.count = app.site.reported_total = 249  # One real tender removed from stable inventory.
    threshold = source_settings.validity.missing_successful_runs
    for missing_count in range(1, threshold + 1):
        # Each missing observation needs a newly published source snapshot, not a replay.
        app.site.inventory_as_of = datetime.now(UTC).isoformat()
        app.site.snapshot_revision += 1
        full = app.ok(["collect", recipe.source_id, "--max-requests", "300"])
        assert full["result"]["records_processed"] == 249
        assert full["result"]["health"]["absence_evidence_allowed"]
        # A bounded first-page collection between inventories never advances missing state.
        app.site.inventory_as_of = datetime.now(UTC).isoformat()
        app.site.snapshot_revision += 1
        subset = app.ok(["collect", recipe.source_id, "--max-tenders", "1"])
        assert not subset["result"]["health"]["absence_evidence_allowed"]
        with app.sessions() as session:
            stored = SqlTenderRepository(session).for_source(recipe.source_id)
            assert len(stored) == 250  # Absence never deletes history or tender identity.
            removed = next(t for t in stored if t.external_tender_id == "249")
            assert removed.presence.missing_successful_runs == missing_count
            assert all(t.presence.missing_successful_runs == 0 for t in stored if t != removed)
            validity = ConfigurableTenderValidity().evaluate(
                removed, source_settings, datetime.now(UTC)
            )
            assert validity.matchable is (missing_count < threshold)
            assert (
                removed.published_at is None
            )  # Inventory timestamps never become publication dates.


def test_console_entrypoint_uses_isolated_environment_and_application_services(
    migrated_engine: Engine,
) -> None:
    root = Path(__file__).resolve().parents[2]
    artifacts = root / "artifacts/tests/reliability-audit/acceptance" / uuid4().hex
    artifacts.mkdir(parents=True)
    config = artifacts / "configuration.toml"
    config.write_text(
        '[global]\ndatabase_url_env = "QUARRY_ACCEPTANCE_DATABASE_URL"\n'
        '[runtime]\nevidence_directory = "' + str(artifacts / "evidence") + '"\n'
        'session_directory = "' + str(artifacts / "sessions") + '"\n'
    )
    environment_file = artifacts / "fixture.env"
    environment_file.write_text(
        "# Configuration for this isolated acceptance database only.\n"
        "DATABASE_URL=\nTEST_DATABASE_URL=\n"
        "QUARRY_BROWSER_ENABLED=true\n"
        "QUARRY_AI_INVESTIGATION_ENABLED=false\nQUARRY_AI_REPAIR_ENABLED=false\n"
        "QUARRY_AI_SEMANTIC_ENABLED=false\nQUARRY_AUTOMATIC_REPAIR_ENABLED=false\n"
        "QUARRY_OPERATOR_WORKER_ENABLED=false\nQUARRY_COLLECTION_ENABLED=false\n"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("QUARRY_") and key not in {"DATABASE_URL", "TEST_DATABASE_URL"}
    }
    # The real PostgreSQL URL stays in process environment, never fixture files or output.
    environment["QUARRY_ACCEPTANCE_DATABASE_URL"] = migrated_engine.url.render_as_string(
        hide_password=False
    )

    def run(words: list[str], *, expected: int = 0) -> Any:
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--locked",
                "quarry-manual",
                "--env-file",
                str(environment_file),
                "--config",
                str(config),
                "--json",
                *words,
            ],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=45,
        )
        rows = [json.loads(line) for line in completed.stdout.splitlines()]
        assert completed.returncode == expected, json.dumps({"command": words, "output": rows})
        assert rows, words
        return rows[-1]

    with acceptance_site() as site:
        recipe = site.recipe(source_id="console-acceptance")
        path = artifacts / "recipe.json"
        path.write_text(recipe.model_dump_json())
        assert (
            run(["doctor", "--expected-database", str(migrated_engine.url.database), "--browser"])[
                "doctor"
            ]
            == "PASS"
        )
        assert run(["validate", str(path)])["valid"]
        imported = run(["recipe-import", str(path)])
        # Invalid approval reaches actual main's error-to-exitcode boundary.
        assert run(["approve", imported["id"]], expected=1)["error"]
        validated = run(
            ["recipe-validate", imported["id"], "--max-tenders", "100", "--max-requests", "110"]
        )
        assert validated["result"]["validation_passed"]
        assert run(["approve", imported["id"]])["status"] == "activated"
        run(["source-enable", recipe.source_id])
        collected = run(
            ["collect", recipe.source_id, "--max-tenders", "100", "--max-requests", "110"]
        )
        assert (
            collected["status"] == "completed" and collected["result"]["records_processed"] == 100
        )
        assert run(["status", collected["id"]])["status"] == "completed"
        catalog = run(["tenders", "--source", recipe.source_id, "--limit", "100"])
        assert len(catalog["items"]) == 100
        with session_factory(migrated_engine)() as session:
            stored = SqlTenderRepository(session).for_source(recipe.source_id)
            assert len(stored) == 100
            assert all(t.product_items and t.document_urls for t in stored)
