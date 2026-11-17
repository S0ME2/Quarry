"""Provider- and storage-independent data contracts. No orchestration here."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    computed_field,
    field_validator,
    model_validator,
)


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


NonEmpty = Annotated[str, Field(min_length=1), AfterValidator(nonblank)]
Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class TenderIdentity(Contract):
    source_id: NonEmpty
    external_tender_id: NonEmpty | None = None
    source_has_stable_external_id: bool = True
    fallback_kind: Literal["url", "fingerprint"] | None = None
    fallback_key: NonEmpty | None = None

    @model_validator(mode="after")
    def check_identity(self) -> Self:
        if self.external_tender_id is not None:
            if not self.source_has_stable_external_id:
                raise ValueError("source without stable IDs cannot use an unstable external ID")
            if self.fallback_kind is not None or self.fallback_key is not None:
                raise ValueError("external ID identity cannot include a fallback")
        elif self.source_has_stable_external_id:
            raise ValueError("missing expected stable external ID; fallback is forbidden")
        elif self.fallback_kind is None or self.fallback_key is None:
            raise ValueError(
                "source without a stable external ID requires explicit fallback identity"
            )
        return self


class TenderCandidate(TenderIdentity):
    source_url: NonEmpty | None = None
    title: NonEmpty | None = None
    description: str | None = None
    buyer: str | None = None
    location: str | None = None
    published_at: AwareDatetime | None = None
    deadline: AwareDatetime | None = None
    value: Money | None = None
    currency: str | None = None
    status: str | None = None
    document_urls: tuple[str, ...] = ()
    product_items: tuple[dict[str, JsonValue], ...] | None = None
    raw_data: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    quality_issues: tuple["DataQualityIssue", ...] = ()

    @field_validator("published_at", "deadline", mode="before")
    @classmethod
    def reject_implicit_epoch(cls, value: object) -> object:
        if isinstance(value, (int, float, bool)):
            raise ValueError("tender dates require explicit aware datetime or ISO representation")
        return value

    @field_validator("published_at", "deadline")
    @classmethod
    def utc_dates(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class DataQualityIssue(Contract):
    field: NonEmpty
    code: NonEmpty
    detail: NonEmpty
    severity: Literal["error", "warning"] = "error"


class NormalizationResult(Contract):
    candidate: TenderCandidate | None
    issues: tuple[DataQualityIssue, ...]
    raw_data: dict[str, JsonValue] = Field(repr=False)


class TenderPresence(Contract):
    missing_successful_runs: Annotated[int, Field(ge=0)] = 0
    last_accounted_run_sequence: Annotated[int, Field(ge=0)] | None = None


class Tender(TenderCandidate):
    document_text: str = ""
    documents_incomplete: bool = False
    internal_id: UUID
    current_version_id: UUID
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    latest_observation_id: UUID | None = None
    presence: TenderPresence = Field(default_factory=TenderPresence)


class TenderVersion(Contract):
    id: UUID
    tender_id: UUID
    previous_version_id: UUID | None
    changed_fields: dict[str, JsonValue]
    observed_at: AwareDatetime
    evidence_reference: NonEmpty
    observation_id: UUID | None = None


class TenderObservation(Contract):
    id: UUID
    candidate: TenderCandidate
    observed_at: AwareDatetime
    evidence_reference: NonEmpty
    source_run_id: UUID | None = None


class TenderWriteResult(Contract):
    tender: Tender
    comparison: "TenderComparison"
    replayed: bool = False


class DeadlineRules(Contract):
    min_days_remaining: Annotated[int, Field(ge=0)] | None = None
    max_days_remaining: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def check_range(self) -> Self:
        if (
            self.min_days_remaining is not None
            and self.max_days_remaining is not None
            and self.min_days_remaining > self.max_days_remaining
        ):
            raise ValueError("minimum days remaining exceeds maximum")
        return self


class NotificationSettings(Contract):
    enabled: bool = True
    channel: Literal["telegram", "email"] = "telegram"
    email: Annotated[str, Field(max_length=254)] | None = None
    chat_id: (
        Annotated[str, Field(pattern=r"^(?:-?[1-9][0-9]{0,19}|@[A-Za-z][A-Za-z0-9_]{4,31})$")]
        | None
    ) = None

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str | None) -> str | None:
        if value is not None and (
            not re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}",
                value,
            )
        ):
            raise ValueError("Invalid notification email address")
        return value

    @model_validator(mode="after")
    def email_destination(self) -> Self:
        if self.enabled and self.channel == "email" and not self.email:
            raise ValueError("Email notifications require a destination")
        return self


class CampaignCriteria(Contract):
    schema_version: Literal[1] = 1
    name: NonEmpty
    description: str = ""
    source_ids: tuple[NonEmpty, ...] = ()
    include_keywords: tuple[NonEmpty, ...] = ()
    exclude_keywords: tuple[NonEmpty, ...] = ()
    min_value: Money | None = None
    max_value: Money | None = None
    currencies: tuple[NonEmpty, ...] = ()
    buyers: tuple[NonEmpty, ...] = ()
    exclude_buyers: tuple[NonEmpty, ...] = ()
    locations: tuple[NonEmpty, ...] = ()
    deadline_rules: DeadlineRules = Field(default_factory=DeadlineRules)
    status_rules: tuple[NonEmpty, ...] = ()
    matching_mode: Literal["broad", "balanced", "strict"] = "balanced"
    custom_notes: str = ""
    automatic_matching: bool = False
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    automatic_mode: Literal["rules", "semantic"] = "rules"
    automatic_ai_authorized: bool = False
    # None inherits the current administrator policy; numeric values are explicit caps.
    ai_request_limit: Annotated[int, Field(ge=0, le=100000)] | None = 0
    ai_budget_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = 0

    @model_validator(mode="after")
    def check_range(self) -> Self:
        if self.min_value is not None and self.max_value is not None:
            if self.min_value > self.max_value:
                raise ValueError("minimum value exceeds maximum")
        return self


class CriteriaVersion(Contract):
    id: UUID
    campaign_id: UUID
    criteria: CampaignCriteria
    created_at: AwareDatetime
    previous_version_id: UUID | None = None
    number: Annotated[int, Field(gt=0)] = 1


class Campaign(Contract):
    id: UUID
    active: bool = True
    current_criteria_version_id: UUID


class Reason(Contract):
    code: NonEmpty
    detail: NonEmpty
    field: str | None = None


class MatchEvidence(Contract):
    field: NonEmpty
    quote: NonEmpty


class ModelOpinion(Contract):
    decision: Literal["match", "no_match", "unsure"]
    confidence: Score
    explanation: NonEmpty


class EvidenceVerification(Contract):
    status: Literal["not_provided", "verified", "invalid"]
    error_code: NonEmpty | None = None
    detail: NonEmpty | None = None


class ModelCall(Contract):
    stage: Literal["semantic", "escalation"]
    provider: NonEmpty
    model: NonEmpty
    model_version: NonEmpty | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    error_code: NonEmpty | None = None
    failure_stage: NonEmpty | None = None
    failure_detail: NonEmpty | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    attempts: int = Field(default=1, ge=1, le=3)
    attempt_errors: tuple[NonEmpty, ...] = ()


class MatchResult(Contract):
    schema_version: Literal[1] = 1
    result: Literal["match", "no_match", "unsure"]
    score: Score
    reasons: tuple[Reason, ...]
    uncertainties: tuple[Reason, ...]
    confidence: Score | None = None
    matcher_implementation: NonEmpty | None = None
    matcher_version: NonEmpty | None = None
    tender_version_id: UUID | None = None
    criteria_version_id: UUID | None = None
    decision_stage: Literal["rules", "semantic", "escalation"] | None = None
    evidence: tuple[MatchEvidence, ...] = ()
    model_calls: tuple[ModelCall, ...] = ()
    model_opinion: ModelOpinion | None = None
    evidence_verification: EvidenceVerification | None = None

    @model_validator(mode="before")
    @classmethod
    def read_diagnostic(cls, value: object) -> object:
        # Read-only projections are recomputed, including when loading legacy JSON.
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k not in {"processing_status", "relevance"}}
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def processing_status(self) -> Literal["finalized", "needs_review", "failed"]:
        if any(call.error_code for call in self.model_calls):
            return "failed"
        if self.result == "unsure" or (
            self.evidence_verification is not None
            and self.evidence_verification.status == "invalid"
        ):
            return "needs_review"
        return "finalized"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def relevance(self) -> Literal["RELEVANT", "NOT_RELEVANT"] | None:
        """FR-16 result exists only after evaluation completes successfully."""
        if self.processing_status != "finalized":
            return None
        return "RELEVANT" if self.result == "match" else "NOT_RELEVANT"


class MatchJob(Contract):
    id: UUID
    tender_id: UUID
    tender_version_id: UUID
    campaign_id: UUID
    criteria_version_id: UUID
    matcher_version: NonEmpty
    matcher_implementation: NonEmpty = "rules"
    matcher_config: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    settings_hash: str = ""
    status: Literal["pending", "running", "done", "failed"] = "pending"
    attempts: Annotated[int, Field(ge=0)] = 0
    created_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    available_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    max_attempts: Annotated[int, Field(gt=0)] = 3
    last_error_code: str | None = None


class StoredMatchResult(Contract):
    id: UUID
    job_id: UUID
    tender_id: UUID
    tender_version_id: UUID
    campaign_id: UUID
    criteria_version_id: UUID
    matcher_version: NonEmpty
    matcher_implementation: NonEmpty = "rules"
    settings_hash: str = ""
    outcome: MatchResult
    created_at: AwareDatetime
    completion_token: UUID | None = None


class HttpStep(Contract):
    url: JsonValue
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    headers: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    query: dict[str, JsonValue] = Field(default_factory=dict)
    body: JsonValue = Field(default=None, repr=False)
    output: NonEmpty = "response"
    accepted_statuses: tuple[int, ...] = (200,)
    encoding: NonEmpty = "utf-8"
    inventory_page: bool = False

    @model_validator(mode="after")
    def valid_request(self) -> Self:
        if not (isinstance(self.url, str) and self.url.strip()) and not (
            isinstance(self.url, dict)
            and set(self.url) == {"$ref"}
            and isinstance(self.url["$ref"], str)
        ):
            raise ValueError("HTTP URL requires literal text or reference")
        if not self.accepted_statuses or any(
            status < 200 or status >= 400 for status in self.accepted_statuses
        ):
            raise ValueError(
                "accepted HTTP statuses must be successful or explicitly handled redirects"
            )
        return self


class BrowserStep(Contract):
    action: Literal[
        "navigate",
        "click",
        "type",
        "select",
        "scroll",
        "content",
        "network",
        "download",
        "restore_session",
        "save_session",
        "wait_for_selector",
        "wait_for_response",
    ]
    value: JsonValue = None
    selector: str | None = None
    output: NonEmpty = "browser"
    inventory_page: bool = False

    @model_validator(mode="after")
    def valid_action(self) -> Self:
        if self.action in {"click", "type", "select", "wait_for_selector"} and not self.selector:
            raise ValueError("browser action requires selector")
        if (
            self.action
            in {"navigate", "type", "select", "download", "restore_session", "wait_for_response"}
            and self.value is None
        ):
            raise ValueError("browser action requires value")
        return self


class ExtractField(Contract):
    selector: str = "."
    attribute: str | None = None
    required: bool = False
    many: bool = False
    direct_text: bool = False


class ExtractStep(Contract):
    input: JsonValue
    format: Literal["json", "html", "xml"]
    selector: str = "."
    fields: dict[str, ExtractField] = Field(default_factory=dict)
    namespaces: dict[str, str] = Field(default_factory=dict)
    output: NonEmpty = "items"
    emit: bool = False
    require_match: bool = True


class TransformStep(Contract):
    name: NonEmpty
    input: JsonValue = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    output: NonEmpty = "value"
    emit: bool = False


class LoopStep(Contract):
    items: JsonValue = None
    variable: NonEmpty = "item"
    until: JsonValue = None
    # Legacy explicit ceilings remain safety limits; omitted ceilings use the run budget.
    max_iterations: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.items is None and self.until is None:
            raise ValueError("loop requires items or an explicit termination condition")
        return self


class ConditionStep(Contract):
    predicate: JsonValue


class CustomStep(Contract):
    name: NonEmpty
    input: JsonValue = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    output: NonEmpty = "custom"
    emit: bool = False


RecipeParameters = (
    HttpStep | BrowserStep | ExtractStep | TransformStep | LoopStep | ConditionStep | CustomStep
)
STEP_PARAMETERS: dict[str, type[Contract]] = {
    "http_request": HttpStep,
    "browser_action": BrowserStep,
    "extract": ExtractStep,
    "transform": TransformStep,
    "loop": LoopStep,
    "condition": ConditionStep,
    "custom": CustomStep,
}


class RecipeStep(Contract):
    type: Literal[
        "http_request", "browser_action", "extract", "transform", "loop", "condition", "custom"
    ]
    parameters: RecipeParameters
    steps: tuple["RecipeStep", ...] = ()
    else_steps: tuple["RecipeStep", ...] = ()

    @model_validator(mode="before")
    @classmethod
    def typed_parameters(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("type") in STEP_PARAMETERS:
            value = value.copy()
            value["parameters"] = STEP_PARAMETERS[value["type"]].model_validate(
                value.get("parameters", {})
            )
        return value

    @model_validator(mode="after")
    def valid_structure(self) -> Self:
        if type(self.parameters) is not STEP_PARAMETERS[self.type]:
            raise ValueError("step parameters do not match step type")
        if self.type not in {"loop", "condition"} and (self.steps or self.else_steps):
            raise ValueError("only loop and condition may contain child steps")
        if self.type == "loop" and (not self.steps or self.else_steps):
            raise ValueError("loop requires steps and cannot have else steps")
        if self.type == "condition" and not self.steps:
            raise ValueError("condition requires then steps")
        return self


CompletionKind = Literal[
    "snapshot",
    "final_page",
    "next_cursor_absent",
    "total_reconciled",
    "end_marker",
    "empty_terminal_page",
]


class CompletionSpec(Contract):
    kind: CompletionKind
    value: JsonValue

    @model_validator(mode="after")
    def source_signal_reference(self) -> Self:
        if self.kind in {"next_cursor_absent", "final_page", "end_marker", "empty_terminal_page"}:
            if not isinstance(self.value, dict) or set(self.value) != {"$ref"}:
                raise ValueError("terminal source signals require an explicit $ref to evidence")
            if not isinstance(self.value["$ref"], str) or not self.value["$ref"].strip():
                raise ValueError("terminal source signal reference must not be blank")
        return self


class CompletionEvidence(Contract):
    kind: CompletionKind
    observed: bool
    value: JsonValue
    reference: NonEmpty


class ContentSignal(Contract):
    kind: Literal["session_expired", "maintenance", "valid_empty"]
    signature: NonEmpty
    reference: NonEmpty


class RunDiagnostics(Contract):
    adapters: dict[str, str] = Field(default_factory=dict)
    pages: tuple[str, ...] = ()
    content_signals: tuple[ContentSignal, ...] = ()
    transport_attempts: Annotated[int, Field(ge=0)] = 0
    browser_accounting_complete: bool = Field(default=True, exclude_if=lambda v: v)
    browser_requests: int = Field(default=0, ge=0, exclude_if=lambda v: not v)
    browser_asset_requests: int = Field(default=0, ge=0, exclude_if=lambda v: not v)
    raw_count: Annotated[int, Field(ge=0)] = 0
    completion: CompletionEvidence | None = None
    inventory_totals: tuple[int, ...] = Field(default=(), exclude_if=lambda v: not v)
    inventory_snapshots: tuple[str, ...] = Field(default=(), exclude_if=lambda v: not v)
    response_bytes: int = Field(default=0, ge=0, exclude_if=lambda v: not v)


class HealthReason(Contract):
    code: NonEmpty
    dimension: NonEmpty
    detail: NonEmpty
    severity: Literal["info", "warning", "error"]
    observed: JsonValue = None
    threshold: JsonValue = None
    evidence_reference: str | None = None


class FreshnessDecision(Contract):
    status: Literal["fresh", "aging", "stale", "unknown"]
    basis: Literal["observed", "inventory_timestamp", "unknown"]
    timestamp: AwareDatetime | None = None
    observed_at: AwareDatetime | None
    age_seconds: float | None = None
    evidence_reference: str | None = None
    evaluated_at: AwareDatetime
    max_age_seconds: float | None = None
    aging_after_seconds: float | None = None
    timestamp_meaning: str | None = None


HealthDimension = Literal["pass", "warning", "fail", "unknown", "not_applicable"]


class HealthDecision(Contract):
    source_id: NonEmpty
    run_id: UUID
    policy: NonEmpty
    calibration: Literal["provisional", "calibrated"]
    overall_status: Literal["healthy", "degraded", "quarantined", "rejected", "unknown"]
    dimensions: dict[str, HealthDimension]
    completeness: Literal["complete", "incomplete", "unknown"]
    inventory_mode: Literal["full", "incremental", "sampled", "unknown"]
    empty_classification: Literal["not_empty", "valid_empty", "suspicious_empty", "unknown_empty"]
    freshness: FreshnessDecision
    publication_allowed: bool
    absence_evidence_allowed: bool
    raw_count: int = Field(ge=0)
    normalized_count: int = Field(ge=0)
    malformed_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    diagnostics: RunDiagnostics
    reasons: tuple[HealthReason, ...]


class SourceSnapshot(Contract):
    sequence: int = Field(default=0, ge=0)
    checkpoint: JsonValue = None
    last_full_count: int | None = Field(default=None, ge=0)
    last_good_evidence_reference: str | None = None
    last_full_verified_at: AwareDatetime | None = None
    last_full_freshness_at: AwareDatetime | None = None
    last_full_evidence_reference: str | None = None
    last_full_policy: str | None = None
    last_full_freshness_basis: Literal["observed", "inventory_timestamp", "unknown"] = "unknown"
    last_incremental_at: AwareDatetime | None = None
    last_source_timestamp: AwareDatetime | None = None
    accepted_full_runs: int = Field(default=0, ge=0)


class SourceSearch(Contract):
    """Alternate traversal; search_keyword is supplied once per campaign keyword."""

    steps: tuple[RecipeStep, ...] = Field(min_length=1)


class SourceRecipe(Contract):
    schema_version: Literal[1] = 1
    source_id: NonEmpty
    version: NonEmpty
    steps: tuple[RecipeStep, ...] = Field(min_length=1)
    search: SourceSearch | None = Field(default=None, exclude_if=lambda v: v is None)
    coverage: Literal["full", "incremental"] | dict[str, JsonValue] = "full"
    completion: JsonValue = True
    completion_signal: CompletionSpec | None = None
    checkpoint: JsonValue = None
    expected_count: JsonValue = None
    data_as_of: JsonValue = None
    inventory_snapshot: JsonValue = Field(default=None, exclude_if=lambda v: v is None)
    inventory_total: JsonValue = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def limits(self) -> Self:
        for label, value in (
            ("snapshot", self.inventory_snapshot),
            ("total", self.inventory_total),
        ):
            if value is not None and not (
                isinstance(value, dict)
                and set(value) == {"$ref"}
                and isinstance(value["$ref"], str)
                and value["$ref"].strip()
            ):
                raise ValueError(
                    f"inventory {label} requires an explicit source evidence reference"
                )
        if isinstance(self.coverage, dict) and (
            set(self.coverage) != {"$ref"} or not isinstance(self.coverage["$ref"], str)
        ):
            raise ValueError("coverage requires full, incremental or a reference")
        if type(self.completion) is not bool and not (
            isinstance(self.completion, dict)
            and set(self.completion) == {"$ref"}
            and isinstance(self.completion["$ref"], str)
        ):
            raise ValueError("completion requires boolean or a reference")
        count = 0

        def visit(steps: tuple[RecipeStep, ...], depth: int) -> None:
            nonlocal count
            count += len(steps)
            if depth > 20 or count > 1000:
                raise ValueError("recipe exceeds nesting/step limits")
            for step in steps:
                visit(step.steps + step.else_steps, depth + 1)

        visit(self.steps, 0)
        if self.search is not None:
            visit(self.search.steps, 0)
        return self


class SourceRecord(Contract):
    id: NonEmpty
    name: NonEmpty
    has_stable_external_id: bool = True


class CollectionRun(Contract):
    id: UUID
    source_id: NonEmpty
    recipe_version: NonEmpty
    started_at: AwareDatetime
    finished_at: AwareDatetime
    candidates: tuple[TenderCandidate, ...]
    complete: bool
    evidence_reference: NonEmpty
    coverage: Literal["full", "incremental", "sampled"] = "full"
    purpose: Literal["collection", "fallback"] = "collection"
    session_reference: str | None = Field(default=None, repr=False)
    raw_observations: tuple[dict[str, JsonValue], ...] = Field(default=(), repr=False)
    checkpoint: JsonValue = None
    expected_count: Annotated[int, Field(ge=0)] | None = None
    quality_issues: tuple[DataQualityIssue, ...] = ()
    data_as_of: AwareDatetime | None = None
    diagnostics: RunDiagnostics = Field(default_factory=RunDiagnostics)


class ExecutionLimits(Contract):
    """Operator scope; independent of recipe inventory and incremental semantics."""

    max_tenders: int | None = Field(default=None, ge=1)
    max_requests: int = Field(default=10000, ge=1, le=100000)
    max_duration_seconds: float = Field(default=600, gt=0, allow_inf_nan=False)
    max_response_bytes: int = Field(default=10_000_000, ge=1)
    max_total_response_bytes: int = Field(default=100_000_000, ge=1)


class BoundedCollectionResult(Contract):
    """Explicit partial execution result; cannot be mistaken for a complete run."""

    id: UUID
    source_id: NonEmpty
    recipe_version: NonEmpty
    observations: tuple[dict[str, JsonValue], ...] = Field(repr=False)
    evidence_reference: NonEmpty
    diagnostics: RunDiagnostics
    expected_count: Annotated[int, Field(ge=0)] | None = None
    stop_reason: Literal["bounded_smoke_completed"] = "bounded_smoke_completed"
    started_at: AwareDatetime
    finished_at: AwareDatetime
    data_as_of: AwareDatetime | None = None

    def sampled_run(self) -> CollectionRun:
        return CollectionRun(
            id=self.id,
            source_id=self.source_id,
            recipe_version=self.recipe_version,
            started_at=self.started_at,
            finished_at=self.finished_at,
            candidates=(),
            complete=False,
            coverage="sampled",
            evidence_reference=self.evidence_reference,
            raw_observations=self.observations,
            expected_count=self.expected_count,
            diagnostics=self.diagnostics,
            data_as_of=self.data_as_of,
        )


class SourceFailure(Contract):
    run_id: UUID
    source_id: NonEmpty
    recipe: SourceRecipe
    error_code: NonEmpty
    error_detail: NonEmpty
    failed_evidence_reference: NonEmpty
    last_good_evidence_reference: str | None = None
    recent_evidence_references: tuple[str, ...] = ()
    authorized_session_reference: str | None = None
    failed_step: str | None = None
    issues: tuple[DataQualityIssue, ...] = ()
    diagnostics: RunDiagnostics = Field(default_factory=RunDiagnostics)
    health: HealthDecision | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    bad_output: tuple[dict[str, JsonValue], ...] = Field(default=(), repr=False)
    recent_runs: tuple[dict[str, JsonValue], ...] = ()
    last_good_run: dict[str, JsonValue] | None = None


class RepairActivationMode(StrEnum):
    AUTOMATIC = "automatic"
    LIMITED = "limited"
    HUMAN = "human"


class PluginChange(Contract):
    name: NonEmpty
    artifact_reference: NonEmpty
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    explanation: NonEmpty


class FixResult(Contract):
    status: Literal["fixed", "fallback", "failed"]
    updated_recipe: SourceRecipe | None = None
    custom_code_reference: str | None = None
    explanation: NonEmpty
    tests_performed: tuple[str, ...] = ()
    confidence: Score
    fallback_method: str | None = None
    plugin_changes: tuple[PluginChange, ...] = ()
    investigation_evidence_references: tuple[NonEmpty, ...] = ()
    escalation: tuple[NonEmpty, ...] = ()


class RepairValidation(Contract):
    baseline_sequence: Annotated[int, Field(ge=0)] | None = None
    passed: bool
    checks: tuple[Reason, ...]
    confidence: Score
    evidence_references: tuple[str, ...]


class ValidityDecision(Contract):
    matchable: bool
    reasons: tuple[Reason, ...]


class TenderComparison(Contract):
    classification: Literal["same", "new", "changed"]
    changed_fields: dict[str, JsonValue]
    requires_rematch: bool


class ExecutionContext(Contract):
    run_id: UUID
    limits: ExecutionLimits | None = None
    values: dict[str, JsonValue] = Field(default_factory=dict)


class JobLease(Contract):
    job: MatchJob
    token: UUID
    expires_at: AwareDatetime


class MatchingInput(Contract):
    tender: Tender
    criteria: CriteriaVersion


class MatchBatchReport(Contract):
    claimed: int = 0
    completed: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0
    lost_leases: int = 0


class RecipeRevision(Contract):
    recipe: SourceRecipe
    plugin_changes: tuple[PluginChange, ...] = ()


class SampleVerification(Contract):
    checked: int = Field(ge=0)
    mismatches: tuple[Reason, ...] = ()
    evidence_references: tuple[NonEmpty, ...] = ()
