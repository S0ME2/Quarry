"""Exercise the actual helper with independently specified browser witnesses."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.recipes import RecipeExecutor
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import CollectionRun, ExecutionContext, SourceFailure, SourceRecipe
from tender_system.testing.fakes import FakeBrowserAdapter, FakePage, ScriptedHttpAdapter

SKILL = Path(__file__).resolve().parents[2] / ".agents/skills/quarry-recipe-engineer"


@pytest.fixture
def browser_evidence() -> dict[str, Any]:
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "skill",
            "version": "1",
            "steps": [
                {
                    "type": "browser_action",
                    "parameters": {"action": "navigate", "value": "https://site.test/1"},
                },
                {"type": "browser_action", "parameters": {"action": "content", "output": "html"}},
                {
                    "type": "extract",
                    "parameters": {
                        "format": "html",
                        "input": {"$ref": "html"},
                        "selector": "article",
                        "fields": {
                            "external_tender_id": {"selector": ".", "attribute": "data-id"},
                            "title": {"selector": "h1"},
                            "source_url": {"selector": "a", "attribute": "href"},
                        },
                        "emit": True,
                    },
                },
            ],
        }
    )
    store = MemoryEvidenceStore()
    result = RecipeExecutor(
        ScriptedHttpAdapter(()),
        store,
        SourceSettings(),
        browser_factory=lambda: FakeBrowserAdapter(
            {
                "https://site.test/1": FakePage(
                    '<article data-id="1"><h1>Поставка 医疗</h1><a href="https://site.test/1">Detail</a></article>'
                )
            }
        ),
    ).execute(recipe, ExecutionContext(run_id=uuid4()))
    assert isinstance(result, CollectionRun)
    return deepcopy(dict(store.get(result.evidence_reference)))


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "corrupt",
        "changed",
        "interrupted",
        "omitted",
        "placeholder",
        "products",
        "legacy",
        "duplicate",
        "raw_changed",
    ],
)
def test_helper_saved_browser_evidence_and_field_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    browser_evidence: dict[str, Any],
    fault: str | None,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "verify_recipe", SKILL / "scripts/verify_recipe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **k: str(tmp_path))
    evidence = browser_evidence
    if fault == "missing":
        evidence["browser_steps"] = []
    elif fault == "corrupt":
        del evidence["browser_steps"][1]["output"]
    elif fault == "changed":
        evidence["browser_steps"][1]["output"] = evidence["browser_steps"][1]["output"].replace(
            "Поставка", "Changed"
        )
    elif fault == "duplicate":
        evidence["browser_steps"][1]["output"] *= 2
        evidence["observations"] *= 2
    elif fault == "interrupted":
        evidence["browser_steps"].pop()
    fields: dict[str, Any] = {"title": "Поставка 医疗", "source_url": "https://site.test/1"}
    available = list(fields)
    if fault in {"omitted", "placeholder", "products"}:
        available.append("product_items" if fault == "products" else "description")
    if fault == "placeholder":
        fields["description"] = None
    witness = {
        "records": [{"external_tender_id": "1", "fields": fields, "available_fields": available}]
    }
    if fault == "legacy":
        del witness["records"][0]["available_fields"]
    elif fault == "raw_changed":
        witness["records"][0]["raw_fields"] = {"title": "Incorrect"}
    for name, value in [
        ("recipe", evidence["recipe"]),
        ("settings", evidence["settings"]),
        ("evidence", evidence),
        ("witness", witness),
    ]:
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    for iteration in range(2 if fault is None else 1):
        output = tmp_path / ".codex" / str(iteration)
        monkeypatch.setattr(
            "sys.argv",
            [
                "verify_recipe",
                str(tmp_path / "recipe.json"),
                "--settings",
                str(tmp_path / "settings.json"),
                "--witness",
                str(tmp_path / "witness.json"),
                "--replay-evidence",
                str(tmp_path / "evidence.json"),
                "--output",
                str(output),
            ],
        )
        code = module.main()
        summary = json.loads((output / "summary.json").read_text())
        assert (code == 0) is (fault in {None, "legacy"}), summary
        assert summary["completeness_verified"] is (fault is None)
        if fault is None:
            assert summary["replay_exact"]
            (tmp_path / "evidence.json").write_text((output / "evidence.json").read_text())


def test_interrupted_browser_action_preserves_successful_steps(
    browser_evidence: dict[str, Any],
) -> None:
    class InterruptedBrowser(FakeBrowserAdapter):
        def content(self) -> str:
            raise TimeoutError("Interrupted acquisition")

    store = MemoryEvidenceStore()
    result = RecipeExecutor(
        ScriptedHttpAdapter(()),
        store,
        SourceSettings(),
        browser_factory=lambda: InterruptedBrowser({"https://site.test/1": FakePage("<article/>")}),
    ).execute(
        SourceRecipe.model_validate(browser_evidence["recipe"]), ExecutionContext(run_id=uuid4())
    )
    assert isinstance(result, SourceFailure)
    assert result.error_code == "TimeoutError"
    captured = store.get(result.failed_evidence_reference)
    assert isinstance(captured["browser_steps"], list)
    assert len(captured["browser_steps"]) == 1
    assert captured["observations"] == []
    assert isinstance(captured["events"], list)
    assert any(
        isinstance(event, dict) and event.get("capture_failed") is True
        for event in captured["events"]
    )
