"""HTTP, evidence and authorized session implementations. No recipe knowledge."""

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4

import httpx
from pydantic import JsonValue, TypeAdapter

from tender_system.core.execution import (
    RedirectLimitExceeded,
    RequestBudgetExceeded,
    ResponseBudgetExceeded,
    remaining_seconds,
)
from tender_system.core.ports import HttpRequest, HttpResponse


class HttpxAdapter:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client if client is not None else httpx.Client(follow_redirects=True)

    def request(self, request: HttpRequest) -> HttpResponse:
        if request.max_response_bytes < 1:
            raise ValueError("HTTP response byte budget must be positive")
        if request.max_requests < 1:
            raise ValueError("HTTP request budget must be positive")
        deadline = time.monotonic() + request.timeout_seconds
        requests_made = received = redirects_followed = 0
        auth_flow = None
        try:
            outgoing = self.client.build_request(
                request.method,
                request.url,
                headers=request.headers,
                content=request.body,
                timeout=request.timeout_seconds,
            )
            auth = self.client.auth
            if auth is None:
                auth = (
                    httpx.BasicAuth(outgoing.url.username, outgoing.url.password)
                    if outgoing.url.username or outgoing.url.password
                    else httpx.Auth()
                )
            # Authentication challenges also issue physical requests. Drive the
            # public auth flow ourselves so send cannot hide a challenge retry.
            auth_flow = auth.sync_auth_flow(outgoing)
            outgoing = next(auth_flow)
            while True:
                if requests_made >= request.max_requests:
                    raise RequestBudgetExceeded(request.max_requests, requests_made, received)
                remaining = remaining_seconds("http_request")
                timeout = deadline - time.monotonic()
                if remaining is not None:
                    timeout = min(timeout, remaining)
                if timeout <= 0:
                    raise TimeoutError("HTTP request deadline exceeded")
                outgoing.extensions["timeout"] = httpx.Timeout(timeout).as_dict()
                requests_made += 1
                # next_request keeps HTTPX redirect method/body and credential
                # stripping. Never let send reapply client auth across hosts.
                response = self.client.send(
                    outgoing, stream=True, follow_redirects=False, auth=None
                )
                try:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        remaining_seconds("http_response")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("HTTP response deadline exceeded")
                        received += len(chunk)
                        if received > request.max_response_bytes:
                            raise ResponseBudgetExceeded(request.max_response_bytes, received)
                        body.extend(chunk)
                    payload = bytes(body)
                    if self.client.follow_redirects and response.next_request is not None:
                        if redirects_followed >= self.client.max_redirects:
                            raise RedirectLimitExceeded(
                                self.client.max_redirects, requests_made, received
                            )
                        outgoing = response.next_request
                        redirects_followed += 1
                    else:
                        # Auth implementations requiring a response body must read
                        # captured bytes, never reconsume the already bounded stream.
                        snapshot = httpx.Response(
                            response.status_code,
                            content=payload,
                            request=response.request,
                        )
                        snapshot.headers = response.headers.copy()
                        try:
                            outgoing = auth_flow.send(snapshot)
                        except StopIteration:
                            return HttpResponse(
                                str(response.url),
                                response.status_code,
                                payload,
                                dict(response.headers),
                                requests_made,
                                received,
                            )
                finally:
                    response.close()
        except (httpx.TimeoutException, httpx.TransportError) as failure:
            error = (
                TimeoutError("HTTP request timed out")
                if isinstance(failure, httpx.TimeoutException)
                else ConnectionError("HTTP transport failed")
            )
            error.__dict__.update(requests_made=requests_made, bytes_received=received)
            raise error from None
        except Exception as error:
            # Accounting is safe metadata, independent of potentially sensitive
            # transport exception text. Replacement adapters may supply it too.
            error.__dict__.update(requests_made=requests_made, bytes_received=received)
            raise
        finally:
            if auth_flow is not None:
                auth_flow.close()

    def close(self) -> None:
        self.client.close()


class MemoryEvidenceStore:
    def __init__(self) -> None:
        self._values: dict[str, dict[str, JsonValue]] = {}

    def put(self, run_id: UUID, evidence: Mapping[str, JsonValue]) -> str:
        payload = json.dumps(dict(evidence), sort_keys=True, ensure_ascii=False)
        reference = f"{run_id}/{hashlib.sha256(payload.encode()).hexdigest()}"
        self._values[reference] = deepcopy(dict(evidence))
        return reference

    def get(self, reference: str) -> Mapping[str, JsonValue]:
        return deepcopy(self._values[reference])


class FileEvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put(self, run_id: UUID, evidence: Mapping[str, JsonValue]) -> str:
        payload = json.dumps(dict(evidence), sort_keys=True, ensure_ascii=False).encode()
        key = f"{run_id.hex}-{hashlib.sha256(payload).hexdigest()}"
        path = self.root / (key + ".json")
        if path.exists():
            if path.read_bytes() != payload:
                raise ValueError("evidence content mismatch")
            return key
        with NamedTemporaryFile(dir=self.root, prefix=".evidence-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return key

    def get(self, reference: str) -> Mapping[str, JsonValue]:
        if re.fullmatch(r"[0-9a-f]{32}-[0-9a-f]{64}", reference) is None:
            raise ValueError("invalid evidence reference")
        payload = (self.root / (reference + ".json")).read_bytes()
        if hashlib.sha256(payload).hexdigest() != reference.split("-")[1]:
            raise ValueError("evidence content mismatch")
        return TypeAdapter(dict[str, JsonValue]).validate_json(payload)


class MemorySessionStore:
    """Opaque session references; cookie/local-storage payloads never enter recipe evidence."""

    def __init__(self) -> None:
        self._states: dict[str, str] = {}

    def save(self, state: str) -> str:
        reference = uuid4().hex
        self._states[reference] = state
        return reference

    def load(self, reference: str) -> str:
        return self._states[reference]


class FileSessionStore:
    def __init__(self, root: Path) -> None:
        self.evidence = FileEvidenceStore(root)

    def save(self, state: str) -> str:
        return self.evidence.put(uuid4(), {"state": state})

    def load(self, reference: str) -> str:
        value = self.evidence.get(reference)["state"]
        if not isinstance(value, str):
            raise ValueError("invalid stored session")
        return value
