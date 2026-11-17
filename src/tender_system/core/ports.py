"""Small replacement boundaries. Implementations import these, never the reverse."""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import JsonValue

from tender_system.core.configuration import CampaignSettings, SourceSettings
from tender_system.core.domain import (
    Campaign,
    CampaignCriteria,
    CollectionRun,
    CriteriaVersion,
    ExecutionContext,
    JobLease,
    MatchingInput,
    MatchJob,
    MatchResult,
    NormalizationResult,
    RecipeRevision,
    RecipeStep,
    SampleVerification,
    SourceFailure,
    SourceRecipe,
    SourceRecord,
    SourceSnapshot,
    StoredMatchResult,
    Tender,
    TenderCandidate,
    TenderComparison,
    TenderIdentity,
    TenderObservation,
    TenderVersion,
    TenderWriteResult,
    ValidityDecision,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class HttpRequest:
    url: str = field(repr=False)
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes | None = field(default=None, repr=False)
    timeout_seconds: float = 30
    max_response_bytes: int = 10_000_000
    max_requests: int = 10_000


@dataclass(frozen=True)
class HttpResponse:
    url: str = field(repr=False)
    status_code: int
    body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    requests_made: int = 1
    bytes_received: int | None = None


class HttpAdapter(Protocol):
    def request(self, request: HttpRequest) -> HttpResponse: ...


@dataclass(frozen=True)
class NetworkExchange:
    request: HttpRequest
    response: HttpResponse | None
    capture_error: str | None = None


class BrowserAdapter(Protocol):
    """One authorized browser session, owned and closed by the caller.

    Implementations enforce session request/retained-byte budgets and I/O deadlines,
    report omitted capture bodies explicitly and surface budget failures to callers.
    """

    def navigate(self, url: str) -> None: ...
    def current_url(self) -> str: ...
    def click(self, selector: str) -> None: ...
    def type_text(self, selector: str, text: str) -> None: ...
    def select(self, selector: str, value: str) -> None: ...
    def scroll(self, pixels: int) -> None: ...
    def content(self) -> str: ...
    def network(self, include_request_details: bool = False) -> tuple[NetworkExchange, ...]: ...
    def screenshot(self) -> bytes: ...
    def trace(self) -> bytes: ...
    def download(self, url: str) -> bytes: ...
    def restore_session(self, authorized_reference: str) -> None: ...
    def save_session(self) -> str: ...
    def session_state(self) -> Mapping[str, JsonValue]: ...
    def wait_for_selector(self, selector: str) -> None: ...
    def wait_for_response(self, url_pattern: str) -> None: ...
    def close(self) -> None: ...


class EvidenceStore(Protocol):
    def put(self, run_id: UUID, evidence: Mapping[str, JsonValue]) -> str: ...
    def get(self, reference: str) -> Mapping[str, JsonValue]: ...


class SessionStore(Protocol):
    def save(self, state: str) -> str: ...
    def load(self, reference: str) -> str: ...


@dataclass(frozen=True)
class ModelRequest:
    model: str
    task: str
    payload: Mapping[str, JsonValue] = field(repr=False)
    timeout_seconds: float = 30
    max_output_tokens: int = 2000
    max_attempts: int = 3
    output_schema: Mapping[str, JsonValue] | None = None
    array_output_key: str | None = None
    validate_output: Callable[["ModelResponse"], object] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ModelResponse:
    output: Mapping[str, JsonValue] = field(repr=False)
    model_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    attempts: int = 1
    attempt_errors: tuple[str, ...] = ()


class ModelFailure(Exception):
    """Sanitized provider failure; never carries response bodies or credentials."""

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        metadata: ModelResponse | None = None,
        failure_stage: str | None = None,
        failure_detail: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code, self.http_status = code, http_status
        self.retry_after_seconds, self.metadata = retry_after_seconds, metadata
        self.failure_stage = failure_stage
        self.failure_detail = failure_detail


class ModelClient(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...


class SourceRecipeLoader(Protocol):
    def load(self, reference: str) -> SourceRecipe: ...


class StepExecutor(Protocol):
    def execute(self, step: RecipeStep, context: ExecutionContext) -> ExecutionContext: ...


class Transform(Protocol):
    def __call__(self, value: JsonValue, parameters: Mapping[str, JsonValue]) -> JsonValue: ...


class TenderNormalizer(Protocol):
    def normalize(
        self, raw: Mapping[str, JsonValue], source: SourceRecord, settings: SourceSettings
    ) -> NormalizationResult: ...


class SourceExecutor(Protocol):
    def execute(
        self, recipe: SourceRecipe, context: ExecutionContext
    ) -> CollectionRun | SourceFailure: ...


class TenderComparator(Protocol):
    def compare(
        self, stored: Tender | None, candidate: TenderCandidate, rematch_fields: tuple[str, ...]
    ) -> TenderComparison: ...


class TenderValidityPolicy(Protocol):
    def evaluate(
        self, tender: Tender, settings: SourceSettings, now: datetime
    ) -> ValidityDecision: ...


class Matcher(Protocol):
    @property
    def implementation(self) -> str: ...
    @property
    def version(self) -> str: ...
    def match(self, tender: Tender, criteria: CampaignCriteria) -> MatchResult: ...


class MatcherFactory(Protocol):
    def __call__(self, settings: CampaignSettings) -> Matcher: ...


class MatchingWorkStore(Protocol):
    """Short transactions only; matcher invocation happens outside this boundary."""

    def claim(self, limit: int, now: datetime) -> tuple[JobLease, ...]: ...
    def inputs(self, lease: JobLease, now: datetime) -> MatchingInput | None: ...
    def complete(self, lease: JobLease, outcome: MatchResult, now: datetime) -> bool: ...
    def fail(
        self, lease: JobLease, error_code: str, now: datetime
    ) -> Literal["retry", "failed", "lost"]: ...


class SourceRepository(Protocol):
    def get(self, source_id: str) -> SourceRecord | None: ...
    def add(self, source: SourceRecord) -> None:
        """Insert only; raise SourceAlreadyExists if the ID exists."""
        ...


class TenderRepository(Protocol):
    def get(self, tender_id: UUID) -> Tender | None: ...
    def find(self, identity: TenderIdentity) -> Tender | None: ...
    def observe(
        self, observation: TenderObservation, rematch_fields: tuple[str, ...]
    ) -> TenderWriteResult:
        """Atomically persist observation/current state/linked diff. Caller owns transaction."""
        ...

    def history(self, tender_id: UUID) -> tuple[TenderVersion, ...]: ...
    def observations(self, tender_id: UUID) -> tuple[TenderObservation, ...]: ...
    def for_source(self, source_id: str) -> tuple[Tender, ...]: ...
    def scan(self, after: UUID | None, limit: int) -> tuple[Tender, ...]: ...
    def account_presence(
        self, source_id: str, seen: tuple[UUID, ...], sequence: int, full: bool
    ) -> None: ...


class CampaignRepository(Protocol):
    def atomic(self) -> AbstractContextManager[None]:
        """Rollback the entire campaign/backfill operation if its caller catches an error."""
        ...

    def get(self, campaign_id: UUID) -> Campaign | None: ...
    def active(self) -> tuple[Campaign, ...]: ...
    def criteria(self, version_id: UUID) -> CriteriaVersion: ...
    def store(self, campaign: Campaign, version: CriteriaVersion) -> None: ...
    def set_active(self, campaign_id: UUID, active: bool) -> Campaign: ...


class MatchJobRepository(Protocol):
    def enqueue(self, jobs: tuple[MatchJob, ...]) -> tuple[MatchJob, ...]:
        """Idempotent for tender/criteria/matcher version tuple."""
        ...

    def claim(self, now: datetime, lease_seconds: float) -> JobLease | None: ...
    def complete(self, lease: JobLease, result: StoredMatchResult) -> None:
        """Atomically append result and finish job; reject stale lease ownership."""
        ...

    def retry(self, lease: JobLease, available_at: datetime, error_code: str) -> None: ...


class SourceAlreadyExists(ValueError):
    """Portable uniqueness failure across storage adapters."""


class RepairEnvironment(Protocol):
    def executor(self, revision: RecipeRevision, settings: SourceSettings) -> SourceExecutor:
        """Return a fresh isolated executor, resolving only explicitly staged plugin artifacts."""
        ...

    def baseline(self, source_id: str) -> tuple[SourceSnapshot, tuple[Tender, ...]]: ...


class SampleVerifier(Protocol):
    def verify(
        self, run: CollectionRun, source: SourceRecord, settings: SourceSettings
    ) -> SampleVerification: ...


class RecipeCatalog(Protocol):
    def active(self, source_id: str) -> RecipeRevision | None: ...
    def revision(self, source_id: str, version: str) -> RecipeRevision | None: ...
