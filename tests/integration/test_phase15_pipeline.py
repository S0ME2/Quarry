"""Generic recipe-to-run regression; source discovery is deliberately separate."""

import json
from pathlib import Path
from uuid import uuid4

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.recipes import FileRecipeLoader, RecipeExecutor
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import CollectionRun, ExecutionContext
from tender_system.core.ports import HttpRequest, HttpResponse


class GenericInventory:
    def request(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            request.url,
            200,
            json.dumps(
                {
                    "items": [
                        {"external_tender_id": "generic-1", "title": "School equipment"},
                        {"external_tender_id": "generic-2", "title": "Medical equipment"},
                    ],
                    "total": 2,
                    "next": None,
                    "modified_since": "generic-checkpoint",
                    "generated_at": "2026-01-01T00:00:00Z",
                }
            ).encode(),
            {},
        )


def test_generic_recipe_pipeline_loads_and_executes_fixture_inventory() -> None:
    root = Path(__file__).resolve().parents[2]
    recipe = FileRecipeLoader().load(str(root / "recipes/api.example.json"))
    result = RecipeExecutor(GenericInventory(), MemoryEvidenceStore(), SourceSettings()).execute(
        recipe, ExecutionContext(run_id=uuid4())
    )
    assert isinstance(result, CollectionRun)
    assert result.expected_count == 2
    assert [row["external_tender_id"] for row in result.raw_observations] == [
        "generic-1",
        "generic-2",
    ]
    assert result.checkpoint == "generic-checkpoint"
