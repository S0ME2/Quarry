"""Health-gated orchestration; storage owns atomic publication and checkpoints."""

import hashlib
import json
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, Field

from tender_system.collection.health import ConservativeRunHealth as ConservativeRunHealth
from tender_system.collection.health import failure_health
from tender_system.collection.performance import timed
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    BoundedCollectionResult,
    CollectionRun,
    Contract,
    DataQualityIssue,
    ExecutionContext,
    ExecutionLimits,
    HealthDecision,
    NonEmpty,
    SourceFailure,
    SourceRecipe,
    SourceRecord,
    Tender,
)
from tender_system.core.domain import (
    SourceSnapshot as SourceSnapshot,
)
from tender_system.core.execution import (
    ExecutionDeadlineExceeded,
    effective_execution_limits,
    remaining_seconds,
)
from tender_system.core.ports import Clock, SourceExecutor, TenderNormalizer
from tender_system.core.tenders import identity_of


class RunReceipt(Contract):
    id: UUID
    source_id: NonEmpty
    recipe_version: NonEmpty
    status: Literal["accepted", "rejected", "failed"]
    request_hash: NonEmpty
    evidence_reference: NonEmpty
    reasons: tuple[str, ...] = ()
    accepted_sequence: int | None = None
    tender_ids: tuple[UUID, ...] = ()
    rematch_tender_ids: tuple[UUID, ...] = ()
    failure: SourceFailure | None = None
    health: HealthDecision | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    missing_incremented: int = Field(default=0, ge=0)
    checkpoint_advanced: bool = False
    purpose: Literal["collection", "fallback"] = "collection"


class RunStore(Protocol):
    def snapshot(self, source_id: str) -> SourceSnapshot: ...
    def receipt(self, run_id: UUID) -> RunReceipt | None: ...
    def publish(
        self,
        run: CollectionRun,
        recipe: SourceRecipe,
        settings: SourceSettings,
        base_sequence: int,
        request_hash: str,
    ) -> RunReceipt:
        """Recheck locked baseline; publish atomically. Caller owns commit."""
        ...

    def fail(self, failure: SourceFailure, request_hash: str) -> RunReceipt: ...


class RunHealthPolicy(Protocol):
    def evaluate(
        self,
        run: CollectionRun,
        settings: SourceSettings,
        snapshot: SourceSnapshot,
        previous: tuple[Tender, ...],
        *,
        now: datetime,
    ) -> HealthDecision: ...


def request_digest(
    recipe: SourceRecipe, settings: SourceSettings, limits: ExecutionLimits | None = None
) -> str:
    payload = json.dumps(
        [recipe.model_dump(mode="json"), settings.model_dump(mode="json")], sort_keys=True
    )
    if limits is not None:
        payload += limits.model_dump_json()
    return hashlib.sha256(payload.encode()).hexdigest()


class SourceRunService:
    def __init__(
        self,
        executor: SourceExecutor,
        normalizer: TenderNormalizer,
        store: RunStore,
        clock: Clock | None = None,
    ) -> None:
        self.executor = executor
        self.normalizer = normalizer
        self.store = store
        self.clock = clock if clock is not None else SystemClock()

    def run(
        self,
        run_id: UUID,
        recipe: SourceRecipe,
        source: SourceRecord,
        settings: SourceSettings,
        limits: ExecutionLimits | None = None,
        search_keywords: tuple[str, ...] = (),
    ) -> RunReceipt:
        if recipe.source_id != source.id:
            raise ValueError("recipe source differs from registered source")
        limits = effective_execution_limits(limits)
        search_keywords = tuple(dict.fromkeys(k.strip() for k in search_keywords if k.strip()))
        if recipe.search is None:
            search_keywords = ()
        digest = request_digest(recipe, settings, limits)
        if search_keywords:
            digest = hashlib.sha256((digest + json.dumps(search_keywords)).encode()).hexdigest()
        prior = self.store.receipt(run_id)
        if prior is not None:
            if prior.source_id != source.id or prior.request_hash != digest:
                raise ValueError("run ID reused with different source/recipe/configuration")
            return prior
        snapshot = self.store.snapshot(source.id)
        with timed("collection_execution", "executor wall"):
            result = self.executor.execute(
                recipe,
                ExecutionContext(
                    run_id=run_id,
                    values={
                        "checkpoint": snapshot.checkpoint,
                        "search_keywords": list(search_keywords),
                    },
                    limits=limits,
                ),
            )
        if isinstance(result, SourceFailure):
            if result.run_id != run_id or result.source_id != source.id or result.recipe != recipe:
                raise ValueError("executor changed failure run/source/recipe identity")
            result = result.model_copy(
                update={
                    "last_good_evidence_reference": snapshot.last_good_evidence_reference,
                    "authorized_session_reference": result.authorized_session_reference
                    or settings.authorized_session_reference,
                    "health": failure_health(result, settings, self.clock.now()),
                }
            )
            return self.store.fail(result, digest)
        if limits is not None and limits.max_tenders is not None:
            result = (
                result.sampled_run()
                if isinstance(result, BoundedCollectionResult)
                else result.model_copy(
                    update={"coverage": "sampled", "complete": False, "checkpoint": None}
                )
            )
        if search_keywords and not isinstance(result, BoundedCollectionResult):
            result = result.model_copy(
                update={"coverage": "sampled", "complete": False, "checkpoint": None}
            )
        retained_raw = []
        candidates = []
        seen: set[str] = set()
        issues: list[DataQualityIssue] = []
        for raw in result.raw_observations:
            try:
                remaining_seconds("normalization")
                with timed("normalization", "tender"):
                    normalized = self.normalizer.normalize(raw, source, settings)
            except Exception as error:
                failure = SourceFailure(
                    run_id=run_id,
                    source_id=source.id,
                    recipe=recipe,
                    error_code=getattr(error, "code", type(error).__name__),
                    error_detail="normalization deadline exceeded; see raw evidence"
                    if isinstance(error, ExecutionDeadlineExceeded)
                    else "normalizer adapter failed; see raw evidence",
                    failed_evidence_reference=result.evidence_reference,
                    failed_step="normalize",
                    bad_output=result.raw_observations,
                    authorized_session_reference=result.session_reference
                    or settings.authorized_session_reference,
                    last_good_evidence_reference=snapshot.last_good_evidence_reference,
                    diagnostics=result.diagnostics,
                    started_at=result.started_at,
                    finished_at=result.finished_at,
                )
                return self.store.fail(
                    failure.model_copy(
                        update={"health": failure_health(failure, settings, self.clock.now())}
                    ),
                    digest,
                )
            issues.extend(normalized.issues)
            if normalized.candidate is None:
                retained_raw.append(raw)
            if normalized.candidate is not None:
                identity = identity_of(normalized.candidate).model_dump_json()
                if not search_keywords or identity not in seen:
                    retained_raw.append(raw)
                    candidates.append(normalized.candidate)
                    seen.add(identity)
        result = result.model_copy(
            update={
                "candidates": tuple(candidates),
                "quality_issues": tuple(issues),
                "raw_observations": tuple(retained_raw),
            }
        )
        if (
            result.id != run_id
            or result.source_id != source.id
            or result.recipe_version != recipe.version
        ):
            raise ValueError("executor changed run/source/recipe identity")
        try:
            remaining_seconds("database_publication")
        except ExecutionDeadlineExceeded as error:
            error.evidence_references = (result.evidence_reference,)
            raise
        with timed("database_publication", "publication transaction"):
            receipt = self.store.publish(result, recipe, settings, snapshot.sequence, digest)
        return receipt
