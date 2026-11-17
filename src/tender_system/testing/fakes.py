"""Scripted adapters return supplied data/errors without network or model calls."""

from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import JsonValue

from tender_system.core.domain import (
    CampaignCriteria,
    MatchResult,
    SourceRecord,
    Tender,
)
from tender_system.core.ports import (
    HttpRequest,
    HttpResponse,
    ModelRequest,
    ModelResponse,
    NetworkExchange,
    SourceAlreadyExists,
)


@dataclass
class FakeClock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


class FakeSourceRepository:
    def __init__(self) -> None:
        self._sources: dict[str, SourceRecord] = {}

    def get(self, source_id: str) -> SourceRecord | None:
        source = self._sources.get(source_id)
        return source.model_copy(deep=True) if source is not None else None

    def add(self, source: SourceRecord) -> None:
        if source.id in self._sources:
            raise SourceAlreadyExists(source.id)
        self._sources[source.id] = source.model_copy(deep=True)


class ScriptedHttpAdapter:
    def __init__(self, responses: tuple[HttpResponse | Exception, ...]) -> None:
        self._responses = deque(deepcopy(responses))
        self.requests: list[HttpRequest] = []

    def request(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(deepcopy(request))
        if not self._responses:
            raise AssertionError("unexpected HTTP request: script exhausted")
        result = self._responses.popleft()
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


@dataclass
class FunctionHttpAdapter:
    handler: Callable[[HttpRequest], HttpResponse]

    def request(self, request: HttpRequest) -> HttpResponse:
        return self.handler(request)


class ScriptedModelClient:
    def __init__(self, responses: tuple[ModelResponse | Exception, ...]) -> None:
        self._responses = deque(deepcopy(responses))
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(deepcopy(request))
        if not self._responses:
            raise AssertionError("unexpected model request: script exhausted")
        result = self._responses.popleft()
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


@dataclass
class FunctionModelClient:
    handler: Callable[[ModelRequest], ModelResponse]

    def generate(self, request: ModelRequest) -> ModelResponse:
        return self.handler(request)


@dataclass(frozen=True)
class FakePage:
    html: str
    network_exchanges: tuple[NetworkExchange, ...] = ()
    screenshot_bytes: bytes = b"fake-screenshot"
    trace_bytes: bytes = b"fake-trace"
    downloads: Mapping[str, bytes] = field(default_factory=dict)
    click_targets: Mapping[str, str] = field(default_factory=dict)


class FakeBrowserAdapter:
    def __init__(
        self, pages: Mapping[str, FakePage], failures: Mapping[str, Exception] | None = None
    ) -> None:
        self._pages = deepcopy(dict(pages))
        self._failures = dict(failures or {})
        self._url: str | None = None
        self._closed = False
        self._session_reference: str | None = None
        self.events: list[tuple[str, str]] = []

    def _check(self, operation: str) -> None:
        if self._closed:
            raise RuntimeError("browser session is closed")
        if operation in self._failures:
            raise self._failures[operation]

    def _page(self) -> FakePage:
        if self._url is None:
            raise RuntimeError("navigate before using page")
        return self._pages[self._url]

    def navigate(self, url: str) -> None:
        self._check("navigate")
        if url not in self._pages:
            raise LookupError("unknown fixture URL")
        self._url = url
        self.events.append(("navigate", url))

    def current_url(self) -> str:
        self._check("current_url")
        if self._url is None:
            raise RuntimeError("navigate before reading current URL")
        return self._url

    def click(self, selector: str) -> None:
        self._check("click")
        target = self._page().click_targets.get(selector)
        self.events.append(("click", selector))
        if target is not None:
            self.navigate(target)

    def type_text(self, selector: str, text: str) -> None:
        self._check("type_text")
        self._page()
        # Typed values can contain credentials. Record selector only.
        self.events.append(("type_text", selector))

    def select(self, selector: str, value: str) -> None:
        self._check("select")
        self._page()
        self.events.append(("select", selector))

    def scroll(self, pixels: int) -> None:
        self._check("scroll")
        self._page()
        self.events.append(("scroll", str(pixels)))

    def content(self) -> str:
        self._check("content")
        return self._page().html

    def network(self, include_request_details: bool = False) -> tuple[NetworkExchange, ...]:
        self._check("network")
        return deepcopy(self._page().network_exchanges)

    def screenshot(self) -> bytes:
        self._check("screenshot")
        return self._page().screenshot_bytes

    def trace(self) -> bytes:
        self._check("trace")
        return self._page().trace_bytes

    def download(self, url: str) -> bytes:
        self._check("download")
        return self._page().downloads[url]

    def restore_session(self, authorized_reference: str) -> None:
        self._check("restore_session")
        self._session_reference = authorized_reference
        self.events.append(("restore_session", "[reference redacted]"))

    def close(self) -> None:
        self._closed = True

    def save_session(self) -> str:
        self._check("save_session")
        return self._session_reference or "fake-session"

    def session_state(self) -> dict[str, JsonValue]:
        self._check("session_state")
        return {"cookies": [], "origins": []}

    def wait_for_selector(self, selector: str) -> None:
        self._check("wait_for_selector")
        self.events.append(("wait_for_selector", selector))

    def wait_for_response(self, url_pattern: str) -> None:
        self._check("wait_for_response")
        self.events.append(("wait_for_response", url_pattern))


class ScriptedMatcher:
    implementation = "fake"

    def __init__(self, outcomes: tuple["MatchResult | Exception", ...], version: str = "1") -> None:
        self.version = version
        self.outcomes = deque(outcomes)
        self.calls: list[tuple[Tender, CampaignCriteria]] = []

    def match(self, tender: "Tender", criteria: "CampaignCriteria") -> "MatchResult":
        self.calls.append((tender.model_copy(deep=True), criteria))
        if not self.outcomes:
            raise AssertionError("matcher script exhausted")
        result = self.outcomes.popleft()
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FunctionMatcher:
    handler: Callable[["Tender", "CampaignCriteria"], "MatchResult"]
    implementation: str = "fake"
    version: str = "1"

    def match(self, tender: "Tender", criteria: "CampaignCriteria") -> "MatchResult":
        return self.handler(tender, criteria)
