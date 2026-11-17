"""Bounded, format-independent recipes. No database or normalization dependencies."""

import base64
import hashlib
import json
import re
import time
import tomllib
import unicodedata
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import cast, overload
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from defusedxml.ElementTree import fromstring
from pydantic import AwareDatetime, JsonValue, TypeAdapter

from tender_system.collection.normalization import parse_date, parse_money
from tender_system.collection.performance import current_trace, timed
from tender_system.collection.validation_evidence import (
    CapturedBrowserSteps,
    EvidenceReplayError,
    fingerprint,
    request_fingerprint,
)
from tender_system.core.clock import SystemClock as SystemClock
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    BoundedCollectionResult,
    BrowserStep,
    CollectionRun,
    CompletionEvidence,
    ConditionStep,
    ContentSignal,
    CustomStep,
    ExecutionContext,
    ExtractStep,
    HttpStep,
    LoopStep,
    RecipeStep,
    RunDiagnostics,
    SourceFailure,
    SourceRecipe,
    TransformStep,
)
from tender_system.core.execution import (
    ExecutionDeadlineExceeded,
    RequestBudgetExceeded,
    ResponseBudgetExceeded,
    current_transport_usage,
    effective_resource_limits,
    execution_budget,
    remaining_seconds,
    transport_usage,
)
from tender_system.core.ports import (
    BrowserAdapter,
    Clock,
    EvidenceStore,
    HttpAdapter,
    HttpRequest,
    NetworkExchange,
    StepExecutor,
    Transform,
)
from tender_system.core.registry import Registry

JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class FileRecipeLoader:
    def load(self, reference: str) -> SourceRecipe:
        path = Path(reference)
        if path.suffix == ".toml":
            with path.open("rb") as stream:
                return SourceRecipe.model_validate(tomllib.load(stream))
        return SourceRecipe.model_validate_json(path.read_text())


def lookup(value: JsonValue, path: str) -> JsonValue:
    if path in {"", "."}:
        return deepcopy(value)
    for segment in path.split("."):
        if isinstance(value, dict):
            value = value[segment]
        elif isinstance(value, list) and segment.isdigit():
            value = value[int(segment)]
        else:
            raise ValueError(f"cannot resolve path {path}")
    return deepcopy(value)


def resolve(value: JsonValue, values: dict[str, JsonValue]) -> JsonValue:
    """Explicit references; missing paths fail. Never eval or implicit string interpolation."""
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            path = value["$ref"]
            if not isinstance(path, str):
                raise ValueError("reference path must be a string")
            return lookup(values, path)
        return {key: resolve(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, values) for item in value]
    return value


def builtin_transform(value: JsonValue, arguments: Mapping[str, JsonValue]) -> JsonValue:
    name = arguments["operation"]
    if name == "identity":
        return deepcopy(value)
    if name == "strip":
        if not isinstance(value, str):
            raise ValueError("strip requires text")
        return value.strip()
    if name == "json":
        if not isinstance(value, str):
            raise ValueError("json requires text")
        return JSON.validate_json(value)
    if name == "get":
        path = arguments["path"]
        if not isinstance(path, str):
            raise ValueError("get requires path")
        return lookup(value, path)
    if name == "urljoin":
        base = arguments["base"]
        if isinstance(base, str) and isinstance(value, list):
            if not all(isinstance(item, str) for item in value):
                raise ValueError("urljoin requires text URLs")
            return [urljoin(base, cast(str, item)) for item in value]
        if not isinstance(base, str) or not isinstance(value, str):
            raise ValueError("urljoin requires text URLs")
        return urljoin(base, value)
    if name == "equals":
        return value == arguments["other"]
    if name == "decimal_equals":
        values = (value, arguments["other"])
        if any(isinstance(v, bool) or not isinstance(v, (str, int, float)) for v in values):
            raise ExecutionError("decimal_equals requires numeric inputs")
        try:
            left, right = (Decimal(str(v)) for v in values)
        except InvalidOperation:
            raise ExecutionError("decimal_equals requires decimal inputs") from None
        if not left.is_finite() or not right.is_finite():
            raise ExecutionError("decimal_equals requires finite inputs")
        return left == right
    if name == "not":
        return not bool(value)
    if name == "length":
        if not isinstance(value, (str, list, dict)):
            raise ValueError("length requires collection")
        return len(value)
    if name == "add":
        other = arguments["other"]
        if type(value) not in (int, float) or type(other) not in (int, float):
            raise ValueError("add requires numbers")
        return cast(int | float, value) + cast(int | float, other)
    if name == "scale_decimal":
        factor = arguments.get("factor")
        if any(
            isinstance(item, bool) or not isinstance(item, (str, int, float))
            for item in (value, factor)
        ):
            raise ExecutionError("scale_decimal requires numeric value and factor")
        try:
            number, multiplier = Decimal(str(value)), Decimal(str(factor))
        except InvalidOperation:
            raise ExecutionError("scale_decimal requires decimal inputs") from None
        if not number.is_finite() or not multiplier.is_finite() or multiplier <= 0:
            raise ExecutionError("scale_decimal requires finite inputs and positive factor")
        with localcontext() as decimal_context:
            decimal_context.prec = len(number.as_tuple().digits) + len(multiplier.as_tuple().digits)
            return str(number * multiplier)
    if name == "deadline_from_remaining":
        if type(value) not in (int, float):
            raise ExecutionError("deadline_from_remaining requires numeric seconds")
        anchor = datetime.fromisoformat(text_value(arguments["observed_at"]))
        if anchor.tzinfo is None or anchor.utcoffset() is None:
            raise ExecutionError("countdown anchor requires an aware source timestamp")
        return (anchor + timedelta(seconds=cast(int | float, value))).isoformat()
    if name == "normalize_text":
        return " ".join(unicodedata.normalize("NFC", text_value(value)).split())
    if name == "regex_extract":
        pattern = text_value(arguments["pattern"])
        match = re.search(pattern, text_value(value))
        if match is None:
            raise ExecutionError("regex did not match")
        group = arguments.get("group", 0)
        if not isinstance(group, (int, str)):
            raise ExecutionError("regex group must be integer or name")
        return match.group(group)
    if name == "join_fields":
        if arguments.get("skip_null", False) is True and isinstance(value, list):
            value = [item for item in value if item is not None]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ExecutionError("join_fields requires array of text")
        return text_value(arguments.get("separator", " ")).join(cast(list[str], value))
    if name == "merge_fields":
        additions = arguments.get("fields")
        if not isinstance(additions, dict):
            raise ExecutionError("merge_fields requires fields")
        records = value if isinstance(value, list) else [value]
        if any(not isinstance(item, dict) for item in records):
            raise ExecutionError("merge_fields requires object input")
        object_records = [cast(dict[str, JsonValue], item) for item in records]
        if any(set(item) & set(additions) for item in object_records):
            raise ExecutionError("merge_fields cannot replace extracted fields")
        merged = [{**item, **additions} for item in object_records]
        if isinstance(value, list):
            return cast(JsonValue, merged)
        return cast(JsonValue, merged[0])
    if name == "format_scalar":
        template = text_value(arguments["template"])
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ExecutionError("format_scalar requires text or a number")
        if template.count("{value}") != 1 or any(
            char in template.replace("{value}", "") for char in "{}"
        ):
            raise ExecutionError("format_scalar requires one literal {value} placeholder")
        return template.replace("{value}", str(value))
    if name == "assert":
        if value is not True:
            raise ExecutionError(text_value(arguments.get("message", "recipe assertion failed")))
        return True
    if name == "map_status":
        mapping = arguments["mapping"]
        if not isinstance(mapping, dict):
            raise ExecutionError("map_status requires mapping")
        return mapping.get(text_value(value), value)
    if name in {"parse_date", "parse_money"}:
        settings = SourceSettings.model_validate(arguments.get("settings", {}))
        if name == "parse_money":
            return str(parse_money(value, settings))
        return parse_date(
            value, text_value(arguments.get("field", "deadline")), settings
        ).isoformat()
    raise ValueError(f"unknown operation {name}")


