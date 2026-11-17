"""Automatic recipes fail closed at acquisition and independent evidence boundaries."""

import json
import socket
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue

from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore
from tender_system.collection.network import (
    PublicHTTPTransport,
    PublicReadOnlyHttp,
    UnsafeWebsite,
    charge_public_bytes,
    charge_public_request,
    public_acquisition,
    public_url,
)
from tender_system.core.configuration import ModelSettings, SourceSettings
from tender_system.core.domain import ExecutionLimits, SourceRecipe
from tender_system.core.execution import RequestBudgetExceeded, ResponseBudgetExceeded
from tender_system.core.ports import HttpRequest, ModelRequest, ModelResponse
from tender_system.repair.recipe_generation import RecipeGenerator, model_json, prompt_observations
from tender_system.testing.fakes import FunctionModelClient


@pytest.mark.parametrize("repair", [False, True])
def test_generated_numeric_array_references_execute_without_paid_repair(repair: bool) -> None:
    from tender_system.collection.recipes import RecipeExecutor
    from tender_system.core.domain import CollectionRun, ExecutionContext

    proposal: dict[str, JsonValue] = {
        "steps": [
            {
                "type": "transform",
                "parameters": {
                    "name": "identity",
                    "input": [{"external_tender_id": "tender-1"}],
                    "output": "detail_rows",
                },
            },
            {
                "type": "transform",
                "parameters": {
                    "name": "identity",
                    "input": {
                        "external_tender_id": {"$ref": "detail_rows[0].external_tender_id"},
                        "title": "Router supply",
                        "source_url": "https://example.org/detail/1",
                    },
                    "emit": True,
                },
            },
        ]
    }
    adapter = HttpxAdapter(
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text="<h1>Listing</h1>"))
        )
    )
    evidence = MemoryEvidenceStore()
    generator = RecipeGenerator(
        adapter,
        lambda: pytest.fail("no browser needed"),
        FunctionModelClient(lambda _: ModelResponse({"recipe": proposal}, "fixture")),
        ModelSettings(provider="fixture", model="fixture"),
        evidence,
    )
    generated = (
        generator.revise(uuid4(), "new", {})
        if repair
        else generator.generate(uuid4(), "new", "https://example.org/tenders", SourceSettings())
    )
    assert generated.updated_recipe is not None
    result = RecipeExecutor(adapter, evidence, SourceSettings()).execute(
        generated.updated_recipe, ExecutionContext(run_id=uuid4())
    )
    assert isinstance(result, CollectionRun)
    assert result.raw_observations[0]["external_tender_id"] == "tender-1"


@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize("pseudo", [":-soup-contains", ":contains"])
def test_generated_multiword_text_selectors_preserve_source_identity(
    repair: bool, pseudo: str
) -> None:
    from tender_system.collection.recipes import RecipeExecutor
    from tender_system.core.domain import CollectionRun, ExecutionContext

    proposal: dict[str, JsonValue] = {
        "steps": [
            {"type": "http_request", "parameters": {"url": "https://example.org/tenders"}},
            {
                "type": "extract",
                "parameters": {
                    "format": "html",
                    "input": {"$ref": "response.body"},
                    "selector": ".",
                    "fields": {
                        "external_tender_id": {
                            "selector": f"dt{pseudo}(Reference Number) + dd",
                            "required": True,
                        },
                        "title": {"selector": "h1"},
                    },
                    "emit": True,
                },
            },
        ]
    }
    adapter = HttpxAdapter(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    text="<h1>Router supply</h1><dl><dt>Reference Number</dt><dd>RFQ-123</dd></dl>",
                )
            )
        )
    )
    evidence = MemoryEvidenceStore()
    generator = RecipeGenerator(
        adapter,
        lambda: pytest.fail("no browser needed"),
        FunctionModelClient(lambda _: ModelResponse({"recipe": proposal}, "fixture")),
        ModelSettings(provider="fixture", model="fixture"),
        evidence,
    )
    generator.allowed.add("https://example.org/tenders")
    generated = (
        generator.revise(uuid4(), "new", {})
        if repair
        else generator.generate(uuid4(), "new", "https://example.org/tenders", SourceSettings())
    )
    assert generated.updated_recipe is not None
    result = RecipeExecutor(adapter, evidence, SourceSettings()).execute(
        generated.updated_recipe, ExecutionContext(run_id=uuid4())
    )
    assert isinstance(result, CollectionRun)
    assert result.raw_observations[0]["external_tender_id"] == "RFQ-123"


