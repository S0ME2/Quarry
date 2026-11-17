import json
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import JsonValue, ValidationError

from tender_system.collection.adapters import FileEvidenceStore, MemoryEvidenceStore
from tender_system.collection.recipes import FileRecipeLoader, RecipeExecutor, resolve, transforms
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    CollectionRun,
    ExecutionContext,
    SourceFailure,
    SourceRecipe,
    SourceSearch,
)
from tender_system.core.ports import HttpRequest, HttpResponse, Transform
from tender_system.core.registry import Registry
from tender_system.testing.fakes import FakeBrowserAdapter, FakePage, ScriptedHttpAdapter


def recipe(steps: list[dict[str, JsonValue]], **options: JsonValue) -> SourceRecipe:
    return SourceRecipe.model_validate(
        {"source_id": "s", "version": "1", "steps": steps, **options}
    )


def step(kind: str, **parameters: JsonValue) -> dict[str, JsonValue]:
    return {"type": kind, "parameters": parameters}


def ref(path: str) -> dict[str, JsonValue]:
    return {"$ref": path}


def execute(
    definition: SourceRecipe,
    responses: list[HttpResponse | Exception] | None = None,
    *,
    plugins: Registry[Transform] | None = None,
) -> tuple[CollectionRun | SourceFailure, MemoryEvidenceStore, ScriptedHttpAdapter]:
    evidence = MemoryEvidenceStore()
    http = ScriptedHttpAdapter(tuple(responses or []))
    executor = RecipeExecutor(
        http, evidence, SourceSettings(), plugins=plugins, sleep=lambda _: None
    )
    return executor.execute(definition, ExecutionContext(run_id=uuid4())), evidence, http


def pagination_recipe() -> SourceRecipe:
    return recipe(
        [
            step("transform", name="identity", input=1, output="page"),
            {
                "type": "loop",
                "parameters": {"until": ref("done"), "max_iterations": 3},
                "steps": [
                    step(
                        "http_request", url="https://fixture.test/api", query={"page": ref("page")}
                    ),
                    step("extract", input=ref("response.body"), format="json", output="document"),
                    step(
                        "transform",
                        name="get",
                        input=ref("document"),
                        arguments={"path": "items"},
                        emit=True,
                    ),
                    step(
                        "transform",
                        name="get",
                        input=ref("document"),
                        arguments={"path": "done"},
                        output="done",
                    ),
                    step(
                        "transform",
                        name="add",
                        input=ref("page"),
                        arguments={"other": 1},
                        output="page",
                    ),
                ],
            },
        ]
    )


def response(payload: JsonValue, status: int = 200) -> HttpResponse:
    return HttpResponse("https://fixture.test/api", status, json.dumps(payload).encode())


def test_api_pagination_and_evidence() -> None:
    result, evidence, http = execute(
        pagination_recipe(),
        [
            response({"items": [{"external_tender_id": "1", "title": "One"}], "done": False}),
            response({"items": [{"external_tender_id": "2", "title": "Two"}], "done": True}),
        ],
    )
    assert isinstance(result, CollectionRun)
    assert result.complete and len(result.raw_observations) == 2
    assert evidence.get(result.evidence_reference)["recipe"] == pagination_recipe().model_dump(
        mode="json"
    )
    assert evidence.get(result.evidence_reference)["settings"] == SourceSettings().model_dump(
        mode="json"
    )
    assert [request.url for request in http.requests] == [
        "https://fixture.test/api?page=1",
        "https://fixture.test/api?page=2",
    ]
    assert evidence.get(result.evidence_reference)["observations"] == list(result.raw_observations)


def test_partial_pagination_keeps_evidence_but_never_returns_complete_run() -> None:
    result, evidence, _ = execute(
        pagination_recipe(),
        [
            response({"items": [{"external_tender_id": "1"}], "done": False}),
            TimeoutError(),
            TimeoutError(),
            TimeoutError(),
        ],
    )
    assert isinstance(result, SourceFailure)
    assert result.failed_step and "http_request" in result.failed_step
    assert evidence.get(result.failed_evidence_reference)["observations"] == [
        {"external_tender_id": "1"}
    ]


def test_unterminated_pagination_is_failure() -> None:
    result, _, http = execute(pagination_recipe(), [response({"items": [], "done": False})] * 3)
    assert isinstance(result, SourceFailure)
    assert len(http.requests) == 3


