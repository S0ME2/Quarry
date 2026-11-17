"""Content-addressed recipe validation fingerprints and offline HTTP replay."""

import base64
import hashlib
import json
from collections.abc import Mapping
from typing import cast

from tender_system.core.domain import ExecutionLimits
from tender_system.core.execution import (
    ExecutionDeadlineExceeded,
    RedirectLimitExceeded,
    RequestBudgetExceeded,
    ResponseBudgetExceeded,
    TransportUsage,
)
from tender_system.core.ports import HttpRequest, HttpResponse


def fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def request_fingerprint(request: HttpRequest) -> str:
    return fingerprint(
        {
            "method": request.method.upper(),
            "url": request.url,
            "headers": {key.casefold(): value for key, value in sorted(request.headers.items())},
            "body_sha256": hashlib.sha256(request.body or b"").hexdigest(),
        }
    )


class CapturedResponseHttpAdapter:
    """Replay captured attempts in order, rejecting any request drift."""

    def __init__(self, exchanges: object):
        if not isinstance(exchanges, list):
            raise ValueError("validation evidence has invalid captured HTTP exchanges")
        if not all(isinstance(item, dict) for item in exchanges):
            raise ValueError("validation evidence contains invalid HTTP exchanges")
        self.exchanges = cast(list[dict[str, object]], exchanges)
        self.index = 0

    def request(self, request: HttpRequest) -> HttpResponse:
        if self.index >= len(self.exchanges):
            raise ValueError("offline replay requested an uncaptured HTTP exchange")
        exchange = self.exchanges[self.index]
        self.index += 1
        if exchange.get("request_fingerprint") != request_fingerprint(request):
            raise ValueError("offline replay request differs from captured validation evidence")
        requests_made = exchange.get("requests_made", 1)
        bytes_received = exchange.get("bytes_received")
        if type(requests_made) is not int or requests_made < 0:
            raise ValueError("validation evidence contains invalid HTTP request accounting")
        if bytes_received is not None and (type(bytes_received) is not int or bytes_received < 0):
            raise ValueError("validation evidence contains invalid HTTP byte accounting")
        if requests_made == 0 and bytes_received:
            raise ValueError("validation evidence contains inconsistent HTTP accounting")
        if requests_made > request.max_requests:
            raise ValueError("captured HTTP exchange exceeds the replay request budget")
        error = exchange.get("error")
        if isinstance(error, str):
            failure: Exception
            if error == "ResponseBudgetExceeded":
                limit, received = exchange.get("limit"), exchange.get("received")
                if type(limit) is not int or type(received) is not int:
                    raise ValueError("invalid captured response byte budget failure")
                if bytes_received is not None and bytes_received != received:
                    raise ValueError("captured response failure has inconsistent HTTP accounting")
                failure = ResponseBudgetExceeded(limit, received)
                if bytes_received is None:
                    bytes_received = received
            elif error in {"RequestBudgetExceeded", "RedirectLimitExceeded"}:
                limit = exchange.get("limit")
                if type(limit) is not int or limit < 0:
                    raise ValueError("invalid captured HTTP request budget failure")
                failure = (
                    RequestBudgetExceeded(limit, requests_made, bytes_received or 0)
                    if error == "RequestBudgetExceeded"
                    else RedirectLimitExceeded(limit, requests_made, bytes_received or 0)
                )
            elif error == "ExecutionDeadlineExceeded":
                stage = exchange.get("stage")
                if not isinstance(stage, str):
                    raise ValueError("invalid captured HTTP deadline stage")
                failure = ExecutionDeadlineExceeded(stage)
            elif error == "TimeoutError":
                failure = TimeoutError("captured HTTP timeout")
            elif error == "ConnectionError":
                failure = ConnectionError("captured HTTP transport failure")
            elif error == "OSError":
                failure = OSError("captured HTTP I/O failure")
            else:
                raise ValueError("validation evidence contains unsupported captured error")
            failure.__dict__.update(requests_made=requests_made, bytes_received=bytes_received or 0)
            raise failure
        status = exchange.get("status_code")
        body = exchange.get("body_base64")
        headers = exchange.get("headers", {})
        if type(status) is not int or not isinstance(body, str) or not isinstance(headers, dict):
            raise ValueError("validation evidence contains an invalid HTTP response")
        if not all(
            isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
        ):
            raise ValueError("validation evidence contains invalid response headers")
        # Older evidence predates redirect capture and can only attest the requested URL.
        url = exchange.get("url", request.url)
        if not isinstance(url, str) or not url:
            raise ValueError("validation evidence contains an invalid response URL")
        decoded = base64.b64decode(body, validate=True)
        if requests_made < 1 or (bytes_received is not None and bytes_received < len(decoded)):
            raise ValueError("validation evidence contains inconsistent HTTP accounting")
        return HttpResponse(
            url,
            status,
            decoded,
            cast(Mapping[str, str], headers),
            requests_made,
            bytes_received,
        )

    def assert_exhausted(self) -> None:
        if self.index != len(self.exchanges):
            raise ValueError("offline replay did not consume all captured HTTP exchanges")


class EvidenceReplayError(ValueError):
    """Safe, actionable evidence validation error (no source content)."""


class CapturedBrowserSteps:
    """Replay browser-step outputs without opening a browser or making requests."""

    def __init__(self, steps: object):
        if not isinstance(steps, list) or not all(isinstance(item, dict) for item in steps):
            raise EvidenceReplayError("validation evidence has invalid captured browser steps")
        self.steps = cast(list[dict[str, object]], steps)
        self.index = 0

    def step(self, action: str, value: object, selector: str | None) -> object:
        if self.index >= len(self.steps):
            raise EvidenceReplayError("offline replay requested an uncaptured browser step")
        expected = self.steps[self.index]
        self.index += 1
        if (
            expected.get("action") != action
            or expected.get("value_fingerprint") != fingerprint(value)
            or expected.get("selector") != selector
        ):
            raise EvidenceReplayError("offline replay browser step differs from captured evidence")
        if "output" not in expected:
            raise EvidenceReplayError("captured browser step is missing output")
        return expected["output"]

    def assert_exhausted(self) -> None:
        if self.index != len(self.steps):
            raise EvidenceReplayError("offline replay did not consume all captured browser steps")

    def inventory_page_key(self) -> str | None:
        if self.index == 0:
            raise EvidenceReplayError("browser replay has not consumed a step")
        value = self.steps[self.index - 1].get("inventory_page_key")
        if value is not None and (not isinstance(value, str) or not value):
            raise EvidenceReplayError("captured browser inventory page key is invalid")
        return value

    def apply_usage(self, usage: TransportUsage, limits: ExecutionLimits) -> None:
        step = self.steps[self.index - 1]
        usage.browser_accounting_complete &= (
            all(key in step for key in ("requests_made", "bytes_received", "asset_requests"))
            and step.get("accounting_complete", True) is True
        )
        counts = [step.get(key, 0) for key in ("requests_made", "bytes_received", "asset_requests")]
        if any(type(value) is not int or value < 0 for value in counts):
            raise EvidenceReplayError("captured browser step has invalid resource accounting")
        requests, received, assets = cast(list[int], counts)
        if assets > requests:
            raise EvidenceReplayError("captured browser asset count exceeds requests")
        if usage.requests + requests > limits.max_requests:
            raise EvidenceReplayError("captured browser step exceeds shared request budget")
        if usage.response_bytes + received > limits.max_total_response_bytes:
            raise EvidenceReplayError("captured browser step exceeds shared download budget")
        usage.requests += requests
        usage.browser_requests += requests
        usage.browser_asset_requests += assets
        usage.response_bytes += received