def test_numeric_reference_canonicalization_preserves_nested_index_semantics() -> None:
    from tender_system.collection.recipes import resolve
    from tender_system.repair.recipe_generation import canonicalize_references

    value: JsonValue = {"rows": [{"$ref": "payload[0].items[1].id"}]}
    canonicalize_references(value)
    assert value == {"rows": [{"$ref": "payload.0.items.1.id"}]}
    assert resolve(value, {"payload": [{"items": [{"id": "first"}, {"id": "second"}]}]}) == {
        "rows": ["second"]
    }


@pytest.mark.parametrize(
    "selector",
    [
        'dt:-soup-contains("Reference Number") + dd',
        "dt:-soup-contains('Reference Number') + dd",
        "dt:-soup-contains(Reference, Number) + dd",
        r"dt:-soup-contains(Reference\ Number) + dd",
        "a[href*='Reference Number']",
        'a[data-example=":-soup-contains(Reference Number)"]',
    ],
)
def test_selector_canonicalization_preserves_explicit_css_semantics(selector: str) -> None:
    from tender_system.repair.recipe_generation import canonicalize_text_selectors

    value: JsonValue = {"selector": selector, "description": "dt:-soup-contains(A phrase)"}
    canonicalize_text_selectors(value)
    assert value == {"selector": selector, "description": "dt:-soup-contains(A phrase)"}


def test_authoring_schema_compacts_metadata_without_removing_contract_fields() -> None:
    from tender_system.repair.recipe_generation import authoring_schema

    original = SourceRecipe.model_json_schema()
    compact = authoring_schema()
    assert isinstance(compact, dict)
    properties = compact["properties"]
    assert isinstance(properties, dict)
    assert properties.keys() == original["properties"].keys()
    assert compact["required"] == original["required"]
    assert len(json.dumps(compact)) < len(json.dumps(original)) * 0.75


@pytest.mark.parametrize("reference", ["rows[*].id", "rows[-1].id", "rows[0 + 1].id"])
def test_reference_canonicalization_does_not_interpret_expressions(reference: str) -> None:
    from tender_system.repair.recipe_generation import canonicalize_references

    value: JsonValue = {"$ref": reference}
    canonicalize_references(value)
    assert value == {"$ref": reference}


def test_generation_fallback_paces_the_selected_groq_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tender_system.core.configuration import load_configuration
    from tender_system.operator.operator_runtime import OperatorRuntime

    config = load_configuration(
        environ={"QUARRY_MATCHER__PROVIDER": "groq", "QUARRY_MATCHER__MODEL": "fixture"},
        validate_models=False,
    )
    from contextlib import nullcontext

    from tender_system.operator import ai_settings

    monkeypatch.setattr(ai_settings, "assignment", lambda session, role, fallback: fallback)
    runtime = Mock(spec=OperatorRuntime)
    runtime.sessions = Mock(return_value=nullcontext())
    settings, _ = OperatorRuntime.generation_model(runtime, SourceSettings(), config)
    assert settings.provider == "groq"
    assert settings.request_interval_seconds == 61
    explicit, _ = OperatorRuntime.generation_model(
        runtime,
        SourceSettings(fixer_model=ModelSettings(provider="groq", model="explicit")),
        config,
    )
    assert explicit.model == "explicit" and explicit.request_interval_seconds == 61


