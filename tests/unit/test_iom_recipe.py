"""Source-backed HTML fragments; terminal sequence is deliberately synthetic."""

from pathlib import Path
from uuid import uuid4

import pytest

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.recipes import FileRecipeLoader, RecipeExecutor
from tender_system.collection.validation_evidence import CapturedResponseHttpAdapter
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import CollectionRun, ExecutionContext, SourceFailure
from tender_system.core.ports import HttpResponse
from tender_system.testing.fakes import ScriptedHttpAdapter

ASSETS = Path(".agents/skills/quarry-recipe-engineer/assets")
FIXTURES = Path("tests/fixtures/iom")


@pytest.mark.parametrize(
    "fault", [None, "wrong_id", "missing_title", "blank_title", "repeated_page", "empty_page"]
)
def test_iom_html_pagination_and_source_faults(fault: str | None) -> None:
    recipe = FileRecipeLoader().load(str(ASSETS / "iom.recipe.2026-10-19.json"))
    listing = [(FIXTURES / f"listing-{i}.html").read_text() for i in range(2)]
    details = [(FIXTURES / f"detail-{i}.html").read_text() for i in range(2)]
    if fault == "wrong_id":
        details[0] = details[0].replace("30000035139", "WRONG")
    elif fault == "missing_title":
        details[0] = details[0].replace("<h2>", "<h4>").replace("</h2>", "</h4>")
    elif fault == "blank_title":
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(details[0], "html.parser")
        title = soup.select_one("h2")
        assert title is not None
        title.string = "  "
        details[0] = str(soup)
    elif fault == "repeated_page":
        listing[1] = listing[0]
    elif fault == "empty_page":
        listing[1] = "<html>No records</html>"
    responses = [
        HttpResponse("https://www.iom.int", 200, html.encode())
        for html in [listing[0], details[0], listing[1], details[1]]
    ]
    store = MemoryEvidenceStore()
    executor = RecipeExecutor(ScriptedHttpAdapter(tuple(responses)), store, SourceSettings())
    result = executor.execute(recipe, ExecutionContext(run_id=uuid4()))
    if fault:
        assert isinstance(result, SourceFailure), result
        return
    assert isinstance(result, CollectionRun)
    assert len(result.raw_observations) == 2
    assert len({row["external_tender_id"] for row in result.raw_observations}) == 2
    assert len(result.diagnostics.pages) == 2
    assert result.diagnostics.completion is not None and result.diagnostics.completion.observed
    evidence = store.get(result.evidence_reference)
    adapter = CapturedResponseHttpAdapter(evidence["http_exchanges"])
    replay = executor.execute(recipe, ExecutionContext(run_id=uuid4()), http_adapter=adapter)
    adapter.assert_exhausted()
    assert isinstance(replay, CollectionRun)
    assert replay.raw_observations == result.raw_observations