def transforms(settings: SourceSettings | None = None) -> Registry[Transform]:
    registry: Registry[Transform] = Registry()

    def build(operation: str) -> Transform:
        def transform(value: JsonValue, parameters: Mapping[str, JsonValue]) -> JsonValue:
            if (
                settings is not None
                and operation in {"parse_date", "parse_money"}
                and "settings" not in parameters
            ):
                parameters = {**parameters, "settings": settings.model_dump(mode="json")}
            return builtin_transform(value, {**parameters, "operation": operation})

        return transform

    for name in (
        "identity",
        "strip",
        "json",
        "get",
        "urljoin",
        "equals",
        "decimal_equals",
        "not",
        "length",
        "add",
        "scale_decimal",
        "deadline_from_remaining",
        "normalize_text",
        "regex_extract",
        "join_fields",
        "merge_fields",
        "format_scalar",
        "assert",
        "map_status",
        "parse_date",
        "parse_money",
    ):
        registry.register(name, build(name))
    return registry


class ExecutionError(ValueError):
    pass


class _BoundedCollectionCompleted(Exception):
    """Stop signal consumed into a successful, explicitly partial smoke result."""


class RecipeExecutor:
    def __init__(
        self,
        http: HttpAdapter,
        evidence: EvidenceStore,
        settings: SourceSettings,
        *,
        browser_factory: Callable[[], BrowserAdapter] | None = None,
        transformations: Registry[Transform] | None = None,
        plugins: Registry[Transform] | None = None,
        handlers: Registry[StepExecutor] | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = http
        self.evidence = evidence
        self.settings = settings
        self.browser_factory = browser_factory
        self.transformations = (
            transformations if transformations is not None else transforms(settings)
        )
        self.plugins = plugins if plugins is not None else Registry()
        self.handlers = handlers if handlers is not None else Registry()
        self.clock = clock if clock is not None else SystemClock()
        self.sleep = sleep

    @overload
    def execute(
        self,
        recipe: SourceRecipe,
        context: ExecutionContext,
        *,
        max_observations: int,
        http_adapter: HttpAdapter | None = None,
        browser_replay: CapturedBrowserSteps | None = None,
    ) -> CollectionRun | BoundedCollectionResult | SourceFailure: ...

    @overload
    def execute(
        self,
        recipe: SourceRecipe,
        context: ExecutionContext,
        *,
        max_observations: None = None,
        http_adapter: HttpAdapter | None = None,
        browser_replay: CapturedBrowserSteps | None = None,
    ) -> CollectionRun | SourceFailure: ...

    def execute(
        self,
        recipe: SourceRecipe,
        context: ExecutionContext,
        *,
        max_observations: int | None = None,
        http_adapter: HttpAdapter | None = None,
        browser_replay: CapturedBrowserSteps | None = None,
    ) -> CollectionRun | BoundedCollectionResult | SourceFailure:
        limits = effective_resource_limits(context.limits)
        with execution_budget(limits.max_duration_seconds, limits=limits), transport_usage():
            return self._execute(
                recipe,
                context,
                max_observations=max_observations,
                http_adapter=http_adapter,
                browser_replay=browser_replay,
            )

    def _execute(
        self,
        recipe: SourceRecipe,
        context: ExecutionContext,
        *,
        max_observations: int | None,
        http_adapter: HttpAdapter | None,
        browser_replay: CapturedBrowserSteps | None,
    ) -> CollectionRun | BoundedCollectionResult | SourceFailure:
        if max_observations is not None and max_observations < 1:
            raise ValueError("max_observations must be positive")
        limits = effective_resource_limits(context.limits)
        if limits.max_tenders is not None:
            max_observations = (
                min(max_observations, limits.max_tenders)
                if max_observations is not None
                else limits.max_tenders
            )
        remaining = remaining_seconds("recipe_execution")
        duration = limits.max_duration_seconds if limits else 900
        deadline = time.monotonic() + min(
            duration, remaining if remaining is not None else duration
        )
        started = self.clock.now()
        values = deepcopy(context.values)
        keywords = context.values.get("search_keywords") if recipe.search else None
        filtered = isinstance(keywords, list) and bool(keywords)
        observations: list[dict[str, JsonValue]] = []
        events: list[JsonValue] = []
        http_exchanges: list[JsonValue] = []
        browser_steps: list[JsonValue] = []
        browser: BrowserAdapter | None = None
        session_reference = self.settings.authorized_session_reference
        executed = 0
        failed_step = "start"
        pages: list[str] = []
        signals: list[ContentSignal] = []
        usage = current_transport_usage()
        assert usage is not None
        terminal: CompletionEvidence | None = None
        adapters = {"http": type(http_adapter or self.http).__name__}
        trace = current_trace()
        executor_started_ns = time.perf_counter_ns() if trace is not None else 0
        listing_requests = detail_requests = 0
        inventory_totals: list[int] = []
        inventory_snapshots: list[str] = []
        recorded_inventory_pages = 0
        inventory_observation_start = 0
        pagination_requests: list[set[str]] = []
        pagination_responses: list[set[str]] = []

        def inspect_content(text: str, reference: str) -> None:
            for kind, signatures in (
                ("session_expired", self.settings.calibration.session_expiry_signatures),
                ("maintenance", self.settings.calibration.maintenance_signatures),
                ("valid_empty", self.settings.calibration.empty_signatures),
            ):
                for signature in signatures:
                    if signature.casefold() in text.casefold():
                        signals.append(
                            ContentSignal.model_validate(
                                dict(kind=kind, signature=signature, reference=reference)
                            )
                        )

        def diagnostics() -> RunDiagnostics:
            return RunDiagnostics(
                adapters=adapters,
                pages=tuple(pages),
                content_signals=tuple(signals),
                transport_attempts=usage.requests,
                raw_count=len(observations),
                completion=terminal,
                inventory_totals=tuple(inventory_totals),
                inventory_snapshots=tuple(inventory_snapshots),
                response_bytes=usage.response_bytes,
                browser_requests=usage.browser_requests,
                browser_accounting_complete=usage.browser_accounting_complete,
                browser_asset_requests=usage.browser_asset_requests,
            )

        def capture_browser_dom(reference: str) -> None:
            assert browser is not None
            before = (usage.requests, usage.response_bytes, usage.browser_asset_requests)
            try:
                dom = browser.content()
                browser.network()
                inspect_content(dom, reference)
                events.append({"step": reference, "output": dom})
            finally:
                # Final DOM acquisition can deliver pending network callbacks after
                # the last recipe action. Preserve their consumption across replay.
                if browser_steps:
                    last = cast(dict[str, JsonValue], browser_steps[-1])
                    for key, delta in zip(
                        ("requests_made", "bytes_received", "asset_requests"),
                        (
                            usage.requests - before[0],
                            usage.response_bytes - before[1],
                            usage.browser_asset_requests - before[2],
                        ),
                        strict=True,
                    ):
                        last[key] = cast(int, last[key]) + delta

        def record_inventory_state(*, partial: bool = False) -> None:
            nonlocal recorded_inventory_pages
            if filtered:
                return
            if len(pages) <= recorded_inventory_pages:
                return
            recorded_inventory_pages = len(pages)
            try:
                total = resolve(
                    recipe.inventory_total
                    if recipe.inventory_total is not None
                    else recipe.expected_count,
                    values,
                )
            except (KeyError, ValueError, TypeError):
                if recipe.inventory_total is not None and not partial:
                    raise ExecutionError("declared source inventory total is unavailable") from None
                total = None  # Some recipes calculate the scope count only after traversal.
            if type(total) is int and total >= 0:
                inventory_totals.append(total)
            elif recipe.inventory_total is not None and not partial:
                raise ExecutionError("declared source inventory total must be nonnegative integer")
            if recipe.inventory_snapshot is not None:
                try:
                    snapshot = resolve(recipe.inventory_snapshot, values)
                except (KeyError, ValueError, TypeError):
                    if partial:
                        return  # A sample cannot attest metadata calculated after its stop point.
                    raise ExecutionError(
                        "declared source inventory snapshot is unavailable"
                    ) from None
                if (
                    not isinstance(snapshot, (str, int))
                    or isinstance(snapshot, bool)
                    or isinstance(snapshot, str)
                    and not snapshot.strip()
                ):
                    raise ExecutionError("source inventory snapshot must resolve to a scalar token")
                inventory_snapshots.append(str(snapshot))

        def check_budget() -> None:
            remaining_seconds("recipe_execution")
            if time.monotonic() >= deadline:
                raise ExecutionError("execution duration budget exceeded")

        def emit(value: JsonValue) -> None:
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, dict):
                    raise ExecutionError("emitted observations must be objects")
                observations.append(deepcopy(item))
                if max_observations is not None and len(observations) >= max_observations:
                    raise _BoundedCollectionCompleted("record limit reached")

        def loop_iteration(step_path: str, iteration: int, body: Callable[[], object]) -> object:
            if trace is None:
                return body()
            started_ns = time.perf_counter_ns()
            event_start = len(trace.events)
            result = body()
            direct_child_ms = sum(
                event["duration_ms"]
                for event in trace.events[event_start:]
                if event["category"] == "recipe_step"
                and isinstance(event.get("step"), str)
                and event["step"].startswith(step_path + "/")
                and "/" not in event["step"][len(step_path) + 1 :]
            )
            elapsed_ns = time.perf_counter_ns() - started_ns
            overhead_ns = max(0, elapsed_ns - int(direct_child_ms * 1_000_000))
            trace.record("loop_overhead", step_path, overhead_ns, iteration=iteration)
            return result

        def execute_steps(steps: tuple[RecipeStep, ...], prefix: str = "") -> None:
            nonlocal browser, executed, failed_step, session_reference
            nonlocal listing_requests, detail_requests
            nonlocal inventory_observation_start
            for index, step in enumerate(steps):
                check_budget()
                step_started_ns = time.perf_counter_ns() if trace is not None else 0
                step_path = f"{prefix}{index}:{step.type}"
                failed_step = step_path
                executed += 1
                if executed > self.settings.health.max_executed_steps:
                    raise ExecutionError(
                        "execution step budget exceeded "
                        f"({executed}/{self.settings.health.max_executed_steps}); "
                        "partial evidence retained; increase the explicit safety budget"
                    )
                parameters = step.parameters
                if step.type in self.handlers.names():
                    replaced = self.handlers.get(step.type).execute(
                        step,
                        ExecutionContext(
                            run_id=context.run_id, values=deepcopy(values), limits=limits
                        ),
                    )
                    if replaced.run_id != context.run_id:
                        raise ExecutionError("step handler changed run identity")
                    if replaced.limits != limits:
                        raise ExecutionError("step handler changed execution limits")
                    values.clear()
                    values.update(deepcopy(replaced.values))
                    if (
                        isinstance(parameters, (ExtractStep, TransformStep, CustomStep))
                        and parameters.emit
                    ):
                        emit(values[parameters.output])
                    events.append({"step": failed_step, "handler": "registered"})
                elif isinstance(parameters, HttpStep):
                    url = resolve(parameters.url, values)
                    if not isinstance(url, str):
                        raise ExecutionError("HTTP URL must resolve to text")
                    query = resolve(parameters.query, values)
                    assert isinstance(query, dict)
                    if any(isinstance(value, (dict, list)) for value in query.values()):
                        raise ExecutionError("HTTP query values must be scalar")
                    if query:
                        parts = urlsplit(url)
                        extra = urlencode(
                            {key: value for key, value in query.items() if value is not None}
                        )
                        url = urlunsplit(
                            parts._replace(query=parts.query + ("&" if parts.query else "") + extra)
                        )
                    headers = resolve(parameters.headers, values)
                    if not isinstance(headers, dict) or any(
                        not isinstance(v, str) for v in headers.values()
                    ):
                        raise ExecutionError("HTTP headers must resolve to text")
                    body = resolve(parameters.body, values)
                    request = HttpRequest(
                        url,
                        parameters.method,
                        cast(dict[str, str], headers),
                        None if body is None else json.dumps(body).encode(),
                        self.settings.timeout_seconds,
                    )
                    if parameters.inventory_page and pagination_requests:
                        signature = request_fingerprint(request)
                        if signature in pagination_requests[-1]:
                            raise ExecutionError("repeated inventory page or continuation cursor")
                        pagination_requests[-1].add(signature)
                    attempts = (
                        self.settings.retry.max_attempts
                        if parameters.method in {"GET", "HEAD"}
                        else 1
                    )
                    request_label = None
                    if trace is not None:
                        if parameters.inventory_page:
                            listing_requests += 1
                            request_label = f"Inventory page {listing_requests}"
                        else:
                            detail_requests += 1
                            request_label = f"Resource request {detail_requests}"
                    for attempt in range(attempts):
                        check_budget()
                        if limits is not None and usage.requests >= limits.max_requests:
                            raise RequestBudgetExceeded(limits.max_requests, usage.requests)
                        request = HttpRequest(
                            request.url,
                            request.method,
                            request.headers,
                            request.body,
                            min(
                                self.settings.timeout_seconds,
                                max(0.001, deadline - time.monotonic()),
                            ),
                            min(
                                limits.max_response_bytes,
                                limits.max_total_response_bytes - usage.response_bytes,
                            )
                            if limits is not None
                            else 10_000_000,
                            limits.max_requests - usage.requests if limits is not None else 10_000,
                        )
                        if request.max_response_bytes < 1:
                            raise ResponseBudgetExceeded(
                                limits.max_total_response_bytes if limits else 100_000_000,
                                usage.response_bytes,
                            )
                        usage.requests += 1
                        request_started_ns = time.perf_counter_ns() if trace is not None else 0
                        response = None
                        exchange_attempts = 1
                        exchange_bytes = 0
                        try:
                            response = (http_adapter or self.http).request(request)
                            exchange_attempts = response.requests_made
                            exchange_bytes = (
                                response.bytes_received
                                if response.bytes_received is not None
                                else len(response.body)
                            )
                            if (
                                type(exchange_attempts) is not int
                                or exchange_attempts < 1
                                or type(exchange_bytes) is not int
                                or exchange_bytes < len(response.body)
                            ):
                                raise ExecutionError(
                                    "HTTP adapter returned invalid request accounting"
                                )
                            usage.requests += exchange_attempts - 1
                            usage.response_bytes += exchange_bytes
                            if exchange_attempts > request.max_requests:
                                raise RequestBudgetExceeded(
                                    request.max_requests, exchange_attempts, exchange_bytes
                                )
                            if exchange_bytes > request.max_response_bytes:
                                raise ResponseBudgetExceeded(
                                    request.max_response_bytes, exchange_bytes
                                )
                        except RequestBudgetExceeded as request_error:
                            if response is None:
                                exchange_attempts = request_error.requests_made
                                exchange_bytes = request_error.bytes_received
                                usage.requests += exchange_attempts - 1
                                usage.response_bytes += exchange_bytes
                            http_exchanges.append(
                                {
                                    "request_fingerprint": request_fingerprint(request),
                                    "error": type(request_error).__name__,
                                    "limit": request_error.limit,
                                    "requests_made": exchange_attempts,
                                    "bytes_received": exchange_bytes,
                                }
                            )
                            raise
                        except ResponseBudgetExceeded as byte_error:
                            if response is None:
                                exchange_attempts = getattr(byte_error, "requests_made", 1)
                                exchange_bytes = getattr(
                                    byte_error, "bytes_received", byte_error.received
                                )
                                usage.requests += exchange_attempts - 1
                                usage.response_bytes += exchange_bytes
                            http_exchanges.append(
                                {
                                    "request_fingerprint": request_fingerprint(request),
                                    "error": "ResponseBudgetExceeded",
                                    "limit": byte_error.limit,
                                    "received": byte_error.received,
                                    "requests_made": exchange_attempts,
                                    "bytes_received": exchange_bytes,
                                }
                            )
                            raise
                        except (TimeoutError, ConnectionError, OSError) as error:
                            exchange_attempts = getattr(error, "requests_made", 1)
                            exchange_bytes = getattr(error, "bytes_received", 0)
                            usage.requests += exchange_attempts - 1
                            usage.response_bytes += exchange_bytes
                            http_exchanges.append(
                                {
                                    "request_fingerprint": request_fingerprint(request),
                                    "error": type(error).__name__,
                                    "stage": getattr(error, "stage", "http_request"),
                                    "requests_made": exchange_attempts,
                                    "bytes_received": exchange_bytes,
                                }
                            )
                            events.append(
                                {
                                    "step": failed_step,
                                    "attempt": attempt + 1,
                                    "error": type(error).__name__,
                                }
                            )
                            if attempt + 1 == attempts:
                                raise
                        except Exception as error:
                            if response is None:
                                usage.requests += getattr(error, "requests_made", 1) - 1
                                usage.response_bytes += getattr(error, "bytes_received", 0)
                            raise
                        else:
                            http_exchanges.append(
                                {
                                    "request_fingerprint": request_fingerprint(request),
                                    "status_code": response.status_code,
                                    "requests_made": exchange_attempts,
                                    "bytes_received": exchange_bytes,
                                    "url": response.url,
                                    "body_base64": base64.b64encode(response.body).decode(),
                                    "headers": {
                                        key.lower(): value
                                        for key, value in response.headers.items()
                                        if key.lower()
                                        in {"date", "last-modified", "etag", "age", "content-type"}
                                    },
                                }
                            )
                            check_budget()
                            try:
                                decoded_body = response.body.decode(
                                    parameters.encoding, errors="strict"
                                )
                            except (UnicodeError, LookupError):
                                events.append(
                                    {
                                        "step": failed_step,
                                        "status": response.status_code,
                                        "raw_body_base64": base64.b64encode(response.body).decode(),
                                    }
                                )
                                raise
                            payload: dict[str, JsonValue] = {
                                "url": response.url,
                                "status": response.status_code,
                                "body": decoded_body,
                                "headers": {
                                    key.lower(): value
                                    for key, value in response.headers.items()
                                    if key.lower()
                                    in {"date", "last-modified", "etag", "age", "content-type"}
                                },
                            }
                            events.append(
                                {"step": failed_step, "response": payload, "attempt": attempt + 1}
                            )
                            if response.status_code in parameters.accepted_statuses:
                                inspect_content(decoded_body, step_path)
                                if parameters.inventory_page:
                                    # Read-only query APIs often paginate POST bodies at one URL.
                                    # Hash payloads rather than exposing possible request secrets.
                                    page = request.url
                                    if parameters.method not in {"GET", "HEAD"}:
                                        digest = hashlib.sha256(
                                            json.dumps(body, sort_keys=True).encode()
                                        ).hexdigest()
                                        page = f"{parameters.method} {page} body-sha256:{digest}"
                                    pages.append(page)
                                    inventory_observation_start = len(observations)
                                    if pagination_responses:
                                        response_signature = hashlib.sha256(
                                            response.body
                                        ).hexdigest()
                                        if response_signature in pagination_responses[-1]:
                                            raise ExecutionError("repeated inventory page response")
                                        pagination_responses[-1].add(response_signature)
                                values[parameters.output] = payload
                                break
                            if (
                                response.status_code not in {408, 429, 500, 502, 503, 504}
                                or attempt + 1 == attempts
                            ):
                                raise ExecutionError(f"HTTP status {response.status_code}")
                        finally:
                            if trace is not None:
                                trace.http(
                                    label=request_label or "HTTP request",
                                    inventory_page=parameters.inventory_page,
                                    method=request.method,
                                    url=request.url,
                                    duration_ns=time.perf_counter_ns() - request_started_ns,
                                    response_bytes=(exchange_bytes),
                                    requests_made=exchange_attempts,
                                    status=response.status_code if response is not None else None,
                                    attempt=attempt + 1,
                                    failed=(
                                        response is None
                                        or response.status_code not in parameters.accepted_statuses
                                    ),
                                )
                        if attempt + 1 < attempts or (
                            response is not None
                            and response.status_code not in parameters.accepted_statuses
                        ):
                            delay = self.settings.retry.backoff_seconds * 2**attempt
                            with timed("retry_delay", "HTTP retry delay", attempt=attempt + 1):
                                (self.sleep if http_adapter is None else lambda _: None)(
                                    min(delay, max(0, deadline - time.monotonic()))
                                )
                elif isinstance(parameters, BrowserStep):
                    value = resolve(parameters.value, values)
                    output: JsonValue = None
                    inventory_page_key = None
                    before_browser = (
                        usage.requests,
                        usage.response_bytes,
                        usage.browser_asset_requests,
                    )
                    if browser_replay is not None:
                        output = cast(
                            JsonValue,
                            browser_replay.step(
                                parameters.action,
                                value,
                                parameters.selector,
                            ),
                        )
                        if parameters.inventory_page:
                            inventory_page_key = browser_replay.inventory_page_key()
                            if inventory_page_key is None:
                                if parameters.action != "navigate":
                                    raise ExecutionError(
                                        "browser replay lacks inventory page evidence"
                                    )
                                inventory_page_key = text_value(value)  # Legacy navigate capture.
                        if parameters.action == "content" and isinstance(output, str):
                            inspect_content(output, step_path)
                        if parameters.action == "restore_session":
                            session_reference = text_value(value)
                        elif parameters.action == "save_session" and isinstance(output, str):
                            session_reference = output
                        browser_replay.apply_usage(usage, limits)
                        adapters["browser"] = "CapturedBrowserSteps"
                    else:
                        if browser is None:
                            if self.browser_factory is None:
                                raise ExecutionError("browser adapter is not configured")
                            browser = self.browser_factory()
                            adapters["browser"] = type(browser).__name__
                            if session_reference:
                                browser.restore_session(session_reference)
                        match parameters.action:
                            case "navigate":
                                browser.navigate(text_value(value))
                            case "click":
                                browser.click(selector(parameters))
                            case "type":
                                browser.type_text(selector(parameters), text_value(value))
                            case "select":
                                browser.select(selector(parameters), text_value(value))
                            case "scroll":
                                if type(value) is not int:
                                    raise ExecutionError("scroll requires integer pixels")
                                browser.scroll(value)
                            case "content":
                                output = browser.content()
                                inspect_content(output, step_path)
                            case "network":
                                output = [
                                    network_evidence(exchange) for exchange in browser.network()
                                ]
                            case "download":
                                output = base64.b64encode(
                                    browser.download(text_value(value))
                                ).decode()
                            case "restore_session":
                                browser.restore_session(text_value(value))
                                session_reference = text_value(value)
                            case "save_session":
                                output = browser.save_session()
                                session_reference = output
                            case "wait_for_selector":
                                browser.wait_for_selector(selector(parameters))
                                output = True
                            case "wait_for_response":
                                browser.wait_for_response(text_value(value))
                        # Drain delivered network callbacks before recording this action's usage.
                        browser.network()
                        if parameters.inventory_page:
                            inventory_page_key = "browser:" + fingerprint(
                                {"url": browser.current_url(), "rendered": browser.content()}
                            )
                    browser_steps.append(
                        {
                            "accounting_complete": usage.browser_accounting_complete,
                            "requests_made": usage.requests - before_browser[0],
                            "bytes_received": usage.response_bytes - before_browser[1],
                            "asset_requests": usage.browser_asset_requests - before_browser[2],
                            "action": parameters.action,
                            "value_fingerprint": fingerprint(value),
                            "selector": parameters.selector,
                            "output": output,
                            "inventory_page_key": inventory_page_key,
                        }
                    )
                    if inventory_page_key is not None:
                        if inventory_page_key in pages:
                            raise ExecutionError("repeated browser inventory page state")
                        pages.append(inventory_page_key)
                        inventory_observation_start = len(observations)
                    values[parameters.output] = output
                    events.append(
                        {"step": failed_step, "action": parameters.action, "output": output}
                    )
                elif isinstance(parameters, ExtractStep):
                    with timed("field_extraction", parameters.format, step=step_path):
                        output = extract(resolve(parameters.input, values), parameters)
                    values[parameters.output] = output
                    events.append(
                        {
                            "step": failed_step,
                            "extracted_count": len(output) if isinstance(output, list) else 1,
                        }
                    )
                    if parameters.emit:
                        emit(output)
                elif isinstance(parameters, (TransformStep, CustomStep)):
                    registry = (
                        self.plugins if isinstance(parameters, CustomStep) else self.transformations
                    )
                    arguments = resolve(parameters.arguments, values)
                    assert isinstance(arguments, dict)
                    with timed("data_transformation", parameters.name, step=step_path):
                        output = JSON.validate_python(
                            registry.get(parameters.name)(
                                resolve(parameters.input, values), arguments
                            )
                        )
                    values[parameters.output] = deepcopy(output)
                    if parameters.emit:
                        emit(output)
                    events.append({"step": failed_step, "plugin": parameters.name})
                elif isinstance(parameters, ConditionStep):
                    predicate = resolve(parameters.predicate, values)
                    if type(predicate) is not bool:
                        raise ExecutionError("condition must resolve to boolean")
                    execute_steps(step.steps if predicate else step.else_steps, step_path + "/")
                elif isinstance(parameters, LoopStep):
                    iteration_limit = (
                        parameters.max_iterations or self.settings.health.max_executed_steps
                    )
                    items = resolve(parameters.items, values)
                    if parameters.items is not None:
                        if not isinstance(items, list):
                            raise ExecutionError("loop items must resolve to array")
                        if len(items) > iteration_limit:
                            raise ExecutionError("loop exceeds iteration limit")
                        prior = deepcopy(values.get(parameters.variable))
                        existed = parameters.variable in values
                        for iteration, item in enumerate(items, start=1):
                            item_value: JsonValue = item
                            loop_parameters: LoopStep = parameters
                            loop_step: RecipeStep = step
                            loop_path: str = step_path

                            def process_item(
                                item: JsonValue = item_value,
                                parameters: LoopStep = loop_parameters,
                                step: RecipeStep = loop_step,
                                step_path: str = loop_path,
                            ) -> None:
                                values[parameters.variable] = deepcopy(item)
                                execute_steps(step.steps, step_path + "/")

                            loop_iteration(step_path, iteration, process_item)
                        if existed:
                            values[parameters.variable] = prior
                        else:
                            values.pop(parameters.variable, None)
                    else:
                        pagination_requests.append(set())
                        pagination_responses.append(set())
                        try:
                            for iteration in range(1, iteration_limit + 1):
                                loop_parameters = parameters
                                loop_step = step
                                loop_path = step_path

                                def process_page(
                                    parameters: LoopStep = loop_parameters,
                                    step: RecipeStep = loop_step,
                                    step_path: str = loop_path,
                                ) -> bool:
                                    execute_steps(step.steps, step_path + "/")
                                    done = resolve(parameters.until, values)
                                    if type(done) is not bool:
                                        raise ExecutionError(
                                            "loop termination must resolve to boolean"
                                        )
                                    return done

                                if loop_iteration(step_path, iteration, process_page):
                                    break
                            else:
                                raise ExecutionError(
                                    "pagination/loop did not terminate within limit"
                                )
                        finally:
                            pagination_requests.pop()
                            pagination_responses.pop()
                if trace is not None:
                    trace.record(
                        "recipe_step",
                        step.type,
                        time.perf_counter_ns() - step_started_ns,
                        step=step_path,
                    )
            if any(
                isinstance(step.parameters, (HttpStep, BrowserStep))
                and step.parameters.inventory_page
                for step in steps
            ):
                record_inventory_state()

        try:
            if filtered:
                assert recipe.search is not None
                assert isinstance(keywords, list)
                for keyword in keywords:
                    if not isinstance(keyword, str) or not keyword.strip():
                        raise ExecutionError("search keywords must be nonblank strings")
                    values.clear()
                    values.update(deepcopy(context.values))
                    values["checkpoint"] = None
                    values["search_keyword"] = keyword
                    execute_steps(recipe.search.steps)
            else:
                execute_steps(recipe.steps)
            complete = False if filtered else resolve(recipe.completion, values)
            if type(complete) is not bool:
                raise ExecutionError("completion must resolve to boolean")
            expected = None if filtered else resolve(recipe.expected_count, values)
            if expected is not None and (type(expected) is not int or expected < 0):
                raise ExecutionError("expected count must be nonnegative integer")
            checkpoint = None if filtered else resolve(recipe.checkpoint, values)
            coverage = "incremental" if filtered else resolve(recipe.coverage, values)
            if coverage not in ("full", "incremental"):
                raise ExecutionError("coverage must resolve to full or incremental")
            data_as_of = None if filtered else resolve(recipe.data_as_of, values)
            if data_as_of is not None and not isinstance(data_as_of, str):
                raise ExecutionError("data_as_of must resolve to an aware timestamp string")
            if browser is not None:
                capture_browser_dom("browser:final_dom")
                browser.close()
                browser = None
            if recipe.completion_signal is not None and not filtered:
                spec = recipe.completion_signal
                terminal_value = resolve(spec.value, values)
                if spec.kind == "next_cursor_absent":
                    observed = terminal_value is None
                elif spec.kind == "total_reconciled":
                    observed = type(terminal_value) is int and terminal_value == len(observations)
                elif spec.kind == "empty_terminal_page":
                    observed = (
                        terminal_value is True
                        and bool(pages)
                        and len(observations) == inventory_observation_start
                    )
                else:
                    observed = terminal_value is True
                terminal = CompletionEvidence(
                    kind=spec.kind,
                    observed=observed,
                    value=terminal_value,
                    reference=json.dumps(spec.value, sort_keys=True),
                )
            with timed("evidence_storage", "recipe evidence"):
                reference = self.evidence.put(
                    context.run_id,
                    {
                        "recipe": recipe.model_dump(mode="json"),
                        "settings": self.settings.model_dump(mode="json"),
                        "events": events,
                        "http_exchanges": http_exchanges,
                        "browser_steps": browser_steps,
                        "observations": list(observations),
                        "diagnostics": diagnostics().model_dump(mode="json"),
                    },
                )
            return CollectionRun.model_validate(
                dict(
                    id=context.run_id,
                    source_id=recipe.source_id,
                    recipe_version=recipe.version,
                    started_at=started,
                    finished_at=self.clock.now(),
                    candidates=(),
                    complete=complete if max_observations is None and not filtered else False,
                    evidence_reference=reference,
                    coverage=coverage if max_observations is None and not filtered else "sampled",
                    raw_observations=tuple(observations),
                    checkpoint=checkpoint if max_observations is None and not filtered else None,
                    expected_count=expected,
                    data_as_of=data_as_of,
                    diagnostics=diagnostics(),
                    session_reference=session_reference,
                )
            )
        except Exception as original_error:
            error = original_error
            try:
                record_inventory_state(partial=isinstance(error, _BoundedCollectionCompleted))
            except (KeyError, ValueError, TypeError) as inventory_error:
                # An invalid declared snapshot must not turn into a successful bounded result.
                error = inventory_error
            if browser is not None:
                try:
                    capture_browser_dom("browser:failed_dom")
                except Exception:
                    events.append({"step": "browser:failed_dom", "capture_failed": True})
            reference = self.evidence.put(
                context.run_id,
                {
                    "recipe": recipe.model_dump(mode="json"),
                    "settings": self.settings.model_dump(mode="json"),
                    "events": events,
                    "http_exchanges": http_exchanges,
                    "browser_steps": browser_steps,
                    "observations": list(observations),
                    "diagnostics": diagnostics().model_dump(mode="json"),
                    "error_type": type(error).__name__,
                    "detail": str(error)
                    if isinstance(
                        error,
                        (
                            ExecutionError,
                            EvidenceReplayError,
                            ExecutionDeadlineExceeded,
                            RequestBudgetExceeded,
                            ResponseBudgetExceeded,
                            _BoundedCollectionCompleted,
                        ),
                    )
                    else "adapter/plugin failure",
                    "failed_step": failed_step,
                },
            )
            if isinstance(error, _BoundedCollectionCompleted):
                expected_count = None
                try:
                    resolved = resolve(recipe.expected_count, values)
                    if type(resolved) is int and resolved >= 0:
                        expected_count = resolved
                except (KeyError, ValueError, TypeError):
                    pass
                data_as_of_timestamp = None
                try:
                    source_time = resolve(recipe.data_as_of, values)
                except (KeyError, ValueError, TypeError):
                    source_time = None
                if source_time is not None:
                    try:
                        if not isinstance(source_time, str):
                            raise ValueError("source timestamp must be text")
                        data_as_of_timestamp = TypeAdapter(AwareDatetime).validate_python(
                            source_time
                        )
                    except (ValueError, TypeError):
                        error = ExecutionError(
                            "source data_as_of must be an aware timestamp string"
                        )
                if isinstance(error, _BoundedCollectionCompleted):
                    bounded = BoundedCollectionResult(
                        id=context.run_id,
                        source_id=recipe.source_id,
                        recipe_version=recipe.version,
                        observations=tuple(observations),
                        evidence_reference=reference,
                        diagnostics=diagnostics(),
                        expected_count=expected_count,
                        started_at=started,
                        finished_at=self.clock.now(),
                        data_as_of=data_as_of_timestamp,
                    )
                    return bounded.sampled_run() if limits.max_tenders is not None else bounded
            return SourceFailure(
                run_id=context.run_id,
                source_id=recipe.source_id,
                recipe=recipe,
                error_code=getattr(error, "code", type(error).__name__),
                error_detail=str(error)
                if isinstance(
                    error,
                    (
                        ExecutionError,
                        EvidenceReplayError,
                        ExecutionDeadlineExceeded,
                        RequestBudgetExceeded,
                        ResponseBudgetExceeded,
                        _BoundedCollectionCompleted,
                    ),
                )
                else "recipe execution failed; see evidence",
                failed_evidence_reference=reference,
                authorized_session_reference=session_reference,
                bad_output=tuple(observations),
                failed_step=failed_step,
                diagnostics=diagnostics(),
                started_at=started,
                finished_at=self.clock.now(),
            )
        finally:
            if trace is not None:
                trace.record(
                    "recipe_executor",
                    "total",
                    time.perf_counter_ns() - executor_started_ns,
                )
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass  # A cleanup failure must not hide the structured execution failure.