def test_singleton_generation_envelope_does_not_become_an_unobserved_url_action() -> None:
    envelope: dict[str, JsonValue] = {"actions": [], "recipe": {"steps": []}}
    client = FunctionModelClient(lambda _: ModelResponse({"actions": [envelope]}, "fixture"))
    result = model_json(client, ModelSettings(), "recipe_generation", {})
    assert result == envelope
    # Unwrapping must not grant schema validity to an empty or unsafe recipe.
    with pytest.raises(ValueError):
        SourceRecipe.model_validate(result["recipe"])


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "192.0.2.2"]
)
def test_private_and_reserved_networks_rejected(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", (address, 443))]
    )
    with pytest.raises(UnsafeWebsite, match="Private, local"):
        public_url("https://example.org/tenders")


def test_redirect_destination_checked_before_request_and_shared_caps() -> None:
    requests: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    def check(url: str) -> None:
        if "127.0.0.1" in url:
            raise UnsafeWebsite("Private destination")

    adapter = PublicReadOnlyHttp(
        HttpxAdapter(httpx.Client(transport=httpx.MockTransport(send))), check
    )
    with pytest.raises(UnsafeWebsite, match="Private"):
        adapter.request(HttpRequest("https://public.example/tenders"))
    assert requests == ["https://public.example/tenders"]
    with public_acquisition(ExecutionLimits(max_requests=1, max_total_response_bytes=2)):
        charge_public_request()
        with pytest.raises(RequestBudgetExceeded):
            charge_public_request()
        charge_public_bytes(2)
        with pytest.raises(ResponseBudgetExceeded):
            charge_public_bytes(1)


