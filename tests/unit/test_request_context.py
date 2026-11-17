"""Size limits must preserve source evidence and expose every omitted passage."""

from dataclasses import replace

import pytest

from tender_system.core.configuration import ModelSettings
from tender_system.core.ports import ModelFailure, ModelRequest
from tender_system.matching.request_context import bounded_context, request_bytes
from tender_system.matching.semantic_matching import source_quote


def request(document: str) -> ModelRequest:
    return ModelRequest(
        model="fixture",
        task="matching",
        payload={
            "instructions": "Evaluate tender relevance.",
            "tender": {
                "title": "Solar lights",
                "document_text": document,
                "requirements": "Supplier must provide warranty.",
                "conditions": "Payment after delivery.",
            },
            "campaign": {"description": "Solar lights with warranty."},
        },
        max_output_tokens=600,
    )


def test_large_ocr_whitespace_is_compacted_without_omission() -> None:
    original = (" " * 200 + "Solar lights must have warranty.\n\n") * 40
    result = bounded_context(request(original), ModelSettings(max_input_bytes=4000))
    assert request_bytes(result) <= 4000
    assert "document_selection" not in result.payload
    fields = result.payload["tender"]
    assert isinstance(fields, dict)
    assert source_quote(str(fields["document_text"]), original) is not None


def test_selects_requirements_from_document_tail_and_reports_omissions() -> None:
    original = "Unrelated background text. " * 5000 + "\nSolar lights must have warranty."
    result = bounded_context(request(original), ModelSettings(max_input_bytes=3000))
    assert request_bytes(result) <= 3000
    fields = result.payload["tender"]
    assert isinstance(fields, dict)
    assert "Solar lights must have warranty." in str(fields["document_text"])
    assert fields["requirements"] == "Supplier must provide warranty."
    selection = result.payload["document_selection"]
    assert isinstance(selection, dict) and selection["complete"] is False
    assert int(str(selection["included_passages"])) < int(str(selection["total_passages"]))


def test_never_discards_large_campaign_or_tender_conditions() -> None:
    original = request("Short document.")
    with pytest.raises(ModelFailure, match="request_too_large"):
        bounded_context(
            replace(
                original,
                payload={
                    **original.payload,
                    "campaign": {
                        "description": "Mandatory constraint. " * 5000,
                    },
                },
            ),
            ModelSettings(max_input_bytes=3000),
        )


def test_context_reserves_output_tokens_and_framing() -> None:
    with pytest.raises(ModelFailure, match="request_too_large"):
        bounded_context(request("Solar lights"), ModelSettings(context_window_tokens=4500))


def test_context_budget_changes_invalidate_cached_semantic_evaluations() -> None:
    from tender_system.core.configuration import CampaignSettings
    from tender_system.matching.matching import settings_digest

    settings = CampaignSettings(matcher_adapter="intelligent", matcher_version="3")
    changed = settings.model_copy(
        update={"model": settings.model.model_copy(update={"max_input_bytes": 24000})}
    )
    assert settings_digest(settings) != settings_digest(changed)