def test_xml_extraction_with_namespaces_nullable_fields() -> None:
    xml = '<feed xmlns="urn:t"><t id="7"><name>Equipment</name></t></feed>'
    result, _, _ = execute(
        recipe(
            [
                step(
                    "extract",
                    input=xml,
                    format="xml",
                    selector="t:t",
                    namespaces={"t": "urn:t"},
                    fields={
                        "external_tender_id": {"attribute": "id", "required": True},
                        "title": {"selector": "t:name", "required": True},
                        "deadline": {"selector": "t:deadline"},
                    },
                    emit=True,
                )
            ]
        )
    )
    assert isinstance(result, CollectionRun)
    assert result.raw_observations == (
        {"external_tender_id": "7", "title": "Equipment", "deadline": None},
    )


def test_xml_entity_and_dtd_are_rejected() -> None:
    result, _, _ = execute(
        recipe([step("extract", format="xml", input='<!DOCTYPE a [<!ENTITY e "boom">]><a>&e;</a>')])
    )
    assert isinstance(result, SourceFailure)


def test_html_list_detail_loop_and_urljoin() -> None:
    definition = recipe(
        [
            step("http_request", url="https://fixture.test/list"),
            step(
                "extract",
                format="html",
                input=ref("response.body"),
                selector="a.tender",
                fields={"href": {"attribute": "href", "required": True}},
            ),
            {
                "type": "loop",
                "parameters": {"items": ref("items")},
                "steps": [
                    step(
                        "transform",
                        name="urljoin",
                        input=ref("item.href"),
                        arguments={"base": "https://fixture.test/list"},
                        output="url",
                    ),
                    step("http_request", url=ref("url")),
                    step(
                        "extract",
                        format="html",
                        input=ref("response.body"),
                        selector="article",
                        fields={
                            "external_tender_id": {"attribute": "data-id", "required": True},
                            "title": {"selector": "h1", "required": True},
                        },
                        emit=True,
                    ),
                ],
            },
        ]
    )
    result, _, http = execute(
        definition,
        [
            HttpResponse(
                "https://fixture.test/list", 200, b'<a class="tender" href="/detail/1">One</a>'
            ),
            HttpResponse(
                "https://fixture.test/detail/1",
                200,
                b'<article data-id="1"><h1>Computers</h1></article>',
            ),
        ],
    )
    assert isinstance(result, CollectionRun)
    assert result.raw_observations == ({"external_tender_id": "1", "title": "Computers"},)
    assert http.requests[-1].url == "https://fixture.test/detail/1"


@pytest.mark.parametrize("predicate,expected", [(True, "yes"), (False, "no")])
def test_if_else_and_transform(predicate: bool, expected: str) -> None:
    result, _, _ = execute(
        recipe(
            [
                {
                    "type": "condition",
                    "parameters": {"predicate": predicate},
                    "steps": [
                        step("transform", name="identity", input={"title": "yes"}, emit=True)
                    ],
                    "else_steps": [
                        step("transform", name="identity", input={"title": "no"}, emit=True)
                    ],
                }
            ]
        )
    )
    assert isinstance(result, CollectionRun)
    assert result.raw_observations[0]["title"] == expected


def test_custom_plugin_and_failure_isolation_between_runs() -> None:
    plugins: Registry[Transform] = Registry()

    def custom(value: JsonValue, parameters: Mapping[str, JsonValue]) -> JsonValue:
        if parameters.get("fail"):
            raise RuntimeError("secret detail should not enter failure message")
        return {"title": value}

    plugins.register("source_specific", custom)
    bad, _, _ = execute(
        recipe(
            [step("custom", name="source_specific", input="x", arguments={"fail": True}, emit=True)]
        ),
        plugins=plugins,
    )
    good, _, _ = execute(
        recipe([step("custom", name="source_specific", input="x", emit=True)]), plugins=plugins
    )
    assert isinstance(bad, SourceFailure) and "secret" not in bad.error_detail
    assert isinstance(good, CollectionRun) and good.raw_observations == ({"title": "x"},)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "unknown", "parameters": {}},
        step("http_request"),
        step("loop"),
        {
            "type": "loop",
            "parameters": {"items": []},
            "else_steps": [step("transform", name="identity")],
        },
        {"type": "transform", "parameters": {"name": "identity", "typo": True}},
        {"type": "extract", "parameters": {"input": "x", "format": "yaml"}},
    ],
)
def test_malformed_recipes_rejected(bad: dict[str, JsonValue]) -> None:
    with pytest.raises(ValidationError):
        recipe([bad])