def test_public_transport_pins_validated_address_and_preserves_tls_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions: list[str] = []
    sent: list[httpx.Request] = []

    def resolve(host: str, port: int, **kwargs: object) -> list[tuple[object, ...]]:
        resolutions.append(host)
        # A later hostname resolution would rebind to loopback.
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("93.184.215.14" if len(resolutions) == 1 else "127.0.0.1", port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    def send(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        assert request.url.host == "93.184.215.14"
        assert request.headers["host"] == "example.org"
        assert request.extensions["sni_hostname"] == "example.org"
        return httpx.Response(200, content=b"public")

    with httpx.Client(transport=PublicHTTPTransport(httpx.MockTransport(send))) as client:
        response = HttpxAdapter(client).request(HttpRequest("https://example.org/tenders"))
    assert response.url == "https://example.org/tenders"
    assert response.body == b"public"
    assert resolutions == ["example.org"] and len(sent) == 1


def test_public_transport_rechecks_dns_before_connecting_after_outer_policy_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def resolve(host: str, port: int, **kwargs: object) -> list[tuple[object, ...]]:
        nonlocal calls
        calls += 1
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("93.184.215.14" if calls == 1 else "127.0.0.1", port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with httpx.Client(
        transport=PublicHTTPTransport(
            httpx.MockTransport(lambda _: pytest.fail("private address must not reach transport"))
        )
    ) as client:
        with pytest.raises(UnsafeWebsite, match="Private, local"):
            PublicReadOnlyHttp(HttpxAdapter(client)).request(HttpRequest("https://example.org"))
    assert calls == 2


def test_mutation_requests_and_credentials_rejected_without_network() -> None:
    adapter = PublicReadOnlyHttp(HttpxAdapter(), lambda _: None)
    with pytest.raises(UnsafeWebsite, match="GET/HEAD"):
        adapter.request(HttpRequest("https://example.org", method="POST", body=b"{}"))
    with pytest.raises(ValueError):
        public_url("https://user:secret@example.org")


def test_prompt_bound_includes_links_and_multiple_pages() -> None:
    observations = [
        {
            "url": "https://example.org",
            "body": "А" * 16000,
            "links": ["https://example.org/" + "a" * 500 for _ in range(80)],
        }
    ] * 4
    bounded = prompt_observations(cast(list[JsonValue], observations), 6000)
    assert len(json.dumps(bounded, ensure_ascii=False).encode()) <= 6000
    assert all(isinstance(item, dict) and item["truncated"] is True for item in bounded)


@pytest.mark.parametrize("already_truncated", [False, True])
def test_prompt_preserves_short_complete_detail_when_listings_need_truncation(
    already_truncated: bool,
) -> None:
    detail = "<main>" + "<div>Label and value</div>" * 60 + "<p>Critical requirements</p></main>"
    observations: list[dict[str, JsonValue]] = [
        {"url": "https://example.org/tenders", "body": "Listing " * 1300, "links": []},
        {"url": "https://example.org/detail/1", "body": detail, "links": []},
        {"url": "https://example.org/tenders?page=2", "body": "Listing " * 1300, "links": []},
    ]
    if already_truncated:
        for listing in (observations[0], observations[2]):
            listing["truncated"] = True
            listing["links"] = ["https://example.org/" + "tender-slug-" * 14] * 8
    bounded = prompt_observations(cast(list[JsonValue], observations), 6000)
    record = bounded[1]
    assert isinstance(record, dict)
    assert record["body"] == detail
    assert record["truncated"] is False
    assert len(json.dumps(bounded, ensure_ascii=False).encode()) <= 6000


def test_html_listing_detail_and_next_are_ready_for_generation() -> None:
    calls: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            text='<html><section role="main"><h4><a href="/detail/1">Router</a></h4>'
            '<h4><a href="/detail/2">Switch</a></h4>'
            '<nav><a rel="next" href="?page=2">Next</a></nav></section></html>',
        )

    def model(request: ModelRequest) -> ModelResponse:
        assert "Generate the simplest supported recipe now" in str(request.payload["instructions"])
        observations = request.payload["observations"]
        assert isinstance(observations, list) and len(observations) == 3
        first = observations[0]
        assert isinstance(first, dict)
        links = first["links"]
        assert isinstance(links, list) and links[0] == "https://example.org/tenders?page=2"
        return ModelResponse({"recipe": None, "actions": [], "reason": "Unsupported"}, "fixture")

    generator = RecipeGenerator(
        HttpxAdapter(httpx.Client(transport=httpx.MockTransport(send))),
        lambda: pytest.fail("no browser needed"),
        FunctionModelClient(model),
        ModelSettings(provider="fixture", model="fixture"),
        MemoryEvidenceStore(),
    )
    with pytest.raises(UnsafeWebsite, match="No reliable"):
        generator.generate(uuid4(), "new", "https://example.org/tenders", SourceSettings())
    assert calls == [
        "https://example.org/tenders",
        "https://example.org/detail/1",
        "https://example.org/tenders?page=2",
    ]


def test_nested_cms_listing_ignores_sidebar_and_prefetches_single_tender() -> None:
    calls: list[str] = []
    listing = (
        '<html><div id="block-mainpagecontent"><aside><h2><a href="/">Home</a>'
        '</h2></aside><section role="main"><h4><a href="/detail/1">Router</a></h4>'
        '<a rel="next" href="?page=2">Next</a></section></div></html>'
    )

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=listing)

    def model(request: ModelRequest) -> ModelResponse:
        assert "Generate the simplest supported recipe now" in str(request.payload["instructions"])
        observations = request.payload["observations"]
        assert isinstance(observations, list)
        first = observations[0]
        assert isinstance(first, dict) and "<aside>" not in str(first["body"])
        assert calls == [
            "https://example.org/tenders",
            "https://example.org/detail/1",
            "https://example.org/tenders?page=2",
        ]
        return ModelResponse({"recipe": None, "actions": [], "reason": "Unsupported"}, "fixture")

    generator = RecipeGenerator(
        HttpxAdapter(httpx.Client(transport=httpx.MockTransport(send))),
        lambda: pytest.fail("no browser needed"),
        FunctionModelClient(model),
        ModelSettings(provider="fixture", model="fixture"),
        MemoryEvidenceStore(),
    )
    with pytest.raises(UnsafeWebsite, match="No reliable"):
        generator.generate(uuid4(), "new", "https://example.org/tenders", SourceSettings())


def test_independent_detail_scope_supports_semantic_main_without_hiding_truncation() -> None:
    from tender_system.repair.recipe_generation import page_evidence

    html = (
        "<html><body><nav>" + "Navigation " * 2000 + "</nav>"
        '<section role="main"><h1>Real tender</h1><p>Complete details.</p></section>'
        "</body></html>"
    )
    witness = page_evidence("https://example.org/detail/1", html, 1000)
    assert witness["truncated"] is False
    assert "Complete details." in str(witness["body"])
    oversized = html.replace("Complete details.", "Requirements " * 2000)
    assert page_evidence("https://example.org/detail/1", oversized, 1000)["truncated"] is True


def test_unobserved_urls_and_executable_steps_rejected() -> None:
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "new",
            "version": "1",
            "coverage": "incremental",
            "steps": [
                {"type": "http_request", "parameters": {"url": "https://unobserved.example/api"}}
            ],
        }
    )
    with pytest.raises(UnsafeWebsite, match="without investigation"):
        RecipeGenerator.check_recipe(recipe, {"https://example.org"})
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "new",
            "version": "1",
            "coverage": "incremental",
            "steps": [{"type": "custom", "parameters": {"name": "arbitrary"}}],
        }
    )
    with pytest.raises(UnsafeWebsite, match="plugins"):
        RecipeGenerator.check_recipe(recipe, {"https://example.org"})


