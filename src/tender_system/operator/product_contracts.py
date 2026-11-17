"""Public product contracts; safe projections of backend records."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from tender_system.core.domain import CampaignCriteria, Contract, CriteriaVersion, Reason


class Page[T](Contract):
    items: tuple[T, ...]
    total: int
    offset: int
    limit: int


class PageQuery(Contract):
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=50, ge=1, le=100)


class CampaignWrite(Contract):
    criteria: CampaignCriteria
    active: bool = True
    expected_version_id: UUID | None = None


class ActiveWrite(Contract):
    active: bool


class CampaignView(Contract):
    id: UUID
    active: bool
    current_criteria_version_id: UUID
    criteria: CampaignCriteria
    criteria_number: int
    processing: int
    failed: int


class TenderView(Contract):
    id: UUID
    version_id: UUID
    title: str | None
    description: str | None
    buyer: str | None
    location: str | None
    value: Decimal | None
    currency: str | None
    deadline: datetime | None
    published_at: datetime | None
    observed_at: datetime
    source: str
    source_url: str | None
    status: str | None
    valid: bool
    validity_reasons: tuple[Reason, ...]


class CampaignDetail(Contract):
    campaign: CampaignView
    versions: tuple[CriteriaVersion, ...]


class FieldError(Contract):
    field: str
    code: str
    message: str


class ErrorDetail(Contract):
    code: str
    message: str
    fields: tuple[FieldError, ...] = ()


class ErrorResponse(Contract):
    error: ErrorDetail