@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionError(), OSError()])
def test_transport_retries(failure: Exception) -> None:
    result, evidence, http = execute(
        recipe([step("http_request", url="https://fixture.test")]), [failure, response([])]
    )
    assert isinstance(result, CollectionRun)
    assert len(http.requests) == 2
    assert http.requests[0].timeout_seconds == 30
    assert len(evidence.get(result.evidence_reference)["events"]) == 2  # type: ignore[arg-type]


@pytest.mark.parametrize("status,attempts", [(404, 1), (401, 1), (429, 3), (500, 3), (503, 3)])
def test_http_status_failures(status: int, attempts: int) -> None:
    result, _, http = execute(
        recipe([step("http_request", url="https://fixture.test")]),
        [response({}, status)] * attempts,
    )
    assert isinstance(result, SourceFailure) and len(http.requests) == attempts


def test_non_idempotent_http_action_is_not_blindly_retried() -> None:
    result, _, http = execute(
        recipe([step("http_request", url="https://fixture.test", method="POST", body={"x": 1})]),
        [TimeoutError()],
    )
    assert isinstance(result, SourceFailure) and len(http.requests) == 1


@pytest.mark.parametrize(
    "steps",
    [
        [step("transform", name="identity", input=ref("missing"), emit=True)],
        [step("transform", name="unknown")],
        [step("transform", name="identity", input="not an object", emit=True)],
        [
            {
                "type": "condition",
                "parameters": {"predicate": "truthy"},
                "steps": [step("transform", name="identity")],
            }
        ],
        [step("extract", format="html", input="<div>login</div>", selector="article")],
        [
            step(
                "extract",
                format="html",
                input="<article></article>",
                selector="article",
                fields={"id": {"selector": "a", "required": True}},
            )
        ],
    ],
)
def test_silent_corruption_paths_fail(steps: list[dict[str, JsonValue]]) -> None:
    result, _, _ = execute(recipe(steps))
    assert isinstance(result, SourceFailure)


def test_json_and_toml_load_into_same_typed_model(tmp_path: Path) -> None:
    definition = recipe([step("http_request", url="https://fixture.test")])
    json_path = tmp_path / "recipe.json"
    json_path.write_text(definition.model_dump_json())
    toml_path = tmp_path / "recipe.toml"
    toml_path.write_text(
        'source_id = "s"\nversion = "1"\n[[steps]]\ntype = "http_request"\n'
        '[steps.parameters]\nurl = "https://fixture.test"\n'
    )
    loader = FileRecipeLoader()
    assert loader.load(str(json_path)) == loader.load(str(toml_path)) == definition


def test_evidence_implementations_are_interchangeable_and_isolated(tmp_path: Path) -> None:
    for store in (MemoryEvidenceStore(), FileEvidenceStore(tmp_path)):
        payload: dict[str, JsonValue] = {"rows": [{"title": "original"}]}
        reference = store.put(uuid4(), payload)
        payload["rows"] = []
        assert store.get(reference)["rows"] == [{"title": "original"}]
    with pytest.raises(ValueError):
        FileEvidenceStore(tmp_path).get("../secret")


def test_fake_browser_execution_contract() -> None:
    browser = FakeBrowserAdapter({"https://fixture.test": FakePage(html="<article>One</article>")})
    executor = RecipeExecutor(
        ScriptedHttpAdapter(()),
        MemoryEvidenceStore(),
        SourceSettings(),
        browser_factory=lambda: browser,
    )
    result = executor.execute(
        recipe(
            [
                step("browser_action", action="navigate", value="https://fixture.test"),
                step("browser_action", action="content"),
                step(
                    "extract",
                    input=ref("browser"),
                    format="html",
                    selector="article",
                    fields={"title": {}},
                    emit=True,
                ),
            ]
        ),
        ExecutionContext(run_id=uuid4()),
    )
    assert isinstance(result, CollectionRun) and result.raw_observations == ({"title": "One"},)
    with pytest.raises(RuntimeError, match="closed"):
        browser.content()


def test_builtin_transforms_preserve_types_and_do_not_evaluate_text() -> None:
    registry = transforms()
    assert registry.get("strip")(" x ", {}) == "x"
    assert registry.get("equals")(1, {"other": 1}) is True
    assert registry.get("not")(True, {}) is False
    assert registry.get("length")([1, 2], {}) == 2
    assert registry.get("json")('{"x": 1}', {}) == {"x": 1}
    assert resolve("__import__('os')", {}) == "__import__('os')"
    with pytest.raises(ValueError):
        registry.get("add")("1", {"other": 2})