def test_unsupported_reason_retains_evidence_without_acceptance() -> None:
    evidence = MemoryEvidenceStore()
    adapter = HttpxAdapter(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="<h1>Sign in</h1>")
            )
        )
    )
    generator = RecipeGenerator(
        adapter,
        lambda: pytest.fail("no browser requested"),
        FunctionModelClient(
            lambda _: ModelResponse(
                {"recipe": None, "actions": [], "reason": "Login required"}, "fixture"
            )
        ),
        ModelSettings(provider="fixture", model="fixture"),
        evidence,
    )
    with pytest.raises(UnsafeWebsite, match="No reliable") as failure:
        generator.generate(uuid4(), "new", "https://example.org", SourceSettings())
    references = failure.value.evidence_references
    assert len(references) == 4
    assert evidence.get(references[-2])["reason"] == "Login required"


def test_investigation_keeps_tenders_ahead_of_asset_and_form_noise() -> None:
    from tender_system.repair.recipe_generation import page_evidence

    html = (
        "<html><head>"
        + "".join(f'<link href="/asset-{i}.css"/>' for i in range(100))
        + "</head><body><main><form><select>"
        + "".join(f"<option>{i}</option>" for i in range(1000))
        + '</select></form><article><a href="/detail/123">Real tender</a></article>'
        + "</main></body></html>"
    )
    evidence = page_evidence("https://example.org", html, 6000, investigation=True)
    assert "Real tender" in str(evidence["body"])
    assert "https://example.org/detail/123" in cast(list[JsonValue], evidence["links"])
    # Blind witness content must keep its original completeness boundary.
    witness = page_evidence("https://example.org", html, 6000)
    assert witness["truncated"] is True


def test_discovery_chrome_cannot_crowd_out_detail_fields_or_pagination() -> None:
    from tender_system.repair.recipe_generation import page_evidence

    html = (
        '<html><main><div class="page__breadcrumbs"><nav>'
        + "Navigation " * 1000
        + '</nav></div><ul class="social-media"><li>Share</li></ul>'
        '<h1>Supply routers</h1><span class="reference">RFQ-123</span>'
        '<nav class="pager"><a rel="next" href="?page=2">Next</a></nav>'
        "</main></html>"
    )
    evidence = page_evidence("https://example.org/tenders", html, 1000, investigation=True)
    assert "RFQ-123" in str(evidence["body"])
    assert 'rel="next"' in str(evidence["body"])
    assert evidence["truncated"] is False
    assert page_evidence("https://example.org/tenders", html, 1000)["truncated"] is True


