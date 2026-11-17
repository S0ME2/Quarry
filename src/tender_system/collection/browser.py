"""One replaceable Playwright session with captured network evidence."""

import base64
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast
from urllib.parse import urljoin

from playwright.sync_api import CDPSession, Page, Request, Route, StorageState, sync_playwright
from pydantic import JsonValue

from tender_system.collection.network import (
    UnsafeWebsite,
    charge_public_bytes,
    charge_public_request,
    public_bytes_remaining,
)
from tender_system.core.domain import ExecutionLimits
from tender_system.core.execution import (
    RequestBudgetExceeded,
    ResponseBudgetExceeded,
    TransportUsage,
    current_transport_usage,
    effective_resource_limits,
    remaining_seconds,
)
from tender_system.core.ports import (
    HttpAdapter,
    HttpRequest,
    HttpResponse,
    NetworkExchange,
    SessionStore,
)


class UnsupportedBrowserTarget(ValueError):
    code = "unsupported_browser_target"
    stage = "browser_network"


class PlaywrightBrowserAdapter:
    """Bound Python network evidence, not Chromium native-process memory.

    Playwright exposes complete bodies, not streams. Known oversized content lengths
    are rejected before body(); unknown/encoded bodies are checked immediately after
    native allocation and discarded. Chromium/DOM/trace memory needs process isolation.
    """

    def __init__(
        self,
        sessions: SessionStore,
        timeout_seconds: float = 30,
        limits: ExecutionLimits | None = None,
        *,
        navigation_wait_until: Literal["load", "domcontentloaded"] = "load",
        request_guard: Callable[[str, str], None] | None = None,
        request_http: HttpAdapter | None = None,
    ) -> None:
        self.sessions = sessions
        self.request_guard = request_guard
        self.request_http = request_http
        self.limits = effective_resource_limits(limits)
        self._usage = current_transport_usage() or TransportUsage()
        self._budget_error: Exception | None = None
        self._closing = False
        self._redirect_sessions: dict[Page, CDPSession] = {}
        self.navigation_wait_until = navigation_wait_until
        self.timeout_ms = timeout_seconds * 1000
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True, timeout=self._timeout())
            self._exchanges: list[NetworkExchange] = []
            self._detailed_exchanges: list[NetworkExchange] = []
            self._context = self._browser.new_context(service_workers="block")
            self._setup()
        except Exception:
            self._playwright.stop()
            raise

    def _timeout(self) -> float:
        remaining = remaining_seconds("browser_action")
        return min(self.timeout_ms, remaining * 1000) if remaining is not None else self.timeout_ms

    def _setup(self) -> None:
        self._context.set_default_timeout(self.timeout_ms)
        if self.request_http is None:
            self._context.route("**/*", self._route)
        else:
            self._context.route_web_socket("**/*", lambda route: route.close())
        # Native DOM-snapshot tracing starts a HAR collector that can re-fetch
        # discarded asset bodies outside request interception. Capture DOM explicitly.
        self._context.tracing.start(screenshots=True, snapshots=False, sources=False)
        self._page = self._context.new_page()
        self._instrument_redirects(self._page)
        self._context.on("page", self._reject_popup)
        self._context.on("requestfinished", self._capture)
        self._context.on("requestfailed", self._capture)

    def _reject_popup(self, page: Page) -> None:
        if self._closing or page is self._page:
            return
        self._budget_error = UnsupportedBrowserTarget(
            "Popup acquisition cannot attest the browser network budget; "
            "use a same-page or HTTP recipe"
        )
        page.close()

    def _instrument_redirects(self, page: Page) -> None:
        if page in self._redirect_sessions or self._closing:
            return
        if len(self._redirect_sessions) >= self.limits.max_requests:
            error = RequestBudgetExceeded(self.limits.max_requests, self._usage.requests)
            error.stage = "browser_network"
            error.args = ("Browser page allocation exhausted the request safety budget",)
            self._budget_error = error
            page.close()
            return
        session = self._context.new_cdp_session(page)
        self._redirect_sessions[page] = session

        def forget(closed_page: Page) -> None:
            self._redirect_sessions.pop(closed_page, None)

        page.once("close", forget)

        def paused(event: dict[str, Any]) -> None:
            if self._closing:
                return
            if self.request_http is not None:
                # Fulfill every browser request through the pinned HTTP transport.
                # Chromium never resolves or connects to an untrusted destination,
                # including redirects which Playwright's route hook can miss.
                try:
                    if not self._guard(
                        event["request"]["url"],
                        event["request"]["method"],
                        required=event.get("resourceType") == "Document",
                    ):
                        session.send(
                            "Fetch.failRequest",
                            {"requestId": event["requestId"], "errorReason": "BlockedByClient"},
                        )
                        return
                    if page is not self._page or not self._request_allowed(
                        asset=event.get("resourceType") not in {"Document", "XHR", "Fetch"}
                    ):
                        raise UnsupportedBrowserTarget("Public browser request rejected")
                    response = self.request_http.request(
                        HttpRequest(
                            event["request"]["url"],
                            event["request"]["method"],
                            headers=event["request"].get("headers", {}),
                            timeout_seconds=self._timeout() / 1000,
                            max_requests=1,
                            max_response_bytes=max(
                                1,
                                min(
                                    self.limits.max_response_bytes,
                                    self.limits.max_total_response_bytes
                                    - self._usage.response_bytes,
                                ),
                            ),
                        )
                    )
                    if not self._body_allowed(len(response.body)):
                        raise self._budget_error or UnsupportedBrowserTarget("Body rejected")
                    session.send(
                        "Fetch.fulfillRequest",
                        {
                            "requestId": event["requestId"],
                            "responseCode": response.status_code,
                            "responseHeaders": [
                                {"name": name, "value": value}
                                for name, value in response.headers.items()
                                if name.lower()
                                not in {
                                    "content-encoding",
                                    "content-length",
                                    "transfer-encoding",
                                    "connection",
                                }
                            ],
                            "body": base64.b64encode(response.body).decode("ascii"),
                        },
                    )
                except Exception as error:
                    if "Invalid InterceptionId" in str(error):
                        # The page cancelled this request while its HTTP fetch was in flight.
                        return
                    self._budget_error = self._budget_error or error
                    try:
                        session.send(
                            "Fetch.failRequest",
                            {"requestId": event["requestId"], "errorReason": "BlockedByClient"},
                        )
                    except Exception as abort_error:
                        # Chromium may already have cancelled a paused request on shutdown.
                        if "Invalid InterceptionId" not in str(abort_error):
                            raise
                return
            # Context routing misses HTTP redirects; pause these before network send.
            allowed = (
                self._guard(event["request"]["url"], event["request"]["method"])
                and self._budget_error is None
                and (
                    not event.get("redirectedRequestId")
                    or self._request_allowed(
                        asset=event.get("resourceType") not in {"Document", "XHR", "Fetch"}
                    )
                )
            )
            if allowed:
                session.send("Fetch.continueRequest", {"requestId": event["requestId"]})
            else:
                session.send(
                    "Fetch.failRequest",
                    {
                        "requestId": event["requestId"],
                        "errorReason": "BlockedByClient",
                    },
                )

        session.on("Fetch.requestPaused", paused)
        session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

    @contextmanager
    def _action(self) -> Iterator[None]:
        self._raise_budget()
        remaining_seconds("browser_action")
        try:
            yield
        finally:
            self._raise_budget()

    def _raise_budget(self) -> None:
        if self._budget_error is not None:
            raise self._budget_error

    def _request_allowed(self, *, asset: bool = False) -> bool:
        if self.request_guard:
            try:
                charge_public_request()
            except Exception as budget_failure:
                self._budget_error = budget_failure
                return False
        if self._closing or self._budget_error is not None:
            return False
        if self._usage.requests >= self.limits.max_requests:
            error = RequestBudgetExceeded(
                self.limits.max_requests, self._usage.requests, self._usage.response_bytes
            )
            error.stage = "browser_network"
            error.args = (
                f"Browser request budget exhausted "
                f"({self._usage.requests}/{self.limits.max_requests}); "
                "additional requests aborted; increase the explicit request budget if appropriate",
            )
            self._budget_error = error
            return False
        self._usage.requests += 1
        self._usage.browser_requests += 1
        self._usage.browser_asset_requests += int(asset)
        return True

    def _route(self, route: Route) -> None:
        if not self._guard(route.request.url, route.request.method):
            route.abort("blockedbyclient")
            return
        # Closing a loaded popup can release its queued requests without routing
        # callbacks. Reject its initial navigation before any document can run.
        if route.request.is_navigation_request():
            try:
                popup = route.request.frame.page is not self._page
            except Exception:
                # Early popup navigations may not yet expose a frame.
                popup = True
            if popup:
                self._budget_error = UnsupportedBrowserTarget(
                    "Popup acquisition cannot attest the browser network budget; "
                    "use a same-page or HTTP recipe"
                )
                route.abort("blockedbyclient")
                return
        if self._request_allowed(
            asset=route.request.resource_type not in {"document", "xhr", "fetch"}
        ):
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def _guard(self, url: str, method: str, *, required: bool = True) -> bool:
        try:
            if self.request_guard:
                self.request_guard(url, method)
            return True
        except Exception as error:
            if isinstance(error, UnsafeWebsite) and not required:
                # Deny background requests without discarding readable public documents.
                return False
            self._budget_error = error
            return False

    def _body_allowed(self, size: int) -> bool:
        limit = min(
            self.limits.max_response_bytes,
            self.limits.max_total_response_bytes - self._usage.response_bytes,
        )
        if self.request_guard:
            limit = public_bytes_remaining(limit)
        if size > limit:
            error = ResponseBudgetExceeded(max(0, limit), size)
            error.stage = "browser_network"
            error.args = (
                f"Browser capture byte budget exceeded ({size}/{max(0, limit)}); "
                "oversized body discarded; increase the explicit byte budget if appropriate",
            )
            self._budget_error = error
            return False
        return True

    def _declared_body_allowed(self, headers: Mapping[str, str]) -> bool:
        length = headers.get("content-length")
        # An encoded body's wire length does not bound its decoded body; check both.
        return not (length and length.isdecimal()) or self._body_allowed(int(length))

    def _capture(self, request: Request) -> None:
        if self._closing or self._budget_error is not None:
            return
        try:
            response = request.response()
        except Exception:
            return  # Context shutdown cannot deliver new usable network evidence.
        # Header/body credentials stay out of captured evidence.
        http_request = HttpRequest(request.url, request.method)
        http_response = None
        capture_error = None
        if response is not None:
            if not self._declared_body_allowed(response.headers):
                return
            # Playwright body() can secretly re-fetch images/scripts/stylesheets
            # when Chromium discarded their body. Keep asset metadata only; data
            # extraction consumes original document/XHR/fetch responses and DOM.
            if request.resource_type not in {"document", "xhr", "fetch"}:
                body = b""
                capture_error = "asset_body_omitted"
            else:
                try:
                    body = response.body()
                except Exception as error:
                    body = b""
                    capture_error = type(error).__name__
            try:
                size = (
                    request.sizes()["responseBodySize"] if capture_error is not None else len(body)
                )
            except Exception:
                if not self._closing:
                    self._usage.browser_accounting_complete = False
                    self._budget_error = UnsupportedBrowserTarget(
                        "Browser closed before response accounting completed"
                    )
                return
            allowed = self._body_allowed(size)
            self._usage.response_bytes += size
            if self.request_guard:
                try:
                    charge_public_bytes(size)
                except Exception as error:
                    self._budget_error = error
                    return
            if not allowed:
                return
            http_response = HttpResponse(response.url, response.status, body)
        try:
            headers = request.all_headers()
            if not self._declared_body_allowed(headers):
                return
            request_body = request.post_data_buffer
        except Exception as error:
            headers, request_body = {}, None
            capture_error = type(error).__name__
        if request_body is not None:
            if not self._body_allowed(len(request_body)):
                return
            # Upload bodies are bounded captures, not downloaded response bytes.
        self._exchanges.append(NetworkExchange(http_request, http_response, capture_error))
        self._detailed_exchanges.append(
            NetworkExchange(
                HttpRequest(request.url, request.method, headers, request_body),
                http_response,
                capture_error,
            )
        )

    def navigate(self, url: str) -> None:
        with self._action():
            response = self._page.goto(
                url, wait_until=self.navigation_wait_until, timeout=self._timeout()
            )
            if response is None or response.status >= 400:
                raise ConnectionError("browser navigation failed")

    def current_url(self) -> str:
        with self._action():
            return self._page.url

    def click(self, selector: str) -> None:
        with self._action():
            self._page.locator(selector).click(timeout=self._timeout())

    def type_text(self, selector: str, text: str) -> None:
        with self._action():
            self._page.locator(selector).fill(text, timeout=self._timeout())

    def select(self, selector: str, value: str) -> None:
        with self._action():
            self._page.locator(selector).select_option(value, timeout=self._timeout())

    def scroll(self, pixels: int) -> None:
        with self._action():
            self._page.mouse.wheel(0, pixels)

    def content(self) -> str:
        with self._action():
            return self._page.content()

    def network(self, include_request_details: bool = False) -> tuple[NetworkExchange, ...]:
        with self._action():
            self._page.evaluate("() => true")
            return tuple(self._detailed_exchanges if include_request_details else self._exchanges)

    def screenshot(self) -> bytes:
        with self._action():
            return self._page.screenshot(full_page=True, timeout=self._timeout())

    def trace(self) -> bytes:
        with self._action():
            with TemporaryDirectory() as directory:
                path = Path(directory) / "trace.zip"
                self._context.tracing.stop(path=path)
                if not self._body_allowed(path.stat().st_size):
                    self._raise_budget()
                data = path.read_bytes()
            # Trace bytes are local artifacts, not downloaded responses.
            self._context.tracing.start(screenshots=True, snapshots=False, sources=False)
            return data

    def download(self, url: str) -> bytes:
        with self._action():
            while True:
                if not self._guard(url, "GET"):
                    self._raise_budget()
                if not self._request_allowed():
                    self._raise_budget()
                response = self._context.request.get(url, timeout=self._timeout(), max_redirects=0)
                try:
                    if not self._declared_body_allowed(response.headers):
                        self._raise_budget()
                    body = response.body()
                    allowed = self._body_allowed(len(body))
                    self._usage.response_bytes += len(body)
                    if not allowed:
                        self._raise_budget()
                    if 300 <= response.status < 400 and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        continue
                    if not response.ok:
                        raise ConnectionError("browser download failed")
                    return body
                finally:
                    response.dispose()

    def restore_session(self, authorized_reference: str) -> None:
        with self._action():
            state = cast(StorageState, json.loads(self.sessions.load(authorized_reference)))
            self._context.close()
            self._redirect_sessions.clear()
            self._context = self._browser.new_context(storage_state=state, service_workers="block")
            self._exchanges.clear()
            self._detailed_exchanges.clear()
            self._setup()

    def save_session(self) -> str:
        with self._action():
            return self.sessions.save(json.dumps(self._context.storage_state()))

    def session_state(self) -> dict[str, JsonValue]:
        with self._action():
            return cast(dict[str, JsonValue], self._context.storage_state())

    def wait_for_selector(self, selector: str) -> None:
        with self._action():
            self._page.locator(selector).wait_for(state="visible", timeout=self._timeout())

    def wait_for_response(self, url_pattern: str) -> None:
        with self._action():
            if not any(fnmatch(exchange.request.url, url_pattern) for exchange in self.network()):
                self._page.wait_for_event(
                    "requestfinished",
                    predicate=lambda request: fnmatch(request.url, url_pattern),
                    timeout=self._timeout(),
                )

    def close(self) -> None:
        self._closing = True
        try:
            self._browser.close()
        finally:
            self._playwright.stop()
