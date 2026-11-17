from uuid import uuid4

import pytest
from pydantic import JsonValue

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.health import ConservativeRunHealth
from tender_system.collection.recipes import ExecutionError, RecipeExecutor, transforms
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import CollectionRun, ExecutionContext, SourceRecipe, SourceSnapshot
from tender_system.core.ports import HttpResponse
from tender_system.testing.fakes import ScriptedHttpAdapter


@pytest.mark.parametrize("required", [False, True])
def test_attachment_array_extraction_and_relative_urls(required: bool) -> None:
    from tender_system.collection.recipes import extract
    from tender_system.core.domain import ExtractStep

    definition = ExtractStep.model_validate(
        {
            "input": None,
            "format": "html",
            "selector": ".",
            "fields": {
                "document_urls": {
                    "selector": "a",
                    "attribute": "href",
                    "many": True,
                    "required": required,
                }
            },
        }
    )
    assert extract('<a href="/one.pdf">One</a><a href="two.docx">Two</a>', definition) == [
        {"document_urls": ["/one.pdf", "two.docx"]}
    ]
    assert transforms().get("urljoin")(
        ["/one.pdf", "two.docx"],
        {
            "base": "https://example.org/tenders/123",
        },
    ) == ["https://example.org/one.pdf", "https://example.org/tenders/two.docx"]
    if required:
        with pytest.raises(ExecutionError, match="required HTML field"):
            extract("<p>No files</p>", definition)
    else:
        assert extract("<p>No files</p>", definition) == [{"document_urls": []}]
    with pytest.raises(ExecutionError, match="HTML field"):
        extract("<a>Missing href</a>", definition)


def test_scalar_formatting_and_nullable_joining() -> None:
    registry = transforms()
    assert registry.get("format_scalar")(123, {"template": "https://fixture.test/{value}"}) == (
        "https://fixture.test/123"
    )
    assert registry.get("join_fields")([None, "District"], {"skip_null": True}) == "District"
    assert registry.get("join_fields")([None, None], {"skip_null": True}) == ""
    with pytest.raises(ExecutionError):
        registry.get("join_fields")([None, "District"], {})
    with pytest.raises(ExecutionError):
        registry.get("join_fields")([12], {"skip_null": True})


@pytest.mark.parametrize("value", [None, True, {}, []])
def test_scalar_formatting_refuses_missing_or_structured_identity(value: JsonValue) -> None:
    with pytest.raises(ExecutionError):
        transforms().get("format_scalar")(value, {"template": "https://fixture.test/{value}"})


@pytest.mark.parametrize(
    "template", ["{value.__class__}", "{value!r}", "{value}{value}", "no-value"]
)
def test_scalar_formatting_refuses_expression_or_ambiguous_templates(template: str) -> None:
    with pytest.raises(ExecutionError):
        transforms().get("format_scalar")(123, {"template": template})


@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_assert_requires_boolean_true(value: JsonValue) -> None:
    with pytest.raises(ExecutionError, match="source mismatch"):
        transforms().get("assert")(value, {"message": "source mismatch"})
    assert transforms().get("assert")(True, {}) is True


@pytest.mark.parametrize(
    "value,anchor",
    [
        (True, "2026-01-01T00:00:00Z"),
        ("100", "2026-01-01T00:00:00Z"),
        (100, "2026-01-01T00:00:00"),
        (100, "2026-01-01"),
        (100, "not a date"),
    ],
)
def test_countdown_transform_requires_explicit_numeric_seconds_and_aware_anchor(
    value: JsonValue, anchor: str
) -> None:
    with pytest.raises(ValueError):
        transforms().get("deadline_from_remaining")(value, {"observed_at": anchor})


def test_post_inventory_identity_includes_canonical_body_without_exposing_it() -> None:
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "fixture",
            "version": "1",
            "steps": [
                {
                    "type": "http_request",
                    "parameters": {
                        "url": "https://fixture.test/query",
                        "method": "POST",
                        "body": body,
                        "inventory_page": True,
                    },
                }
                for body in [
                    {"page": 1, "secret": "hidden"},
                    {"secret": "hidden", "page": 1},
                    {"page": 2},
                ]
            ],
        }
    )
    result = RecipeExecutor(
        ScriptedHttpAdapter(
            tuple(HttpResponse("https://fixture.test/query", 200, b"[]") for _ in range(3))
        ),
        MemoryEvidenceStore(),
        SourceSettings(),
    ).execute(recipe, ExecutionContext(run_id=uuid4()))
    assert isinstance(result, CollectionRun)
    first, repeated, next_page = result.diagnostics.pages
    assert first == repeated and first != next_page
    assert all(
        "hidden" not in page and "POST https://fixture.test/query" in page
        for page in result.diagnostics.pages
    )
    decision = ConservativeRunHealth().evaluate(result, SourceSettings(), SourceSnapshot(), ())
    assert not decision.publication_allowed
    assert "repeated_page_or_cursor" in {reason.code for reason in decision.reasons}
