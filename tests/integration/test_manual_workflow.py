"""Terminal recipe import, validation, scraping and matching against isolated fixtures."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, select

from tender_system.collection.adapters import MemoryEvidenceStore, MemorySessionStore
from tender_system.collection.browser import PlaywrightBrowserAdapter
from tender_system.core.configuration import ModelSettings, load_configuration
from tender_system.core.ports import BrowserAdapter, ModelClient, ModelFailure, ModelResponse
from tender_system.operator import manual_cli as cli
from tender_system.operator.operations import OperatorService
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.matching_storage import matching_planner
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    MatchJobRow,
    MatchResultRow,
    OperationRow,
    session_factory,
)
from tender_system.testing.fakes import FakeBrowserAdapter, FakePage, ScriptedModelClient
from tender_system.testing.operator import fixture_recipe, onboarding_site

pytestmark = pytest.mark.database


@pytest.mark.parametrize("real_browser", [False, pytest.param(True, marks=pytest.mark.browser)])
def test_manual_workflow(
    migrated_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    real_browser: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "offline-fixture-key")
    sessions = session_factory(migrated_engine)
    from tender_system.core.clock import SystemClock
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
                updated_at=SystemClock().now(),
            )
        )
    config = load_configuration(
        environ={
            "QUARRY_MATCHER__PROVIDER": "groq",
            "QUARRY_MATCHER__MODEL": "qwen/qwen3.8-27b",
            "QUARRY_MATCHER__API_KEY_ENV": "GROQ_API_KEY",
        },
        validate_models=False,
    )

    with onboarding_site() as url:
        browser_pages = {
            url: FakePage('<h1>Procurement inventory</h1><a href="/api">API</a>'),
            **{
                f"{url}/detail/{i}": FakePage(f"<h1>Network equipment {i}</h1><p>Tender ID {i}</p>")
                for i in range(2)
            },
        }

        browser_types: list[type[BrowserAdapter]] = []

        def browser() -> BrowserAdapter:
            if real_browser:
                instance: BrowserAdapter = PlaywrightBrowserAdapter(MemorySessionStore())
            else:
                instance = FakeBrowserAdapter(browser_pages)
            browser_types.append(type(instance))
            return instance

        runtime = OperatorRuntime(
            sessions,
            cli.manual_config(config),
            evidence=MemoryEvidenceStore(),
            browsers=browser,
        )
        # Explicit CLI execution must leave unrelated queued work untouched.
        with sessions.begin() as session:
            unrelated = OperatorService(session, config, runtime.clock.now()).enqueue(
                "collection", "unrelated", {}
            )

        def run(
            words: list[str],
            providers: Callable[[ModelSettings], ModelClient] | None = None,
        ) -> tuple[int, list[Any]]:
            args = cli.parser().parse_args(words)
            options: dict[str, Any] = {"runtime": runtime}
            if words[0] == "qwen":
                options["qwen_confirmed"] = True
            if providers is not None:
                options["providers"] = providers
            code = cli.dispatch(args, sessions, config, **options)
            output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            return code, output

        try:
            path = tmp_path / "input-recipe.json"
            path.write_text(fixture_recipe("fixture", url).model_dump_json())
            code, output = run(["recipe-import", str(path)])
            assert code == 0, output
            key = output[-1]["id"]
            code, output = run(["recipe-validate", key])
            assert code == 0, output
            code, output = run(["status", key])
            operation = output[-1]
            assert operation["status"] == "awaiting_approval", operation
            assert operation["result"]["validation_passed"] is True, operation
            assert runtime.thread is None
            if real_browser:
                assert browser_types and all(
                    item is PlaywrightBrowserAdapter for item in browser_types
                )
            with sessions() as session:
                assert session.get(ActiveRecipeRow, operation["source_id"]) is None
            code, output = run(["status", key])
            assert code == 0
            assert output[-1]["next"].endswith(f" approve {key}")
            with pytest.raises((LookupError, ValueError)):
                run(["collect", operation["source_id"]])
            code, output = run(["recipe", key])
            assert code == 0
            assert "/api" in json.dumps(output[-1])
            path = tmp_path / "recipe.json"
            path.write_text(json.dumps(output[-1]))
            assert cli.main(["validate", str(path)]) == 0
            capsys.readouterr()
            code, output = run(["approve", key])
            assert code == 0 and output[-1]["status"] == "activated", output
            source = operation["source_id"]
            code, output = run(["source-enable", source])
            assert code == 0
            code, output = run(["collect", source])
            assert code == 0 and output[-1]["result"]["publication"] == "accepted", output
            code, output = run(["tenders"])
            assert code == 0 and len(output[-1]["items"]) == 2
            tender = output[-1]["items"][0]["id"]
            unselected = output[-1]["items"][1]["id"]
            campaign_file = tmp_path / "campaign.json"
            campaign_file.write_text(
                json.dumps(
                    {
                        "criteria": {
                            "ai_budget_usd": 10,
                            "name": "Network equipment",
                            "include_keywords": ["network"],
                        }
                    }
                )
            )
            code, output = run(["campaign-create", str(campaign_file)])
            assert code == 0
            campaign = output[-1]["id"]
            with sessions.begin() as session:
                assert list(session.scalars(select(MatchJobRow))) == []
                matching_planner(session, config).for_pairs(
                    ((UUID(unselected), UUID(campaign)),), runtime.clock.now()
                )
            selection = f"{tender}:{campaign}"

            # Rules path must not construct any provider.
            def forbidden(settings: ModelSettings) -> ModelClient:
                pytest.fail("deterministic command attempted AI")

            code, output = run(["rules", "--pair", selection], providers=forbidden)
            assert code == 0 and output[-2]["completed"] == 1
            assert output[-1][0]["outcome"]["model_calls"] == []
            code, output = run(["jobs", campaign])
            assert code == 0 and any(
                job["tender_id"] == tender and job["status"] == "done" for job in output[-1]
            )
            response = ModelResponse(
                output={
                    "decision": "match",
                    "confidence": 0.95,
                    "explanation": "Fits network equipment",
                    "evidence": [{"field": "title", "quote": "Network equipment"}],
                },
                model_version="fake",
                input_tokens=42,
                output_tokens=7,
            )
            fake = ScriptedModelClient((response,))
            code, output = run(["qwen", "--pair", selection], providers=lambda settings: fake)
            assert code == 0 and output[-2]["completed"] == 1, output
            assert len(fake.requests) == 1 and fake.requests[0].max_attempts == 1
            assert fake.requests[0].max_output_tokens <= 600
            calls = output[-1][0]["outcome"]["model_calls"]
            assert calls[0]["input_tokens"] == 42 and calls[0]["output_tokens"] == 7
            assert calls[0]["error_code"] is None
            # Repeated identical command is durable/idempotent, not a second inference.
            code, output = run(["qwen", "--pair", selection], providers=forbidden)
            assert code == 0 and output[-2]["claimed"] == 0
            code, output = run(["results", campaign])
            assert code == 0 and len(output[-1]) == 2
            with sessions() as session:
                unrelated_row = session.get(OperationRow, unrelated.id)
                assert unrelated_row is not None and unrelated_row.status == "queued"
                assert len(list(session.scalars(select(MatchResultRow)))) == 2
                assert any(job.status == "pending" for job in session.scalars(select(MatchJobRow)))
            code, output = run(["tenders"])
            other_tender = next(row["id"] for row in output[-1]["items"] if row["id"] != tender)
            failed = ScriptedModelClient((ModelFailure("provider_rate_limited", http_status=429),))
            code, output = run(
                ["qwen", "--pair", f"{other_tender}:{campaign}"], providers=lambda settings: failed
            )
            assert code == 1 and len(failed.requests) == 1
            outcome = output[-1][0]["outcome"]
            assert outcome["result"] == "unsure"
            assert outcome["model_calls"][0]["error_code"] == "provider_rate_limited"
            assert outcome["model_calls"][0]["http_status"] == 429
            assert outcome["model_calls"][0]["input_tokens"] is None
        finally:
            runtime.close()