def test_finite_loop_refuses_truncation_and_restores_binding() -> None:
    result, _, _ = execute(
        recipe(
            [
                {
                    "type": "loop",
                    "parameters": {"items": [{"x": 1}, {"x": 2}], "max_iterations": 1},
                    "steps": [step("transform", name="identity", input=ref("item"), emit=True)],
                }
            ]
        )
    )
    assert isinstance(result, SourceFailure)


def test_global_execution_budget_applies_to_nested_steps() -> None:
    executor = RecipeExecutor(
        ScriptedHttpAdapter(()),
        MemoryEvidenceStore(),
        SourceSettings.model_validate({"health": {"max_executed_steps": 2}}),
    )
    result = executor.execute(
        recipe(
            [
                {
                    "type": "loop",
                    "parameters": {"items": [1, 2]},
                    "steps": [step("transform", name="identity")],
                }
            ]
        ),
        ExecutionContext(run_id=uuid4()),
    )
    assert isinstance(result, SourceFailure)


def test_registered_step_executor_replaces_builtin_implementation() -> None:
    from tender_system.core.domain import ExtractStep, RecipeStep
    from tender_system.core.ports import StepExecutor

    class ReplacementExtractor:
        def execute(self, step: RecipeStep, context: ExecutionContext) -> ExecutionContext:
            assert isinstance(step.parameters, ExtractStep)
            return context.model_copy(
                update={
                    "values": {**context.values, step.parameters.output: [{"title": "Replacement"}]}
                }
            )

    handlers: Registry[StepExecutor] = Registry()
    handlers.register("extract", ReplacementExtractor())
    executor = RecipeExecutor(
        ScriptedHttpAdapter(()), MemoryEvidenceStore(), SourceSettings(), handlers=handlers
    )
    result = executor.execute(
        recipe([step("extract", input="<invalid>", format="json", emit=True)]),
        ExecutionContext(run_id=uuid4()),
    )
    assert isinstance(result, CollectionRun) and result.raw_observations == (
        {"title": "Replacement"},
    )


def test_standard_normalization_transforms_and_failure_visibility() -> None:
    registry = transforms()
    assert registry.get("normalize_text")(" cafe\u0301  beans ", {}) == "café beans"
    assert (
        registry.get("regex_extract")("Tender ID: 123", {"pattern": r"ID: (\d+)", "group": 1})
        == "123"
    )
    assert registry.get("join_fields")(["a", "b"], {"separator": ":"}) == "a:b"
    assert registry.get("map_status")("Closed", {"mapping": {"Closed": "closed"}}) == "closed"
    assert (
        registry.get("parse_money")(
            "1,234.50", {"settings": {"normalization": {"thousands_separator": ","}}}
        )
        == "1234.50"
    )
    assert (
        registry.get("parse_date")("2026-01-01", {"settings": {"timezone": "UTC"}})
        == "2026-01-01T23:59:59.999999+00:00"
    )
    with pytest.raises(ValueError):
        registry.get("parse_money")("unknown", {})


@pytest.mark.parametrize(
    ("value", "factor", "expected"),
    [
        (7629600000000, "0.01", "76296000000.00"),
        (12345, "0.01", "123.45"),
        (0.1, "0.01", "0.001"),
        (
            "123456789012345678901234567890123456789",
            "0.01",
            "1234567890123456789012345678901234567.89",
        ),
    ],
)
def test_decimal_unit_scaling_is_exact(value: JsonValue, factor: str, expected: str) -> None:
    from decimal import localcontext

    with localcontext() as context:
        context.prec = 2
        assert transforms().get("scale_decimal")(value, {"factor": factor}) == expected
        assert context.prec == 2


@pytest.mark.parametrize("value", [True, None, {}, "not a number", "NaN", "Infinity"])
def test_decimal_scaling_rejects_invalid_values(value: JsonValue) -> None:
    with pytest.raises(ValueError):
        transforms().get("scale_decimal")(value, {"factor": "0.01"})


@pytest.mark.parametrize("factor", [True, None, [], "not a number", "NaN", "Infinity", "0", "-1"])
def test_decimal_scaling_rejects_invalid_factors(factor: JsonValue) -> None:
    with pytest.raises(ValueError):
        transforms().get("scale_decimal")(100, {"factor": factor})


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("100.00", 100, True),
        ("0.10", 0.1, True),
        ("100", "100.01", False),
        ("123456789012345678901234567890.00", "123456789012345678901234567890", True),
    ],
)
def test_decimal_comparison_ignores_representation_without_rounding(
    left: JsonValue,
    right: JsonValue,
    expected: bool,
) -> None:
    assert transforms().get("decimal_equals")(left, {"other": right}) is expected


