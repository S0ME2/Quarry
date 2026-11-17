"""Optional, bounded collection performance traces. Payloads and headers are never recorded."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean, median
from typing import Any
from urllib.parse import urlsplit

_ACTIVE: ContextVar[PerformanceTrace | None] = ContextVar("quarry_performance_trace", default=None)
_MAX_EVENTS = 10000
_ID_SEGMENT = re.compile(r"(?:[0-9a-fA-F-]{8,}|[1-9]\d*)")
_SECRET_MARKERS = {
    "token",
    "access_token",
    "auth",
    "authorization",
    "api-key",
    "apikey",
    "secret",
    "password",
    "session",
}


@dataclass
class PerformanceTrace:
    """Collect bounded timing events and safe HTTP metadata for one execution."""

    enabled: bool = True
    max_events: int = _MAX_EVENTS
    started_ns: int = field(default_factory=time.perf_counter_ns)
    events: list[dict[str, Any]] = field(default_factory=list)
    dropped_events: int = 0

    def activate(self) -> _TraceActivation:
        return _TraceActivation(self)

    def record(self, category: str, name: str, duration_ns: int, **metadata: Any) -> None:
        if not self.enabled:
            return
        if len(self.events) >= self.max_events:
            self.dropped_events += 1
            return
        event = {"category": category, "name": name, "duration_ms": duration_ns / 1_000_000}
        event.update(metadata)
        self.events.append(event)

    def http(
        self,
        *,
        label: str,
        method: str,
        url: str,
        duration_ns: int,
        response_bytes: int | None,
        status: int | None,
        attempt: int,
        failed: bool,
        inventory_page: bool = False,
        requests_made: int = 1,
    ) -> None:
        path = _safe_endpoint(urlsplit(url).path)
        self.record(
            "http_request",
            label,
            duration_ns,
            method=method,
            endpoint=path,
            response_bytes=response_bytes,
            status=status,
            attempt=attempt,
            failed=failed,
            inventory_page=inventory_page,
            requests_made=requests_made,
            connection_reused=None,
        )

    @contextmanager
    def span(self, category: str, name: str, **metadata: Any) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        start = time.perf_counter_ns()
        try:
            yield
        finally:
            self.record(category, name, time.perf_counter_ns() - start, **metadata)

    def report(self, **metadata: Any) -> dict[str, Any]:
        requests = [event for event in self.events if event["category"] == "http_request"]
        latencies = [event["duration_ms"] for event in requests]
        by_category: dict[str, dict[str, float | int]] = {}
        counts = Counter(event["category"] for event in self.events)
        for category, count in counts.items():
            durations = [e["duration_ms"] for e in self.events if e["category"] == category]
            by_category[category] = {
                "count": count,
                "summed_ms": sum(durations),
            }
        total_http = sum(latencies)
        retry_delays = [e for e in self.events if e["category"] == "retry_delay"]

        def duration(category: str) -> float:
            return sum(float(e["duration_ms"]) for e in self.events if e["category"] == category)

        endpoint_groups: dict[str, list[float]] = {}
        for event in requests:
            endpoint_groups.setdefault(event["endpoint"], []).append(event["duration_ms"])
        wall_ms = (time.perf_counter_ns() - self.started_ns) / 1_000_000
        listing_http_ms = sum(event["duration_ms"] for event in requests if event["inventory_page"])
        detail_http_ms = sum(
            event["duration_ms"] for event in requests if not event["inventory_page"]
        )
        json_parse_ms = duration("json_parse")
        extract_transform_ms = max(
            0,
            duration("field_extraction") - json_parse_ms + duration("data_transformation"),
        )
        health_ms = duration("health_validation")
        database_writes_ms = max(0, duration("database_publication") - health_ms)
        exclusive = {
            "listing_http_ms": listing_http_ms,
            "detail_http_ms": detail_http_ms,
            "json_parse_ms": json_parse_ms,
            "extraction_and_transform_ms": extract_transform_ms,
            "normalization_ms": duration("normalization"),
            "health_validation_ms": health_ms,
            "evidence_storage_ms": duration("evidence_storage"),
            "database_writes_ms": database_writes_ms,
        }
        exclusive["other_overhead_ms"] = max(0, wall_ms - sum(exclusive.values()))
        return {
            "schema_version": 1,
            "timing_semantics": {
                "clock": "time.perf_counter_ns (monotonic)",
                "wall_clock_ms": wall_ms,
                "event_durations_are_nested": True,
                "http_is_included_in_executor_and_step_wall_times": True,
                "do_not_sum_nested_categories": True,
            },
            "http": {
                "request_count": sum(event.get("requests_made", 1) for event in requests),
                "logical_request_count": len(requests),
                "summed_request_latency_ms": total_http,
                "failed_request_count": sum(bool(event.get("failed")) for event in requests),
                "retry_attempt_count": sum(event["attempt"] > 1 for event in requests),
                "retry_delay_count": len(retry_delays),
                "measured_retry_delay_ms": sum(e["duration_ms"] for e in retry_delays),
                "average_ms": fmean(latencies) if latencies else None,
                "median_ms": median(latencies) if latencies else None,
                "p95_ms": _percentile(latencies, 0.95),
                "maximum_ms": max(latencies, default=None),
                "slowest_10": sorted(requests, key=lambda e: e["duration_ms"], reverse=True)[:10],
                "requests": requests,
                "by_endpoint": {
                    endpoint: {
                        "count": len(values),
                        "average_ms": fmean(values),
                        "median_ms": median(values),
                        "p95_ms": _percentile(values, 0.95),
                        "maximum_ms": max(values),
                        "summed_ms": sum(values),
                    }
                    for endpoint, values in endpoint_groups.items()
                },
                "connection_reuse_observable": False,
            },
            "aggregates": by_category,
            "exclusive_timing_breakdown_ms": exclusive,
            "events": self.events,
            "dropped_events": self.dropped_events,
            "unavailable_metrics": [
                "HTTP connection reuse (HTTPX does not expose a stable per-request reuse signal)",
                "worker queue and initialization (explicit foreground collection has no worker)",
                "commit-only latency (publication transaction timing includes commit)",
            ],
            **metadata,
        }

    def write_report(self, path: Path, **metadata: Any) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        report = self.report(**metadata)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
        return report

    def log_summary(self, report: dict[str, Any]) -> None:
        if not self.enabled:
            return
        for request in report["http"]["slowest_10"]:
            print(
                f"[HTTP] {request['name']}: {request['duration_ms']:.1f}ms "
                f"status={request['status']} bytes={request['response_bytes']}"
            )
        for category, label in (
            ("json_parse", "JSON"),
            ("field_extraction", "EXTRACT"),
            ("normalization", "NORMALIZE"),
            ("database_publication", "DATABASE"),
        ):
            value = report["aggregates"].get(category)
            if value:
                print(f"[{label}] total: {value['summed_ms']:.1f}ms")


class _TraceActivation:
    def __init__(self, trace: PerformanceTrace):
        self.trace = trace
        self.token: Any = None

    def __enter__(self) -> PerformanceTrace:
        self.token = _ACTIVE.set(self.trace)
        return self.trace

    def __exit__(self, *_: object) -> None:
        _ACTIVE.reset(self.token)


def current_trace() -> PerformanceTrace | None:
    trace = _ACTIVE.get()
    return trace if trace is not None and trace.enabled else None


@contextmanager
def timed(category: str, name: str, **metadata: Any) -> Iterator[None]:
    trace = current_trace()
    if trace is None:
        yield
    else:
        with trace.span(category, name, **metadata):
            yield


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)))]


def _safe_endpoint(path: str) -> str:
    parts = path.split("/")
    safe: list[str] = []
    redact_next = False
    for part in parts:
        if redact_next:
            safe.append("{redacted}")
            redact_next = False
        elif part.casefold() in _SECRET_MARKERS:
            safe.append(part)
            redact_next = True
        elif part and (_ID_SEGMENT.fullmatch(part) or len(part) >= 32):
            safe.append("{id}" if _ID_SEGMENT.fullmatch(part) else "{redacted}")
        elif "." in part and len(part) >= 20:
            safe.append("{redacted}")
        else:
            safe.append(part)
    return "/".join(safe)
