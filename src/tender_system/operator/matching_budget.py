"""Matching-only lifetime budgets; durable reservations survive crashes and ambiguous errors."""

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic, sleep
from uuid import UUID

from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tender_system.core.domain import CampaignCriteria, Contract
from tender_system.core.ports import ModelClient, ModelFailure, ModelRequest, ModelResponse
from tender_system.persistence.matching_storage import catalog_lock
from tender_system.persistence.storage import (
    CampaignRow,
    CriteriaRow,
    MatchingSpendRow,
    MatchJobRow,
    OperatorConfigRow,
)


class MatchingBudget(Contract):
    global_limit_usd: float = Field(default=0, ge=0, allow_inf_nan=False)
    # Operator-supplied conservative rates for the configured matching model.
    provider: str = ""
    model: str = ""
    input_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Explicit fallback when pricing is unavailable: lifetime request ceiling.
    unpriced_request_limit: int = Field(default=0, ge=0, le=100000)
    max_requests_per_run: int = Field(default=10, ge=1, le=100)
    max_tokens_per_run: int = Field(default=100000, ge=1, le=1000000)
    max_input_tokens: int = Field(default=16000, ge=1, le=100000)


class BudgetStopped(Exception):
    pass


def budget_settings(session: Session) -> MatchingBudget:
    row = session.get(OperatorConfigRow, "matching_budget")
    return MatchingBudget.model_validate(row.value if row else {})