@pytest.mark.parametrize("invalid", [True, None, [], {}, "x", "NaN", "sNaN", "Infinity"])
@pytest.mark.parametrize("side", ["left", "right"])
def test_decimal_comparison_rejects_invalid_operands(invalid: JsonValue, side: str) -> None:
    with pytest.raises(ValueError):
        transforms().get("decimal_equals")(
            invalid if side == "left" else 1,
            {"other": invalid if side == "right" else 1},
        )


def test_browser_cleanup_failure_becomes_structured_failure() -> None:
    class BrokenCloseBrowser(FakeBrowserAdapter):
        def close(self) -> None:
            raise RuntimeError("cleanup failed")

    browser = BrokenCloseBrowser({"https://fixture.test": FakePage(html="x")})
    executor = RecipeExecutor(
        ScriptedHttpAdapter(()),
        MemoryEvidenceStore(),
        SourceSettings(),
        browser_factory=lambda: browser,
    )
    result = executor.execute(
        recipe([step("browser_action", action="navigate", value="https://fixture.test")]),
        ExecutionContext(run_id=uuid4()),
    )
    assert isinstance(result, SourceFailure)


def test_binary_network_evidence_does_not_break_api_capture() -> None:
    from tender_system.collection.recipes import network_evidence
    from tender_system.core.ports import NetworkExchange

    exchange = NetworkExchange(
        HttpRequest("https://fixture.test/image"),
        HttpResponse("https://fixture.test/image", 200, b"\xff\x00"),
    )
    assert network_evidence(exchange) == {
        "url": "https://fixture.test/image",
        "method": "GET",
        "status": 200,
        "body": None,
        "body_base64": "/wA=",
        "capture_error": None,
    }


def test_bad_http_decoding_preserves_raw_failure_body() -> None:
    result, evidence, _ = execute(
        recipe([step("http_request", url="https://fixture.test")]),
        [HttpResponse("https://fixture.test", 200, b"\xff")],
    )
    assert isinstance(result, SourceFailure)
    assert evidence.get(result.failed_evidence_reference)["events"] == [
        {"step": "0:http_request", "status": 200, "raw_body_base64": "/w=="}
    ]


def test_file_evidence_detects_corruption_and_replays_idempotently(tmp_path: Path) -> None:
    store = FileEvidenceStore(tmp_path)
    run_id = uuid4()
    key = store.put(run_id, {"x": 1})
    assert store.put(run_id, {"x": 1}) == key
    path = tmp_path / (key + ".json")
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("{}")
    with pytest.raises(ValueError, match="mismatch"):
        store.get(key)


def test_session_stores_are_interchangeable_and_opaque(tmp_path: Path) -> None:
    from tender_system.collection.adapters import FileSessionStore, MemorySessionStore

    for store in (MemorySessionStore(), FileSessionStore(tmp_path)):
        reference = store.save('{"cookies": [], "origins": []}')
        assert "cookies" not in reference
        assert store.load(reference) == '{"cookies": [], "origins": []}'


@pytest.mark.parametrize(
    "bad",
    [
        step("http_request", url=123),
        step("http_request", url="x", accepted_statuses=[401]),
        step("browser_action", action="click"),
        step("browser_action", action="navigate"),
    ],
)
def test_malformed_action_parameters_fail_during_loading(bad: dict[str, JsonValue]) -> None:
    with pytest.raises(ValidationError):
        recipe([bad])


def test_adapter_selection_changes_through_configuration() -> None:
    from collections.abc import Callable

    from tender_system.collection.recipes import configured_executor
    from tender_system.core.ports import BrowserAdapter, HttpAdapter

    http: Registry[HttpAdapter] = Registry()
    first = ScriptedHttpAdapter((response({"source": "first"}),))
    second = ScriptedHttpAdapter((response({"source": "second"}),))
    http.register("first", first)
    http.register("second", second)
    browsers: Registry[Callable[[], BrowserAdapter]] = Registry()
    executor = configured_executor(
        SourceSettings(acquisition_adapter="second"), MemoryEvidenceStore(), http, browsers
    )
    result = executor.execute(
        recipe([step("http_request", url="https://fixture.test")]), ExecutionContext(run_id=uuid4())
    )
    assert isinstance(result, CollectionRun) and len(second.requests) == 1 and first.requests == []


