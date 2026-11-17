"""HTTP acquisition and captured replay must expose identical recipe-visible values."""

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue

from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore
from tender_system.collection.recipes import RecipeExecutor
from tender_system.collection.validation_evidence import CapturedResponseHttpAdapter
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import CollectionRun, ExecutionContext, SourceRecipe
from tender_system.core.ports import HttpRequest


def test_redirected_response_replays_recipe_visible_url_and_headers() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "/current"})
        return httpx.Response(200, json={"id": "1"}, headers={"ETag": '"revision-1"'})

    recipe = SourceRecipe.model_validate(
        {
            "source_id": "redirect",
            "version": "1",
            "steps": [
                {"type": "http_request", "parameters": {"url": "https://fixture.test/old"}},
                {
                    "type": "extract",
                    "parameters": {
                        "format": "json",
                        "input": {"$ref": "response"},
                        "fields": {
                            "source_url": {"selector": "url"},
                            "revision": {"selector": "headers.etag"},
                        },
                        "emit": True,
                    },
                },
            ],
        }
    )
    evidence = MemoryEvidenceStore()
    settings = SourceSettings()
    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        acquired = RecipeExecutor(HttpxAdapter(client), evidence, settings).execute(
            recipe, ExecutionContext(run_id=uuid4())
        )
    assert isinstance(acquired, CollectionRun)
    captured = evidence.get(acquired.evidence_reference)
    replay = CapturedResponseHttpAdapter(captured["http_exchanges"])
    repeated = RecipeExecutor(replay, evidence, settings).execute(
        recipe, ExecutionContext(run_id=uuid4())
    )
    assert isinstance(repeated, CollectionRun)
    replay.assert_exhausted()
    assert (
        acquired.raw_observations
        == repeated.raw_observations
        == ({"source_url": "https://fixture.test/current", "revision": '"revision-1"'},)
    )


@pytest.mark.parametrize("failure", ["timeout", "connection"])
def test_live_adapter_and_replay_obey_transport_failure_contract(failure: str) -> None:
    from tender_system.collection.validation_evidence import request_fingerprint

    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive transport detail", request=request)
        raise httpx.ConnectError("sensitive transport detail", request=request)

    error_type = TimeoutError if failure == "timeout" else ConnectionError
    request = HttpRequest("https://fixture.test")
    replay = CapturedResponseHttpAdapter(
        [{"request_fingerprint": request_fingerprint(request), "error": error_type.__name__}]
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        for adapter in (HttpxAdapter(client), replay):
            with pytest.raises(error_type) as caught:
                adapter.request(request)
            assert "sensitive" not in str(caught.value)
    replay.assert_exhausted()


@pytest.mark.parametrize("url", [None, 7, ""])
def test_replay_rejects_corrupt_response_url(url: object) -> None:
    from tender_system.collection.validation_evidence import request_fingerprint

    request = HttpRequest("https://fixture.test")
    replay = CapturedResponseHttpAdapter(
        [
            {
                "request_fingerprint": request_fingerprint(request),
                "status_code": 200,
                "body_base64": "e30=",
                "url": url,
            }
        ]
    )
    with pytest.raises(ValueError, match="response URL"):
        replay.request(request)


def test_legacy_capture_without_redirect_url_remains_replayable() -> None:
    from tender_system.collection.validation_evidence import request_fingerprint

    request = HttpRequest("https://fixture.test")
    replay = CapturedResponseHttpAdapter(
        [
            {
                "request_fingerprint": request_fingerprint(request),
                "status_code": 200,
                "body_base64": "e30=",
            }
        ]
    )
    assert replay.request(request).url == request.url


@pytest.mark.parametrize("backend", ["memory", "file"])
def test_evidence_store_contract_is_idempotent_and_isolates_mutation(
    backend: str, tmp_path: Path
) -> None:
    from tender_system.collection.adapters import FileEvidenceStore

    store = MemoryEvidenceStore() if backend == "memory" else FileEvidenceStore(tmp_path)
    key = uuid4()
    items: list[JsonValue] = ["original"]
    payload: dict[str, JsonValue] = {"nested": {"items": items}}
    reference = store.put(key, payload)
    assert store.put(key, payload) == reference
    items.append("changed")
    read = store.get(reference)
    assert read == {"nested": {"items": ["original"]}}
    assert isinstance(read["nested"], dict)
    read["nested"]["items"] = []
    assert store.get(reference) == {"nested": {"items": ["original"]}}
