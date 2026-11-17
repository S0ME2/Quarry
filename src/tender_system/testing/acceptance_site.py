"""Owned paginated website for application acceptance; never a production adapter."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from threading import Lock
from urllib.parse import parse_qs, urlsplit

import pymupdf

from tender_system.core.domain import SourceRecipe
from tender_system.testing.local_server import local_http_server, server_url


class AcceptanceSite:
    def __init__(self, count: int = 250, page_size: int = 50) -> None:
        self.count = count
        self.page_size = page_size
        self.origin = ""
        self.requests: list[str] = []
        self.lock = Lock()
        self.product_revision = 1
        self.reported_total: int | None = count
        self.bad_witness = False
        self.broken_detail = False
        self.repeated_page = False
        self.rate_limit_once = False
        self.omit_optional = False
        self.snapshot_revision = 1
        # Rendered content has its own representation; it never reads detail JSON.
        self.witness_titles = {key: f"Office supplies {key}" for key in range(count)}
        self.inventory_as_of = datetime.now(UTC).isoformat()

    def witness(self, key: int) -> dict[str, object]:
        return {
            "id": key,
            "title": "Different website record" if self.bad_witness else self.witness_titles[key],
        }

    def record(self, key: int) -> dict[str, object]:
        row: dict[str, object] = {
            "external_tender_id": str(key),
            "title": f"Office supplies {key}",
            "source_url": f"{self.origin}/notice/{key}",
            "currency": "USD",
            "product_items": [
                {
                    "id": "router",
                    "name": "Network router",
                    "quantity": self.product_revision if key == 0 else 1,
                },
                {"id": "cable", "name": "Network cable", "quantity": 2},
            ],
            "document_urls": [f"{self.origin}/documents/{key}.pdf"],
        }
        if self.omit_optional:
            row.pop("document_urls")
            row.pop("currency")
        return row

    def recipe(
        self,
        source_id: str = "acceptance",
        *,
        delayed_total: bool = True,
        incremental: bool = False,
        inventory_timestamp: bool = False,
    ) -> SourceRecipe:
        def transform(
            name: str, value: object, output: str, arguments: dict[str, object] | None = None
        ) -> dict[str, object]:
            return {
                "type": "transform",
                "parameters": {
                    "name": name,
                    "input": value,
                    "output": output,
                    "arguments": arguments or {},
                },
            }

        total: dict[str, object] = {
            "type": "extract",
            "parameters": {
                "input": {"$ref": "listing.body"},
                "format": "json",
                "selector": "total",
                "output": "expected_total",
            },
        }
        page_steps: list[dict[str, object]] = [
            {
                "type": "http_request",
                "parameters": {
                    "url": self.origin + "/api",
                    "query": {"offset": {"$ref": "offset"}},
                    "output": "listing",
                    "inventory_page": True,
                },
            },
            {
                "type": "extract",
                "parameters": {
                    "input": {"$ref": "listing.body"},
                    "format": "json",
                    "selector": "items",
                    "output": "rows",
                },
            },
            {
                "type": "extract",
                "parameters": {
                    "input": {"$ref": "listing.body"},
                    "format": "json",
                    "selector": "snapshot",
                    "output": "snapshot",
                },
            },
        ]
        if not delayed_total:
            page_steps.append(total)
        if inventory_timestamp:
            page_steps.append(
                {
                    "type": "extract",
                    "parameters": {
                        "input": {"$ref": "listing.body"},
                        "format": "json",
                        "selector": "as_of",
                        "output": "inventory_as_of",
                    },
                }
            )
        page_steps.extend(
            [
                {
                    "type": "loop",
                    "parameters": {
                        "items": {"$ref": "rows"},
                        "variable": "entry",
                    },
                    "steps": [
                        {
                            "type": "http_request",
                            "parameters": {
                                "url": {"$ref": "entry.url"},
                                "output": "detail",
                            },
                        },
                        {
                            "type": "extract",
                            "parameters": {
                                "input": {"$ref": "detail.body"},
                                "format": "json",
                                "emit": True,
                            },
                        },
                    ],
                },
            ]
        )
        if delayed_total:
            page_steps.append(total)
        page_steps.extend(
            [
                {
                    "type": "extract",
                    "parameters": {
                        "input": {"$ref": "listing.body"},
                        "format": "json",
                        "selector": "done",
                        "output": "done",
                    },
                },
                transform("add", {"$ref": "offset"}, "offset", {"other": self.page_size}),
            ]
        )
        return SourceRecipe.model_validate(
            {
                "source_id": source_id,
                "version": "acceptance-v1",
                "coverage": "incremental" if incremental else "full",
                "expected_count": {"$ref": "expected_total"},
                "inventory_snapshot": {"$ref": "snapshot"},
                "data_as_of": {"$ref": "inventory_as_of"} if inventory_timestamp else None,
                "completion_signal": {"kind": "final_page", "value": {"$ref": "done"}},
                "steps": [
                    transform("identity", 0, "offset"),
                    transform("identity", False, "done"),
                    {
                        "type": "loop",
                        "parameters": {
                            "until": {"$ref": "done"},
                        },
                        "steps": page_steps,
                    },
                ],
            }
        )


@contextmanager
def acceptance_site(count: int = 250) -> Iterator[AcceptanceSite]:
    site = AcceptanceSite(count)
    pdf = pymupdf.open()  # type: ignore[no-untyped-call]
    pdf.new_page().insert_text((72, 72), "Supply Network router and Network cable.")
    attachment = pdf.tobytes()  # type: ignore[no-untyped-call]
    pdf.close()  # type: ignore[no-untyped-call]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            with site.lock:
                site.requests.append(self.path)
            parsed = urlsplit(self.path)
            status, kind = 200, "application/json"
            if parsed.path == "/api":
                if site.rate_limit_once:
                    site.rate_limit_once = False
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
                if site.repeated_page:
                    offset = 0
                rows = range(offset, min(site.count, offset + site.page_size))
                body = json.dumps(
                    {
                        "items": [{"url": f"{site.origin}/detail/{key}"} for key in rows],
                        "total": site.reported_total,
                        "as_of": site.inventory_as_of,
                        "snapshot": (
                            f"inventory-{site.count}-{site.product_revision}-{site.snapshot_revision}"
                        ),
                        "done": offset + site.page_size >= site.count,
                    }
                ).encode()
            elif parsed.path.startswith(("/detail/", "/witness/")):
                key = int(parsed.path.rsplit("/", 1)[-1])
                row = site.witness(key) if parsed.path.startswith("/witness/") else site.record(key)
                if site.broken_detail and parsed.path.startswith("/detail/"):
                    row = {"unexpected": "changed API"}
                body = json.dumps(row).encode()
            elif parsed.path.startswith("/documents/") and parsed.path.endswith(".pdf"):
                kind, body = "application/pdf", attachment
            elif parsed.path.startswith("/notice/"):
                key = int(parsed.path.rsplit("/", 1)[-1])
                kind = "text/html"
                # Tender content exists only after a real browser runs JS and fetches data.
                body = (
                    '<html><body><main id="content"></main><script>'
                    f"fetch('/witness/{key}').then(r=>r.json()).then(r=>{{"
                    "const h=document.createElement('h1');h.textContent=r.title;"
                    "document.querySelector('#content').appendChild(h);});"
                    "</script></body></html>"
                ).encode()
            else:
                status, body = 404, b"{}"
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with local_http_server(Handler) as server:
        site.origin = server_url(server)
        yield site
