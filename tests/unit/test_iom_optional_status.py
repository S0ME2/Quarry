"""Current public fragments: missing status stays unknown between known statuses."""

from pathlib import Path
from uuid import uuid4

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.recipes import FileRecipeLoader, RecipeExecutor
from tender_system.collection.validation_evidence import CapturedResponseHttpAdapter
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import CollectionRun, ExecutionContext, SourceRecord
from tender_system.core.ports import HttpResponse
from tender_system.testing.fakes import ScriptedHttpAdapter


def test_source_absent_status_does_not_abort_collection_or_inherit_previous_status() -> None:
    fixtures = Path("tests/fixtures/iom")
    recipe = FileRecipeLoader().load(
        ".agents/skills/quarry-recipe-engineer/assets/iom.recipe.2026-10-19.json"
    )
    fragments = [(fixtures / "listing-20261106.html").read_bytes()] + [
        (fixtures / f"detail-20261106-{index}.html").read_bytes() for index in range(3)
    ]
    evidence = MemoryEvidenceStore()
    settings = SourceSettings()
    executor = RecipeExecutor(
        ScriptedHttpAdapter(
            tuple(HttpResponse("https://www.iom.int", 200, fragment) for fragment in fragments)
        ),
        evidence,
        settings,
    )
    result = executor.execute(recipe, ExecutionContext(run_id=uuid4()))
    assert isinstance(result, CollectionRun)
    assert [record["external_tender_id"] for record in result.raw_observations] == [
        "30000036288",
        "014/06.11.2026",
        "30000034745",
    ]
    assert [record["status"] for record in result.raw_observations] == [
        "Ongoing",
        None,
        "Ongoing",
    ]
    for raw in result.raw_observations:
        normalized = ConfigurableTenderNormalizer().normalize(
            raw, SourceRecord(id="iom", name="IOM"), settings
        )
        assert not normalized.issues and normalized.candidate is not None
        assert normalized.candidate.status == ("ongoing" if raw["status"] else None)
        assert normalized.candidate.document_urls
        assert normalized.candidate.deadline is not None
    replay = CapturedResponseHttpAdapter(evidence.get(result.evidence_reference)["http_exchanges"])
    repeated = executor.execute(recipe, ExecutionContext(run_id=uuid4()), http_adapter=replay)
    replay.assert_exhausted()
    assert isinstance(repeated, CollectionRun)
    assert repeated.raw_observations == result.raw_observations