def test_parse_transform_inherits_source_timezone() -> None:
    executor = RecipeExecutor(
        ScriptedHttpAdapter(()), MemoryEvidenceStore(), SourceSettings(timezone="Asia/Tashkent")
    )
    result = executor.execute(
        recipe(
            [
                step("transform", name="parse_date", input="2026-01-01", output="deadline"),
                step("transform", name="identity", input={"deadline": ref("deadline")}, emit=True),
            ]
        ),
        ExecutionContext(run_id=uuid4()),
    )
    assert isinstance(result, CollectionRun)
    assert result.raw_observations[0]["deadline"] == "2026-01-01T18:59:59.999999+00:00"


def test_example_recipe_loads_and_executes_against_fixtures() -> None:
    example = FileRecipeLoader().load(str(Path(__file__).parents[2] / "recipes/api.example.json"))
    result, _, _ = execute(
        example,
        [
            response(
                {
                    "items": [{"external_tender_id": "1", "title": "Equipment"}],
                    "total": 1,
                    "next": None,
                    "modified_since": "cursor",
                    "generated_at": "2026-01-01T00:00:00Z",
                }
            )
        ],
    )
    assert (
        isinstance(result, CollectionRun)
        and result.expected_count == 1
        and result.checkpoint == "cursor"
    )


def test_keyword_search_preserves_pagination_and_shared_limit() -> None:
    from tender_system.core.domain import ExecutionLimits

    listing = pagination_recipe()
    steps = listing.model_dump(mode="json")["steps"]
    steps[1]["steps"][0]["parameters"]["query"]["q"] = ref("search_keyword")
    definition = listing.model_copy(
        update={"search": SourceSearch.model_validate({"steps": steps})}
    )
    http = ScriptedHttpAdapter(
        tuple(
            response({"items": [{"external_tender_id": str(i), "title": "One"}], "done": done})
            for i, done in [(1, False), (2, True), (3, True)]
        )
    )
    executor = RecipeExecutor(http, MemoryEvidenceStore(), SourceSettings(), sleep=lambda _: None)
    result = executor.execute(
        definition,
        ExecutionContext(run_id=uuid4(), values={"search_keywords": ["router", "cable"]}),
    )
    assert isinstance(result, CollectionRun)
    assert len(result.raw_observations) == 3
    assert [request.url for request in http.requests] == [
        "https://fixture.test/api?page=1&q=router",
        "https://fixture.test/api?page=2&q=router",
        "https://fixture.test/api?page=1&q=cable",
    ]
    assert result.coverage == "sampled" and not result.complete and result.checkpoint is None
    limited_http = ScriptedHttpAdapter((response({"items": [{"title": "One"}], "done": False}),))
    executor.http = limited_http
    limited = executor.execute(
        definition,
        ExecutionContext(
            run_id=uuid4(),
            values={"search_keywords": ["router", "cable"]},
            limits=ExecutionLimits(max_requests=1),
        ),
    )
    assert isinstance(limited, SourceFailure)
    assert len(limited_http.requests) == 1


def test_keyword_search_uses_browser_actions() -> None:
    class SearchBrowser(FakeBrowserAdapter):
        typed: list[str] = []

        def type_text(self, selector: str, text: str) -> None:
            super().type_text(selector, text)
            self.typed.append(text)

    browser = SearchBrowser({"https://fixture.test": FakePage(html="<article>One</article>")})
    definition = recipe(
        [step("transform", name="identity", input=[], emit=True)],
        search={
            "steps": [
                step("browser_action", action="navigate", value="https://fixture.test"),
                step(
                    "browser_action",
                    action="type",
                    selector="#search",
                    value=ref("search_keyword"),
                ),
                step("browser_action", action="click", selector="#submit"),
                step("browser_action", action="content"),
                step(
                    "extract",
                    input=ref("browser"),
                    format="html",
                    selector="article",
                    fields={"title": {}},
                    emit=True,
                ),
            ]
        },
    )
    result = RecipeExecutor(
        ScriptedHttpAdapter(()),
        MemoryEvidenceStore(),
        SourceSettings(),
        browser_factory=lambda: browser,
    ).execute(
        definition,
        ExecutionContext(run_id=uuid4(), values={"search_keywords": ["routers", "cables"]}),
    )
    assert isinstance(result, CollectionRun)
    assert browser.typed == ["routers", "cables"]
    assert len(result.raw_observations) == 2