def text_value(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ExecutionError("action requires text")
    return value


def selector(parameters: BrowserStep) -> str:
    if not parameters.selector:
        raise ExecutionError("browser action requires selector")
    return parameters.selector


def network_evidence(exchange: NetworkExchange) -> JsonValue:
    record: dict[str, JsonValue] = {
        "url": exchange.request.url,
        "method": exchange.request.method,
        "status": exchange.response.status_code if exchange.response else None,
        "body": None,
        "capture_error": exchange.capture_error,
    }
    if exchange.response is not None and exchange.capture_error is None:
        try:
            record["body"] = exchange.response.body.decode("utf-8")
        except UnicodeError:
            record["body_base64"] = base64.b64encode(exchange.response.body).decode()
    return record


def extract(value: JsonValue, parameters: ExtractStep) -> JsonValue:
    if parameters.format != "html" and any(
        field.many or field.direct_text for field in parameters.fields.values()
    ):
        raise ExecutionError("many/direct_text fields require HTML extraction")
    if parameters.format == "json":
        if isinstance(value, str):
            with timed("json_parse", "JSON"):
                document = JSON.validate_json(value)
        else:
            document = value
        selected = lookup(document, parameters.selector)
        if not parameters.fields:
            return selected
        items = selected if isinstance(selected, list) else [selected]
        json_output: list[JsonValue] = []
        for item in items:
            json_record: dict[str, JsonValue] = {}
            for key, field in parameters.fields.items():
                try:
                    json_record[key] = lookup(item, field.selector)
                except (IndexError, KeyError, ValueError):
                    if field.required:
                        raise
                    json_record[key] = None
            json_output.append(json_record)
        return json_output
    if not isinstance(value, str):
        raise ExecutionError("HTML/XML extraction requires text")
    output: list[JsonValue] = []
    if parameters.format == "html":
        soup = BeautifulSoup(value, "html.parser")
        rows = [soup] if parameters.selector == "." else soup.select(parameters.selector)
        for row in rows:
            record: dict[str, JsonValue] = {}
            for key, field in parameters.fields.items():
                if field.many:
                    html_nodes = [row] if field.selector == "." else row.select(field.selector)
                    if field.required and not html_nodes:
                        raise ExecutionError(f"required HTML field missing: {key}")
                    found_values: list[JsonValue] = []
                    for entry in html_nodes:
                        entry_value = (
                            entry.get(field.attribute)
                            if field.attribute
                            else " ".join(entry.find_all(string=True, recursive=False)).strip()
                            if field.direct_text
                            else entry.get_text(" ", strip=True)
                        )
                        if not isinstance(entry_value, str) or not entry_value.strip():
                            raise ExecutionError(f"HTML field array contains missing value: {key}")
                        found_values.append(entry_value)
                    record[key] = found_values
                    continue
                node = row if field.selector == "." else row.select_one(field.selector)
                found = (
                    None
                    if node is None
                    else (
                        node.get(field.attribute)
                        if field.attribute
                        else " ".join(node.find_all(string=True, recursive=False)).strip()
                        if field.direct_text
                        else node.get_text(" ", strip=True)
                    )
                )
                if isinstance(found, list):
                    found = " ".join(found)
                if field.required and found is None:
                    raise ExecutionError(f"required HTML field missing: {key}")
                record[key] = found if isinstance(found, str) else None
            output.append(record if parameters.fields else str(row))
    else:
        root = fromstring(value, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        nodes = (
            [root]
            if parameters.selector == "."
            else root.findall(parameters.selector, parameters.namespaces)
        )
        for xml_node in nodes:
            record = {}
            for key, field in parameters.fields.items():
                xml_child = (
                    xml_node
                    if field.selector == "."
                    else xml_node.find(field.selector, parameters.namespaces)
                )
                xml_found = (
                    None
                    if xml_child is None
                    else (
                        xml_child.get(field.attribute)
                        if field.attribute
                        else "".join(xml_child.itertext()).strip()
                    )
                )
                if field.required and xml_found is None:
                    raise ExecutionError(f"required XML field missing: {key}")
                record[key] = xml_found
            output.append(record if parameters.fields else "".join(xml_node.itertext()))
    if not output and parameters.require_match:
        raise ExecutionError("extraction selector matched no rows")
    return output


def configured_executor(
    settings: SourceSettings,
    evidence: EvidenceStore,
    http: Registry[HttpAdapter],
    browsers: Registry[Callable[[], BrowserAdapter]],
    *,
    plugins: Registry[Transform] | None = None,
    transformations: Registry[Transform] | None = None,
    handlers: Registry[StepExecutor] | None = None,
) -> RecipeExecutor:
    """Select adapters through configuration. Browser acquisition remains lazy."""
    return RecipeExecutor(
        http.get(settings.acquisition_adapter),
        evidence,
        settings,
        browser_factory=lambda: browsers.get(settings.browser_adapter)(),
        plugins=plugins,
        transformations=transformations,
        handlers=handlers,
    )
