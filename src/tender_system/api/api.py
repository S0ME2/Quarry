"""Local workspace API with bounded requests and secret-safe errors."""

import logging
import os
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from tender_system.core.clock import SystemClock
from tender_system.core.configuration import Configuration
from tender_system.core.ports import Clock
from tender_system.core.tenders import IdempotencyConflict
from tender_system.operator.operations import OperatorService, sanitized
from tender_system.operator.operator_contracts import OperationView, OperatorDocument, SourceWrite
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.operator.product_contracts import (
    CampaignDetail,
    CampaignView,
    ErrorResponse,
    Page,
    PageQuery,
)

LOG = logging.getLogger(__name__)


def create_app(
    sessions: sessionmaker[Session],
    config: Configuration | None = None,
    clock: Clock | None = None,
    runtime: OperatorRuntime | None = None,
    *,
    operator_mode: bool | None = None,
) -> FastAPI:
    config = config or Configuration()
    operator_mode = (
        operator_mode if operator_mode is not None else os.getenv("QUARRY_OPERATOR_MODE") == "true"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        if runtime:
            runtime.start()
        try:
            yield
        finally:
            if runtime:
                runtime.close()

    app = FastAPI(
        title="Quarry",
        version="1.0.0",
        responses={code: {"model": ErrorResponse} for code in (404, 409, 422, 500)},
        lifespan=lifespan,
    )

    @app.get("/health", include_in_schema=False)
    def health() -> JSONResponse:
        try:
            with sessions() as session:
                session.execute(text("select 1"))
            database_ok = True
        except SQLAlchemyError:
            database_ok = False
        worker_ok = runtime is not None and runtime.healthy()
        healthy = database_ok and worker_ok
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "healthy" if healthy else "unhealthy",
                "database": database_ok,
                "worker": worker_ok,
            },
        )

    def service() -> Iterator[OperatorService]:
        with sessions.begin() as session:
            yield OperatorService(session, config, (clock or SystemClock()).now())

    Service = Annotated[OperatorService, Depends(service, scope="function")]
    from tender_system.api.web import register_web

    register_web(app, service, runtime)
    from tender_system.api.accounts import register_accounts
    from tender_system.api.auth import register_auth, session_user

    database, current_user = register_auth(app, sessions, clock, operator_mode=operator_mode)
    register_accounts(app, database, current_user, config, runtime)
    from tender_system.operator.smtp_settings import register_smtp

    register_smtp(app, sessions)
    from tender_system.operator.ai_settings import register_ai_settings

    register_ai_settings(app, database)

    @app.get("/api/v1/web/sources/{key}", response_model=OperatorDocument)
    def source(key: str, svc: Service) -> OperatorDocument:
        return OperatorDocument(data=svc.source_detail(key))

    @app.patch("/api/v1/web/sources/{key}", response_model=OperatorDocument)
    def configure_source(key: str, body: SourceWrite, svc: Service) -> OperatorDocument:
        return OperatorDocument(data=svc.configure_source(key, body))

    @app.get("/api/v1/web/runs", response_model=OperatorDocument)
    def runs(svc: Service, source: str | None = None) -> OperatorDocument:
        return OperatorDocument(data=svc.runs(source))

    @app.get("/api/v1/web/runs/{key}", response_model=OperatorDocument)
    def run(key: UUID, svc: Service) -> OperatorDocument:
        return OperatorDocument(data=svc.run(key))

    @app.get("/api/v1/web/operations/{key}", response_model=OperationView)
    def operation(key: UUID, svc: Service) -> OperationView:
        return svc.operation(key)

    @app.get("/api/v1/web/runs/{key}/evidence", response_model=OperatorDocument)
    def evidence(key: UUID, svc: Service) -> OperatorDocument:
        return OperatorDocument(data=svc.evidence(key, runtime.evidence if runtime else None))

    @app.get("/api/v1/web/campaigns", response_model=Page[CampaignView])
    def campaigns(svc: Service, query: Annotated[PageQuery, Query()]) -> Page[CampaignView]:
        return svc.campaigns(query)

    @app.get("/api/v1/web/campaigns/{key}", response_model=CampaignDetail)
    def campaign(key: UUID, svc: Service) -> CampaignDetail:
        return svc.campaign_detail(key)

    auth_requests: OrderedDict[str, tuple[float, int]] = OrderedDict()

    @app.middleware("http")
    async def private_response(request: Request, call_next: RequestResponseEndpoint) -> Response:
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                expected = urlsplit(str(request.base_url))
                allowed = (parsed.scheme, parsed.netloc) == (expected.scheme, expected.netloc)
                if operator_mode:
                    allowed = parsed.scheme in {"http", "https"} and parsed.hostname in {
                        "localhost",
                        "127.0.0.1",
                        "::1",
                    }
            except ValueError:
                allowed = False
            if not allowed:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "external_origin",
                            "message": "Use the application origin",
                        }
                    },
                )
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(
                status_code=403,
                content={
                    "error": {"code": "external_origin", "message": "Use the application origin"}
                },
            )
        path = request.url.path
        if path in {"/api/v1/auth/login", "/api/v1/auth/register"} and request.method == "POST":
            client = request.client.host if request.client else "unknown"
            now = time.monotonic()
            started, count = auth_requests.get(client, (now, 0))
            if now - started >= 60:
                started, count = now, 0
            auth_requests[client] = (started, count + 1)
            auth_requests.move_to_end(client)
            if len(auth_requests) > 2048:
                auth_requests.popitem(last=False)
            if count >= 30:
                return JSONResponse(
                    status_code=429,
                    content={"error": {"code": "rate_limited", "message": "Try again in a minute"}},
                    headers={"Retry-After": "60"},
                )
        if path.startswith("/api/") and not path.startswith("/api/v1/auth/"):
            if not operator_mode:
                with sessions() as session:
                    actor = session_user(session, request, clock or SystemClock())
                    if actor is None:
                        return JSONResponse(
                            status_code=401,
                            content={
                                "error": {"code": "unauthenticated", "message": "Sign in required"}
                            },
                        )
                    if not path.startswith("/api/v1/account/") and actor.role != "admin":
                        return JSONResponse(
                            status_code=403,
                            content={
                                "error": {
                                    "code": "forbidden",
                                    "message": "Administrator access required",
                                }
                            },
                        )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={
                "error": {"code": code, "message": message, "fields": []},
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        return error(
            exc.status_code,
            codes.get(exc.status_code, "request_rejected"),
            str(sanitized(exc.detail)) if isinstance(exc.detail, str) else "Request unavailable",
        )

    @app.exception_handler(LookupError)
    async def forbidden(request: Request, exc: LookupError) -> JSONResponse:
        return error(404, "not_found", "Resource unavailable")

    def field_errors(exc: RequestValidationError | ValidationError, code: str) -> JSONResponse:
        fields = [
            {
                "field": ".".join(str(part) for part in item["loc"] if part != "body"),
                "code": item["type"],
                "message": str(sanitized(item["msg"])),
            }
            for item in exc.errors()[:12]
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": code,
                    "message": "; ".join(f"{item['field']}: {item['message']}" for item in fields),
                    "fields": fields,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith("/api/v1/web/"):
            return field_errors(exc, "invalid_request")
        return error(422, "invalid_request", "Invalid request fields or values")

    @app.exception_handler(ValidationError)
    async def invalid_configuration(request: Request, exc: ValidationError) -> JSONResponse:
        if request.url.path.startswith("/api/v1/web/"):
            return field_errors(exc, "invalid_configuration")
        return error(409, "conflict", "Request conflicts with current state")

    @app.exception_handler(IdempotencyConflict)
    @app.exception_handler(ValueError)
    async def conflict(request: Request, exc: ValueError) -> JSONResponse:
        return error(409, "conflict", "Request conflicts with current state")

    @app.exception_handler(Exception)
    async def failed(request: Request, exc: Exception) -> JSONResponse:
        LOG.error("Product request failed: %s", type(exc).__name__)
        return error(500, "internal_error", "Request could not be completed")

    return app
