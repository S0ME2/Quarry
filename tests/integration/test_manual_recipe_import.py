"""External recipe import and approval use the isolated database and local HTTP fixtures."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.core.configuration import load_configuration
from tender_system.core.domain import FixResult
from tender_system.operator import manual_cli as cli
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    OperationRow,
    SourceRow,
    session_factory,
)
from tender_system.testing.fakes import FakeBrowserAdapter, FakePage
from tender_system.testing.operator import fixture_recipe, onboarding_site

pytestmark = pytest.mark.database


def test_recipe_import_validation_replay_and_stale_approval_rejection(
    migrated_engine: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sessions = session_factory(migrated_engine)
    config = load_configuration(validate_models=False)

    source_id = "manual-fixture"

    requests: list[str] = []
    with onboarding_site(requests) as url:
        recipe = fixture_recipe(source_id, url)
        source_dir = tmp_path / source_id
        source_dir.mkdir()
        recipe_path = source_dir / "recipe.json"
        recipe_path.write_text(recipe.model_dump_json())
        pages = {
            url: FakePage("<h1>Procurement inventory</h1>"),
            **{
                f"{url}/detail/{i}": FakePage(f"<h1>Network equipment {i}</h1><p>Tender ID {i}</p>")
                for i in range(2)
            },
        }
        runtime = OperatorRuntime(
            sessions,
            cli.manual_config(config),
            evidence=MemoryEvidenceStore(),
            browsers=lambda: FakeBrowserAdapter(pages),
        )

        def run(words: list[str]) -> tuple[int, list[Any]]:
            result = cli.dispatch(
                cli.parser().parse_args(words),
                sessions,
                config,
                runtime=runtime,
            )
            output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            return result, output

        try:
            code, output = run(["recipe-import", str(recipe_path)])
            assert code == 0
            staged = output[-1]
            operation_id = staged["id"]
            assert staged["status"] == "awaiting_approval"
            assert staged["recipe_schema_validated"] is True
            assert staged["validation_passed"] is False
            with sessions() as session:
                assert session.get(SourceRow, source_id) is None
                assert session.get(ActiveRecipeRow, source_id) is None
                operation = session.get(OperationRow, UUID(operation_id))
                assert operation is not None
                assert operation.result["manual_recipe_import"] is True
            with pytest.raises(ValueError, match="pending recipe import"):
                run(["recipe-import", str(recipe_path)])

            code, output = run(["recipe-smoke", operation_id, "--max-tenders", "1"])
            smoke = output[-1]["result"]
            assert code == 0
            assert smoke["smoke_only"] is True
            assert smoke["full_validation"] is False
            assert smoke["validation_passed"] is False
            assert smoke["stop_reason"] == "bounded_smoke_completed"
            assert smoke["requested_max_tenders"] == smoke["observations_collected"] == 1
            assert 0 < smoke["diagnostics"]["transport_attempts"] <= 2
            assert requests == ["/api"]
            with sessions() as session:
                assert session.get(SourceRow, source_id) is None
                assert session.get(ActiveRecipeRow, source_id) is None

            code, output = run(["recipe", operation_id])
            assert code == 0 and recipe.version in json.dumps(output)
            code, output = run(["recipe-validate", operation_id])
            assert code == 0 and output[-1]["result"]["validation_passed"] is True
            assert output[-1]["result"]["replay_passed"] is True
            assert requests == ["/api", "/api"]
            with sessions() as session:
                imported = session.get(OperationRow, UUID(operation_id))
                assert imported is not None
                assert imported.status == "awaiting_approval"
                assert imported.result["validation_passed"] is True
                assert session.get(SourceRow, source_id) is None
                assert session.get(ActiveRecipeRow, source_id) is None
                evidence_result = dict(imported.result)
            settings = cli.manual_config(config).for_source(source_id)
            validation = runtime.validation_evidence(evidence_result, recipe, source_id, settings)
            runtime.validation_evidence(evidence_result, recipe, source_id, settings)
            runtime.fresh_source_check(UUID(operation_id), source_id, recipe, settings, validation)
            assert requests == ["/api", "/api", "/api"]
            changed = recipe.model_copy(update={"version": "changed"})
            with pytest.raises(ValueError, match="recipe changed"):
                runtime.validation_evidence(evidence_result, changed, source_id, settings)
            changed_settings = settings.model_copy(
                update={"timeout_seconds": settings.timeout_seconds + 1}
            )
            with pytest.raises(ValueError, match="policy changed"):
                runtime.validation_evidence(evidence_result, recipe, source_id, changed_settings)

            record = dict(
                runtime.evidence.get(str(evidence_result["validation_evidence_reference"]))
            )
            record["created_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            stale_reference = runtime.evidence.put(UUID(operation_id), record)
            evidence_result["validation_evidence_reference"] = stale_reference
            with sessions.begin() as session:
                imported = session.get(OperationRow, UUID(operation_id))
                assert imported is not None
                imported.result = {
                    **imported.result,
                    "validation_evidence_reference": stale_reference,
                }
            with pytest.raises(ValueError, match="stale"):
                runtime.execute(
                    UUID(operation_id),
                    "activation",
                    source_id,
                    {"operation_id": operation_id},
                    cli.manual_config(config),
                )
            assert requests == ["/api", "/api", "/api"]

            bad_pages = {
                url: FakePage("Inventory"),
                **{f"{url}/detail/{i}": FakePage("unrelated tender page") for i in range(2)},
            }
            bad_runtime = OperatorRuntime(
                sessions,
                cli.manual_config(config),
                evidence=MemoryEvidenceStore(),
                browsers=lambda: FakeBrowserAdapter(bad_pages),
            )
            try:
                failed = bad_runtime.test_proposal(
                    UUID(operation_id),
                    source_id,
                    FixResult.model_validate(evidence_result["proposal"]),
                    settings,
                )
                assert failed["validation_passed"] is False
            finally:
                bad_runtime.close()
            assert requests == ["/api", "/api", "/api", "/api"]
            with sessions() as session:
                assert session.get(SourceRow, source_id) is None
                assert session.get(ActiveRecipeRow, source_id) is None
        finally:
            runtime.close()