class BudgetedClient:
    def __init__(
        self,
        client: Callable[[], ModelClient],
        sessions: sessionmaker[Session],
        job_id: UUID,
        campaign_id: UUID,
        run_id: UUID,
        provider: str,
        automatic: bool,
        enabled: bool = True,
    ) -> None:
        self.client, self.sessions = client, sessions
        self.job_id, self.campaign_id, self.run_id = job_id, campaign_id, run_id
        self.provider, self.automatic, self.enabled = provider, automatic, enabled
        self.stopped: str | None = None

    def generate(self, request: ModelRequest) -> ModelResponse:
        try:
            return self._generate(request)
        except BudgetStopped as error:
            self.stopped = str(error)
            raise ModelFailure("matching_budget_stopped") from None

    def _generate(self, request: ModelRequest) -> ModelResponse:
        if not self.enabled:
            raise BudgetStopped("AI matching disabled by server configuration.")
        # Match compact UTF-8 provider serialization, then bound tokens
        # by bytes. Include schema and conservative chat-framing headroom.
        tokens = len(
            json.dumps(dict(request.payload), ensure_ascii=False, separators=(",", ":")).encode()
        )
        tokens += len(json.dumps(request.output_schema or {}).encode()) + 4096
        request = replace(
            request, max_attempts=1, max_output_tokens=min(request.max_output_tokens, 600)
        )
        with self.sessions.begin() as session:
            catalog_lock(session)
            settings = budget_settings(session)
            campaign = session.get(CampaignRow, self.campaign_id, populate_existing=True)
            if campaign is None or not campaign.active or campaign.deleted_at is not None:
                raise BudgetStopped("Campaign is inactive or unavailable.")
            job = session.get(MatchJobRow, self.job_id, populate_existing=True)
            if (
                job is None
                or job.criteria_version_id != campaign.current_criteria_version_id
                or job.status != "running"
                or job.lease_expires_at is None
                or job.lease_expires_at <= datetime.now(UTC)
            ):
                raise BudgetStopped("Matching lease or criteria changed before the paid request.")
            version = session.get(CriteriaRow, campaign.current_criteria_version_id)
            assert version is not None
            criteria = CampaignCriteria.model_validate(version.criteria)
            if self.automatic and (
                not campaign.active
                or campaign.deleted_at is not None
                or not criteria.automatic_matching
                or criteria.automatic_mode != "semantic"
                or not criteria.automatic_ai_authorized
            ):
                raise BudgetStopped("Automatic AI authorization is disabled.")
            if session.get(MatchingSpendRow, self.job_id) is not None:
                raise BudgetStopped(
                    "Request already reserved or attempted; uncertain paid work is not repeated. "
                    "Use a forced manual evaluation to retry."
                )
            if tokens > settings.max_input_tokens:
                raise BudgetStopped("Input token limit reached. Evaluation remains pending.")
            run_rows = tuple(
                session.scalars(
                    select(MatchingSpendRow).where(MatchingSpendRow.run_id == self.run_id)
                )
            )
            if (
                len(run_rows) >= settings.max_requests_per_run
                or sum((r.input_tokens or 0) + (r.output_tokens or 0) for r in run_rows)
                + tokens
                + request.max_output_tokens
                > settings.max_tokens_per_run
            ):
                raise BudgetStopped(
                    "Per-run request/token limit reached. Resume remaining evaluations in Activity."
                )
            priced = (
                settings.provider == self.provider
                and settings.model == request.model
                and settings.input_usd_per_million is not None
                and settings.input_usd_per_million > 0
                and settings.output_usd_per_million is not None
                and settings.output_usd_per_million > 0
            )
            if session.scalar(
                select(MatchingSpendRow.job_id).where(MatchingSpendRow.state == "overrun").limit(1)
            ):
                raise BudgetStopped(
                    "Provider usage exceeded reserved bounds. AI paused; "
                    "inspect usage and pricing before continuing."
                )
            self.prices = (settings.input_usd_per_million, settings.output_usd_per_million)
            if priced:
                assert settings.input_usd_per_million is not None
                assert settings.output_usd_per_million is not None
                reserve = (
                    Decimal(tokens) * Decimal(str(settings.input_usd_per_million))
                    + Decimal(request.max_output_tokens)
                    * Decimal(str(settings.output_usd_per_million))
                ) / 1000000
                total = session.scalar(
                    select(func.coalesce(func.sum(MatchingSpendRow.accounted_usd), 0))
                )
                campaign_total = session.scalar(
                    select(func.coalesce(func.sum(MatchingSpendRow.accounted_usd), 0)).where(
                        MatchingSpendRow.campaign_id == self.campaign_id
                    )
                )
                if reserve + (total or Decimal(0)) > Decimal(str(settings.global_limit_usd)) or (
                    criteria.ai_budget_usd is not None
                    and reserve + (campaign_total or Decimal(0))
                    > Decimal(str(criteria.ai_budget_usd))
                ):
                    raise BudgetStopped(
                        "Global or campaign AI budget exhausted. Increase the limit, "
                        "then resume pending work in Activity."
                    )
            else:
                count = session.scalar(select(func.count()).select_from(MatchingSpendRow)) or 0
                campaign_count = (
                    session.scalar(
                        select(func.count())
                        .select_from(MatchingSpendRow)
                        .where(MatchingSpendRow.campaign_id == self.campaign_id)
                    )
                    or 0
                )
                if (
                    not settings.unpriced_request_limit
                    or count >= settings.unpriced_request_limit
                    or (
                        criteria.ai_request_limit is not None
                        and campaign_count >= criteria.ai_request_limit
                    )
                ):
                    raise BudgetStopped(
                        "Reliable model pricing unavailable. Configure matching prices or an "
                        "explicit lifetime request ceiling before AI can run."
                    )
                # No fabricated dollar estimate for unpriced calls.
                reserve = Decimal(0)
            session.add(
                MatchingSpendRow(
                    job_id=self.job_id,
                    campaign_id=self.campaign_id,
                    run_id=self.run_id,
                    reserved_usd=reserve,
                    accounted_usd=reserve,
                    state="reserved" if priced else "unpriced",
                    input_tokens=tokens,
                    output_tokens=request.max_output_tokens,
                )
            )
        # Reservation committed before network I/O. Retry only explicit pre-completion
        # rate-limit rejections; ambiguous timeouts/5xx must not repeat paid work.
        deadline = monotonic() + request.timeout_seconds
        errors: list[str] = []
        try:
            client = self.client()
            for attempt in range(1, 4):
                try:
                    response = client.generate(
                        replace(request, timeout_seconds=max(0.001, deadline - monotonic()))
                    )
                    response = replace(
                        response,
                        attempts=response.attempts + len(errors),
                        attempt_errors=tuple(errors) + response.attempt_errors,
                    )
                    break
                except ModelFailure as error:
                    errors.append(error.code)
                    wait = max(0.5 * 2 ** (attempt - 1), error.retry_after_seconds or 0)
                    if (
                        error.code != "rate_limited"
                        or error.http_status != 429
                        or error.metadata is not None
                        and bool(
                            error.metadata.input_tokens
                            or error.metadata.output_tokens
                            or error.metadata.cost
                        )
                        or attempt == 3
                        or wait + 0.05 >= deadline - monotonic()
                    ):
                        error.metadata = replace(
                            error.metadata or ModelResponse(output={}, model_version=request.model),
                            attempts=attempt,
                            attempt_errors=tuple(errors),
                        )
                        raise
                    sleep(wait)
            else:
                raise AssertionError("bounded retry exhausted")
        except ModelFailure as error:
            self._reconcile(error.metadata, priced, failed=True)
            raise
        except Exception:
            self._reconcile(None, priced, failed=True)
            raise
        self._reconcile(response, priced, failed=False)
        return response

    def _reconcile(self, response: ModelResponse | None, priced: bool, *, failed: bool) -> None:
        with self.sessions.begin() as session:
            catalog_lock(session)
            row = session.get(MatchingSpendRow, self.job_id)
            assert row is not None
            known_usage = (
                response is not None
                and response.input_tokens is not None
                and response.output_tokens is not None
            )
            row.state = (
                "unpriced"
                if not priced
                else "uncertain"
                if failed or not known_usage
                else "estimated"
            )
            if response is not None:
                accounted = row.reserved_usd
                if priced and known_usage:
                    assert response.input_tokens is not None and response.output_tokens is not None
                    accounted = (
                        Decimal(response.input_tokens) * Decimal(str(self.prices[0]))
                        + Decimal(response.output_tokens) * Decimal(str(self.prices[1]))
                    ) / 1000000
                if priced:
                    accounted = max(accounted, Decimal(str(response.cost or 0)))
                    row.accounted_usd = max(row.reserved_usd, accounted) if failed else accounted
                if (
                    (priced and row.accounted_usd > row.reserved_usd)
                    or (response.input_tokens or 0) > (row.input_tokens or 0)
                    or (response.output_tokens or 0) > (row.output_tokens or 0)
                ):
                    row.state = "overrun"
                if not priced and response.cost is not None:
                    row.accounted_usd = Decimal(str(response.cost))
                if response.input_tokens is not None:
                    row.input_tokens = response.input_tokens
                if response.output_tokens is not None:
                    row.output_tokens = response.output_tokens