def test_browser_manifest_is_not_prefetched_as_tender_api() -> None:
    urls: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path in {"/manifest.json", "/browserconfig.xml"}:
            return httpx.Response(404)
        return httpx.Response(
            200,
            text='<link rel="manifest" href="/manifest.json">'
            '<meta name="msapplication-config" content="/browserconfig.xml"><h1>Tenders</h1>',
        )

    evidence = MemoryEvidenceStore()
    generator = RecipeGenerator(
        HttpxAdapter(httpx.Client(transport=httpx.MockTransport(send))),
        lambda: pytest.fail("no browser requested"),
        FunctionModelClient(
            lambda _: ModelResponse(
                {"recipe": None, "actions": [], "reason": "No tenders"}, "fixture"
            )
        ),
        ModelSettings(provider="fixture", model="fixture"),
        evidence,
    )
    with pytest.raises(UnsafeWebsite, match="No reliable"):
        generator.generate(uuid4(), "new", "https://example.org", SourceSettings())
    assert urls == ["https://example.org"]


def test_investigation_prioritizes_role_main_and_h4_tender_links() -> None:
    from tender_system.repair.recipe_generation import page_evidence

    html = (
        '<html><body><aside><h2><a href="/">Navigation</a></h2></aside>'
        '<section role="main"><article><h4><a href="/tender/123">Real tender</a>'
        "</h4></article></section></body></html>"
    )
    evidence = page_evidence("https://example.org", html, 6000, investigation=True)
    assert "Navigation" not in str(evidence["body"])
    assert cast(list[JsonValue], evidence["links"])[0] == "https://example.org/tender/123"


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing_file",
        "wrong_file",
        "wrong_id",
        "browser_failed",
        "missing_requirement",
        "wrong_condition",
    ],
)
def test_blind_attachment_arrays_preserve_independent_safeguards(fault: str | None) -> None:
    from datetime import UTC, datetime

    from tender_system.collection.normalization import ConfigurableTenderNormalizer
    from tender_system.collection.verification import BrowserWitnesses
    from tender_system.core.domain import CollectionRun, SourceRecord, TenderCandidate
    from tender_system.repair.recipe_generation import FIELDS, GeneratedRecipeWitnesses
    from tender_system.testing.fakes import FakeBrowserAdapter, FakePage

    url = "https://example.org/tender/123"
    html = (
        '<h1>Network tender</h1><div id="identity"><b>Reference No</b>123</div>'
        '<section id="attachments"><a href="/one.pdf">One</a>'
        '<a href="two.docx">Two</a></section>'
        '<p id="requirements">Install network equipment</p>'
        '<p id="conditions">Delivery within 30 days</p>'
    )

    def browsers() -> FakeBrowserAdapter:
        return FakeBrowserAdapter(
            {url: FakePage(html)},
            {"navigate": ConnectionError()} if fault == "browser_failed" else {},
        )

    urls = ["https://example.org/one.pdf", "https://example.org/tender/two.docx"]
    if fault == "missing_file":
        urls.pop()
    if fault == "wrong_file":
        urls[-1] = "https://example.org/wrong.pdf"
    candidate = TenderCandidate(
        source_id="s",
        external_tender_id="456" if fault == "wrong_id" else "123",
        source_url=url,
        title="Network tender",
        document_urls=tuple(urls),
        raw_data={
            "requirements": None if fault == "missing_requirement" else "Install network equipment",
            "conditions": "Delivery within 90 days"
            if fault == "wrong_condition"
            else "Delivery within 30 days",
        },
    )
    now = datetime.now(UTC)
    run = CollectionRun(
        id=uuid4(),
        source_id="s",
        recipe_version="1",
        started_at=now,
        finished_at=now,
        complete=True,
        evidence_reference="test",
        candidates=(candidate,),
    )

    def inventory(request: ModelRequest) -> ModelResponse:
        assert "candidates" not in request.payload and "recipe" not in request.payload
        return ModelResponse(
            {
                "fields": {
                    "external_tender_id": {"selector": "#identity", "direct_text": True},
                    "title": {"selector": "h1"},
                    "requirements": {"selector": "#requirements"},
                    "conditions": {"selector": "#conditions"},
                    "document_urls": {
                        "selector": "#attachments a",
                        "attribute": "href",
                        "many": True,
                    },
                },
                "unavailable_fields": {
                    name: "source_absent"
                    for name in FIELDS
                    if name not in {"title", "document_urls", "requirements", "conditions"}
                },
            },
            "fixture",
        )

    evidence = MemoryEvidenceStore()
    verifier = GeneratedRecipeWitnesses(
        browsers,
        FunctionModelClient(inventory),
        ModelSettings(provider="fixture", model="fixture"),
        ConfigurableTenderNormalizer(),
        evidence,
        BrowserWitnesses(browsers, evidence),
    )
    outcome = verifier.verify(run, SourceRecord(id="s", name="Source"), SourceSettings())
    if fault:
        assert outcome.mismatches or not outcome.checked
    else:
        assert outcome.checked == 1 and not outcome.mismatches


