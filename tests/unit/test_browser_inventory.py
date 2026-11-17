"""Browser pagination identity must represent rendered state rather than just a URL."""

from uuid import uuid4

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.recipes import RecipeExecutor
from tender_system.collection.validation_evidence import CapturedBrowserSteps
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import CollectionRun, ExecutionContext, SourceFailure, SourceRecipe
from tender_system.testing.fakes import FakeBrowserAdapter, FakePage, ScriptedHttpAdapter


def recipe() -> SourceRecipe:
    return SourceRecipe.model_validate(
        {
            "source_id": "browser-pages",
            "version": "1",
            "steps": [
                {
                    "type": "browser_action",
                    "parameters": {"action": "navigate", "value": "http://website.test/page/0"},
                },
                {
                    "type": "loop",
                    "parameters": {"items": [1, 2]},
                    "steps": [
                        {
                            "type": "browser_action",
                            "parameters": {
                                "action": "click",
                                "selector": ".next",
                                "inventory_page": True,
                            },
                        },
                        {
                            "type": "browser_action",
                            "parameters": {"action": "content", "output": "html"},
                        },
                        {
                            "type": "extract",
                            "parameters": {
                                "format": "html",
                                "input": {"$ref": "html"},
                                "selector": "article",
                                "fields": {
                                    "external_tender_id": {"selector": ".", "attribute": "data-id"},
                                    "title": {"selector": "h1"},
                                },
                                "emit": True,
                            },
                        },
                    ],
                },
            ],
        }
    )


def test_click_inventory_pages_are_captured_and_replay_without_browser() -> None:
    pages = {
        f"http://website.test/page/{i}": FakePage(
            f'<article data-id="{i}"><h1>Supply {i}</h1></article>',
            click_targets={".next": f"http://website.test/page/{min(i + 1, 2)}"},
        )
        for i in range(3)
    }
    evidence = MemoryEvidenceStore()
    result = RecipeExecutor(
        ScriptedHttpAdapter(()),
        evidence,
        SourceSettings(),
        browser_factory=lambda: FakeBrowserAdapter(pages),
    ).execute(recipe(), ExecutionContext(run_id=uuid4()))
    assert isinstance(result, CollectionRun)
    assert len(result.diagnostics.pages) == 2
    assert len(set(result.diagnostics.pages)) == 2
    captured = evidence.get(result.evidence_reference)
    replay = RecipeExecutor(ScriptedHttpAdapter(()), evidence, SourceSettings()).execute(
        recipe(),
        ExecutionContext(run_id=uuid4()),
        browser_replay=CapturedBrowserSteps(captured["browser_steps"]),
    )
    assert isinstance(replay, CollectionRun)
    assert replay.raw_observations == result.raw_observations
    assert replay.diagnostics.pages == result.diagnostics.pages


def test_repeated_click_page_fails_closed_before_fabricating_completion() -> None:
    page = FakePage('<article data-id="1"><h1>Supply</h1></article>')
    result = RecipeExecutor(
        ScriptedHttpAdapter(()),
        MemoryEvidenceStore(),
        SourceSettings(),
        browser_factory=lambda: FakeBrowserAdapter({"http://website.test/page/0": page}),
    ).execute(recipe(), ExecutionContext(run_id=uuid4()))
    assert isinstance(result, SourceFailure)
    assert "repeated" in result.error_detail
