"""One cooperative deadline shared by acquisition, checks and publication."""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from tender_system.core.domain import ExecutionLimits

_DEADLINE: ContextVar[float | None] = ContextVar("quarry_execution_deadline", default=None)
_WRITE_FENCE: ContextVar[Callable[[], None] | None] = ContextVar("quarry_write_fence", default=None)
_RESOURCES: ContextVar[ExecutionLimits | None] = ContextVar("quarry_resource_limits", default=None)


def current_resource_limits() -> ExecutionLimits:
    return _RESOURCES.get() or ExecutionLimits()


def effective_resource_limits(limits: ExecutionLimits | None = None) -> ExecutionLimits:
    """Respect explicit parent caps without treating default caps as an ambient parent."""
    parent = _RESOURCES.get()
    requested = limits or parent or ExecutionLimits()
    if parent is None:
        return requested
    bounds = [bound for bound in (requested.max_tenders, parent.max_tenders) if bound is not None]
    return requested.model_copy(
        update={
            **{
                name: min(getattr(requested, name), getattr(parent, name))
                for name in (
                    "max_requests",
                    "max_duration_seconds",
                    "max_response_bytes",
                    "max_total_response_bytes",
                )
            },
            "max_tenders": min(bounds) if bounds else None,
        }
    )


def effective_execution_limits(limits: ExecutionLimits | None) -> ExecutionLimits | None:
    """Keep implicit defaults compatible; bind any explicit or inherited run contract."""
    if limits is None and _RESOURCES.get() is None:
        return None
    return effective_resource_limits(limits)


class ResponseBudgetExceeded(ValueError):
    code = "response_budget_exceeded"
    stage = "http_response"

    def __init__(self, limit: int, received: int):
        self.limit, self.received = limit, received
        super().__init__(
            f"HTTP response byte budget exceeded ({received}/{limit}); "
            "oversized body discarded; increase the explicit response budget if appropriate"
        )


class RequestBudgetExceeded(ValueError):
    code = "request_budget_exceeded"
    stage = "http_request"

    def __init__(self, limit: int, requests_made: int, bytes_received: int = 0):
        self.limit = limit
        self.requests_made = requests_made
        self.bytes_received = bytes_received
        super().__init__(
            f"HTTP request budget exceeded ({requests_made}/{limit}); "
            "no further redirect request sent; increase the explicit budget if appropriate"
        )


class RedirectLimitExceeded(RequestBudgetExceeded):
    code = "redirect_limit_exceeded"

    def __init__(self, limit: int, requests_made: int, bytes_received: int):
        super().__init__(limit, requests_made, bytes_received)
        self.args = (
            f"HTTP redirect limit exceeded ({limit}); no further request sent; "
            "inspect the source redirect chain before retrying",
        )


class ExecutionDeadlineExceeded(TimeoutError):
    code = "operation_timeout"

    def __init__(self, stage: str):
        super().__init__(f"Operation deadline exceeded during {stage}")
        self.stage = stage
        self.evidence_references: tuple[str, ...] = ()


def fence_execution() -> None:
    fence = _WRITE_FENCE.get()
    if fence is not None:
        fence()


def remaining_seconds(stage: str) -> float | None:
    deadline = _DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ExecutionDeadlineExceeded(stage)
    return remaining


@contextmanager
def execution_budget(
    seconds: float,
    before_write: Callable[[], None] | None = None,
    *,
    limits: ExecutionLimits | None = None,
) -> Iterator[None]:
    resources = (
        effective_resource_limits(limits)
        if limits is not None or _RESOURCES.get() is not None
        else None
    )
    existing = _DEADLINE.get()
    duration = min(seconds, resources.max_duration_seconds) if resources is not None else seconds
    deadline = time.monotonic() + duration
    token = _DEADLINE.set(min(existing, deadline) if existing is not None else deadline)
    fence_token = _WRITE_FENCE.set(before_write or _WRITE_FENCE.get())
    resource_token = _RESOURCES.set(resources)
    try:
        yield
    finally:
        _DEADLINE.reset(token)
        _WRITE_FENCE.reset(fence_token)
        _RESOURCES.reset(resource_token)


@dataclass
class TransportUsage:
    """Per-execution consumption; adapters share it, concurrent runs do not."""

    requests: int = 0
    response_bytes: int = 0
    browser_requests: int = 0
    browser_asset_requests: int = 0
    browser_accounting_complete: bool = True


_USAGE: ContextVar[TransportUsage | None] = ContextVar("quarry_transport_usage", default=None)


def current_transport_usage() -> TransportUsage | None:
    return _USAGE.get()


@contextmanager
def transport_usage() -> Iterator[TransportUsage]:
    usage = TransportUsage()
    token = _USAGE.set(usage)
    try:
        yield usage
    finally:
        _USAGE.reset(token)