def test_investigation_preserves_listing_wrapped_in_form() -> None:
    from tender_system.repair.recipe_generation import page_evidence

    html = (
        '<html><body><form><main><a href="/tender/7">Supply hospital equipment'
        "</a></main></form></body></html>"
    )
    observation = page_evidence("https://example.org", html, 16000, investigation=True)
    assert "Supply hospital equipment" in str(observation["body"])
    assert "https://example.org/tender/7" in cast(list[JsonValue], observation["links"])


def test_optional_api_failure_preserves_html_and_reuses_fetched_evidence() -> None:
    calls: list[str] = []
    turns: list[ModelRequest] = []
    listing = (
        '<html><body><form><main><h2><a href="/detail/1">Supply routers</a></h2>'
        '<a href="/old-api.json">Old API</a><a rel="next" href="?page=2">Next</a>'
        "</main></form></body></html>"
    )

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404 if request.url.path == "/old-api.json" else 200, text=listing)

    def model(request: ModelRequest) -> ModelResponse:
        turns.append(request)
        assert "Supply routers" in str(request.payload["observations"])
        if len(turns) == 1:
            return ModelResponse(
                {"actions": [{"kind": "get", "url": "https://example.org/detail/1"}]}, "fixture"
            )
        return ModelResponse({"actions": [], "reason": "Unsupported"}, "fixture")

    generator = RecipeGenerator(
        HttpxAdapter(httpx.Client(transport=httpx.MockTransport(send))),
        lambda: pytest.fail("no browser"),
        FunctionModelClient(model),
        ModelSettings(provider="fixture", model="fixture"),
        MemoryEvidenceStore(),
    )
    with pytest.raises(UnsafeWebsite, match="No reliable"):
        generator.generate(uuid4(), "new", "https://example.org/tenders", SourceSettings())
    assert len(turns) == 2
    assert calls.count("https://example.org/detail/1") == 1
    assert any(isinstance(item, dict) and item.get("error") for item in generator.observations)


def test_optional_failed_endpoint_does_not_hide_other_observed_api() -> None:
    urls: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path == "/a-old.json":
            return httpx.Response(404)
        if request.url.path == "/z-tenders.json":
            return httpx.Response(200, json={"items": [{"title": "Supply routers"}]})
        return httpx.Response(
            200,
            text=(
                '<html><main><a href="/a-old.json">Old API</a>'
                '<a href="/z-tenders.json">Tender API</a></main></html>'
            ),
        )

    def model(request: ModelRequest) -> ModelResponse:
        assert "Supply routers" in str(request.payload["observations"])
        return ModelResponse({"actions": [], "reason": "Unsupported"}, "fixture")

    generator = RecipeGenerator(
        HttpxAdapter(httpx.Client(transport=httpx.MockTransport(send))),
        lambda: pytest.fail("no browser"),
        FunctionModelClient(model),
        ModelSettings(provider="fixture", model="fixture"),
        MemoryEvidenceStore(),
    )
    with pytest.raises(UnsafeWebsite, match="No reliable"):
        generator.generate(uuid4(), "new", "https://example.org", SourceSettings())
    assert urls == [
        "https://example.org",
        "https://example.org/a-old.json",
        "https://example.org/z-tenders.json",
    ]
