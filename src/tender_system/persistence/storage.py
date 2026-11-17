"""PostgreSQL foundation. Transaction/session lifetime belongs to the caller."""

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4, uuid5

from pydantic import JsonValue, SecretStr, ValidationError
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Engine,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from tender_system.collection.health import block_decision
from tender_system.collection.performance import timed
from tender_system.collection.source_runs import (
    ConservativeRunHealth,
    RunHealthPolicy,
    RunReceipt,
    SourceSnapshot,
)
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import Configuration, SourceSettings
from tender_system.core.domain import (
    CollectionRun,
    HealthReason,
    SourceFailure,
    SourceRecipe,
    SourceRecord,
    Tender,
    TenderCandidate,
    TenderComparison,
    TenderIdentity,
    TenderObservation,
    TenderPresence,
    TenderVersion,
    TenderWriteResult,
)
from tender_system.core.execution import fence_execution, remaining_seconds
from tender_system.core.ports import Clock, SourceAlreadyExists, TenderComparator, TenderRepository
from tender_system.core.tenders import (
    MEANINGFUL_FIELDS,
    IdempotencyConflict,
    IdentityConflict,
    SemanticTenderComparator,
    StaleObservation,
    identity_of,
    reconstruct_history,
    state_of,
)
from tender_system.matching.semantic_matching import tender_fields


class Base(DeclarativeBase):
    pass


class OperatorConfigRow(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MatchingSpendRow(Base):
    __tablename__ = "matching_spend"
    __table_args__ = (
        CheckConstraint("reserved_usd >= 0 AND accounted_usd >= 0"),
        Index("ix_matching_spend_campaign", "campaign_id"),
    )
    job_id: Mapped[UUID] = mapped_column(ForeignKey("match_jobs.id"), primary_key=True)
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    accounted_usd: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(Text, nullable=False)


class OperationRow(Base):
    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'awaiting_approval', "
            "'activated', 'rejected', 'escalated')",
            name="ck_operation_status",
        ),
        Index("ix_operations_queue", "status", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)


class SourceRow(Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("length(trim(id)) > 0", name="ck_sources_id_nonblank"),
        CheckConstraint("length(trim(name)) > 0", name="ck_sources_name_nonblank"),
        UniqueConstraint("id", "has_stable_external_id", name="uq_sources_identity_policy"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    has_stable_external_id: Mapped[bool] = mapped_column(nullable=False)


class SourceRunRow(Base):
    __tablename__ = "source_runs"
    __table_args__ = (
        ForeignKeyConstraint(["source_id"], ["sources.id"], name="fk_source_runs_source"),
        UniqueConstraint("source_id", "accepted_sequence", name="uq_source_runs_sequence"),
        CheckConstraint(
            "status IN ('accepted', 'rejected', 'failed')", name="ck_source_runs_status"
        ),
        CheckConstraint(
            "(status = 'accepted' AND accepted_sequence IS NOT NULL AND accepted_sequence > 0) OR "
            "(status <> 'accepted' AND accepted_sequence IS NULL)",
            name="ck_source_runs_sequence",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_sequence: Mapped[int | None] = mapped_column(BigInteger)
    receipt: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)


class RecipeRevisionRow(Base):
    __tablename__ = "recipe_revisions"
    __table_args__ = (
        ForeignKeyConstraint(["source_id"], ["sources.id"], name="fk_recipe_revision_source"),
    )
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActiveRecipeRow(Base):
    __tablename__ = "active_recipes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "version"],
            ["recipe_revisions.source_id", "recipe_revisions.version"],
            name="fk_active_recipe_revision",
        ),
    )
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, nullable=False)


class SourceStateRow(Base):
    __tablename__ = "source_states"
    __table_args__ = (
        ForeignKeyConstraint(["source_id"], ["sources.id"], name="fk_source_states_source"),
        CheckConstraint("accepted_sequence >= 0", name="ck_source_states_sequence"),
        CheckConstraint("last_full_count >= 0", name="ck_source_states_count"),
    )
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    accepted_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checkpoint: Mapped[JsonValue] = mapped_column(JSONB, nullable=True)
    last_full_count: Mapped[int | None] = mapped_column(BigInteger)
    last_good_evidence_reference: Mapped[str | None] = mapped_column(Text)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_state: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB, nullable=True)


class TenderRow(Base):
    __tablename__ = "tenders"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "source_has_stable_external_id"],
            ["sources.id", "sources.has_stable_external_id"],
            name="fk_tenders_source_policy",
        ),
        ForeignKeyConstraint(
            ["id", "current_version_id"],
            ["tender_versions.tender_id", "tender_versions.id"],
            name="fk_tenders_current_version",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["id", "latest_observation_id"],
            ["tender_observations.tender_id", "tender_observations.id"],
            name="fk_tenders_latest_observation",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "(source_has_stable_external_id AND external_tender_id IS NOT NULL "
            "AND length(trim(external_tender_id)) > 0 "
            "AND fallback_kind IS NULL AND fallback_key IS NULL) OR "
            "(external_tender_id IS NULL AND NOT source_has_stable_external_id "
            "AND fallback_kind IS NOT NULL AND fallback_kind IN ('url', 'fingerprint') "
            "AND fallback_key IS NOT NULL "
            "AND length(trim(fallback_key)) > 0)",
            name="ck_tenders_identity",
        ),
        CheckConstraint(
            "value IS NULL OR (value >= 0 AND value != 'NaN'::numeric "
            "AND value != 'Infinity'::numeric)",
            name="ck_tenders_value",
        ),
        CheckConstraint("last_seen_at >= first_seen_at", name="ck_tenders_seen_order"),
        CheckConstraint("missing_successful_runs >= 0", name="ck_tenders_missing"),
        CheckConstraint(
            "last_accounted_run_sequence IS NULL OR last_accounted_run_sequence >= 0",
            name="ck_tenders_presence_sequence",
        ),
        Index("ix_tenders_recent", text("last_seen_at DESC"), "id"),
        Index("ix_tenders_source_recent", "source_id", text("last_seen_at DESC"), "id"),
        Index(
            "ix_tenders_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "public.gin_trgm_ops"},
        ),
        Index(
            "ix_tenders_description_trgm",
            "description",
            postgresql_using="gin",
            postgresql_ops={"description": "public.gin_trgm_ops"},
        ),
        Index(
            "ix_tenders_buyer_trgm",
            "buyer",
            postgresql_using="gin",
            postgresql_ops={"buyer": "public.gin_trgm_ops"},
        ),
        Index(
            "uq_tenders_external_identity",
            "source_id",
            "external_tender_id",
            unique=True,
            postgresql_where=text("external_tender_id IS NOT NULL"),
        ),
        Index(
            "uq_tenders_fallback_identity",
            "source_id",
            "fallback_kind",
            "fallback_key",
            unique=True,
            postgresql_where=text("external_tender_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(Text)
    external_tender_id: Mapped[str | None] = mapped_column(Text)
    source_has_stable_external_id: Mapped[bool]
    fallback_kind: Mapped[str | None] = mapped_column(Text)
    fallback_key: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    buyer: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value: Mapped[Decimal | None] = mapped_column(Numeric())
    currency: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    document_urls: Mapped[list[str]] = mapped_column(JSONB)
    product_items: Mapped[list[dict[str, JsonValue]] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    current_version_id: Mapped[UUID]
    latest_observation_id: Mapped[UUID]
    missing_successful_runs: Mapped[int] = mapped_column(default=0)
    last_accounted_run_sequence: Mapped[int | None] = mapped_column(BigInteger)


class TenderDocumentRow(Base):
    __tablename__ = "tender_documents"

    tender_id: Mapped[UUID] = mapped_column(ForeignKey("tenders.id"), primary_key=True)
    url: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenderVersionRow(Base):
    __tablename__ = "tender_versions"
    __table_args__ = (
        ForeignKeyConstraint(["tender_id"], ["tenders.id"], name="fk_versions_tender"),
        UniqueConstraint("tender_id", "id", name="uq_versions_owner"),
        ForeignKeyConstraint(
            ["tender_id", "previous_version_id"],
            ["tender_versions.tender_id", "tender_versions.id"],
            name="fk_versions_previous_owner",
        ),
        ForeignKeyConstraint(
            ["tender_id", "observation_id"],
            ["tender_observations.tender_id", "tender_observations.id"],
            name="fk_versions_observation",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        UniqueConstraint("previous_version_id", name="uq_versions_no_fork"),
        UniqueConstraint("observation_id", name="uq_versions_observation"),
        CheckConstraint(
            "previous_version_id IS NULL OR previous_version_id != id",
            name="ck_versions_no_self_link",
        ),
        CheckConstraint(
            "jsonb_typeof(changed_fields) = 'object' AND changed_fields != '{}'::jsonb",
            name="ck_versions_nonempty_diff",
        ),
        Index(
            "uq_versions_initial",
            "tender_id",
            unique=True,
            postgresql_where=text("previous_version_id IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tender_id: Mapped[UUID]
    previous_version_id: Mapped[UUID | None]
    changed_fields: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence_reference: Mapped[str] = mapped_column(Text)
    observation_id: Mapped[UUID]


class TenderObservationRow(Base):
    __tablename__ = "tender_observations"
    __table_args__ = (
        UniqueConstraint("tender_id", "id", name="uq_observations_owner"),
        ForeignKeyConstraint(
            ["tender_id", "version_id"],
            ["tender_versions.tender_id", "tender_versions.id"],
            name="fk_observations_version_owner",
        ),
        Index("ix_observations_tender", "tender_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tender_id: Mapped[UUID]
    version_id: Mapped[UUID]
    payload_hash: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence_reference: Mapped[str] = mapped_column(Text)
    source_run_id: Mapped[UUID | None]
    raw_data: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    quality_issues: Mapped[list[dict[str, JsonValue]]] = mapped_column(JSONB)
    normalization_overrides: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)


def database_url_from_environment(
    variable: str = "DATABASE_URL", environ: Mapping[str, str] | None = None
) -> SecretStr:
    from tender_system.core.environment import load_environment

    environment = load_environment() if environ is None else environ
    if environ is None and variable == "DATABASE_URL" and os.environ.get("PYTEST_CURRENT_TEST"):
        from sqlalchemy.engine import make_url

        configured = environment.get("DATABASE_URL")
        test_url = environment.get("TEST_DATABASE_URL")
        if (
            not configured
            or not test_url
            or make_url(configured).database != make_url(test_url).database
        ):
            variable = "TEST_DATABASE_URL"
        else:
            validate_test_database(configured)

    value = environment.get(variable)
    if not value:
        raise ValueError(f"missing database URL environment variable: {variable}")
    if variable == "TEST_DATABASE_URL":
        validate_test_database(value, environment.get("DATABASE_URL"))
    return SecretStr(value)


def validate_test_database(value: str, development: str | None = None) -> None:
    from sqlalchemy.engine import make_url

    try:
        test = make_url(value)
        dev = make_url(development) if development else None
    except Exception:
        raise ValueError("Invalid TEST_DATABASE_URL (value redacted)") from None
    if (
        test.get_backend_name() != "postgresql"
        or not test.database
        or "test" not in test.database.lower()
    ):
        raise ValueError("TEST_DATABASE_URL must name a dedicated PostgreSQL test database")
    if dev and test.database == dev.database:
        raise ValueError("TEST_DATABASE_URL must differ from development database")


def build_engine(database_url: SecretStr) -> Engine:
    engine = create_engine(
        database_url.get_secret_value(),
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
    if engine.dialect.name != "postgresql":
        engine.dispose()
        raise ValueError("this adapter requires PostgreSQL")
    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


class SqlSourceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, source_id: str) -> SourceRecord | None:
        row = self._session.get(SourceRow, source_id)
        if row is None:
            return None
        return SourceRecord(
            id=row.id, name=row.name, has_stable_external_id=row.has_stable_external_id
        )

    def add(self, source: SourceRecord) -> None:
        try:
            # A savepoint keeps duplicate insertion from poisoning the outer transaction.
            with self._session.begin_nested():
                self._session.add(SourceRow(**source.model_dump()))
                self._session.flush()
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) == "23505":
                raise SourceAlreadyExists(source.id) from None
            raise


def observation_hash(observation: TenderObservation) -> str:
    payload = {
        "state": state_of(observation.candidate),
        "raw_data": observation.candidate.raw_data,
        "quality_issues": [
            issue.model_dump(mode="json") for issue in observation.candidate.quality_issues
        ],
        "observed_at": observation.observed_at.astimezone(UTC).isoformat(),
        "evidence_reference": observation.evidence_reference,
        "source_run_id": str(observation.source_run_id) if observation.source_run_id else None,
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


class SqlRunStore:
    """Caller owns commit. Network execution never happens while holding source locks."""

    def __init__(
        self,
        session: Session,
        tenders: TenderRepository | None = None,
        health: RunHealthPolicy | None = None,
        clock: Clock | None = None,
        configuration: Configuration | None = None,
    ) -> None:
        self._session = session
        self._tenders = tenders
        self._health = health if health is not None else ConservativeRunHealth()
        self._clock = clock if clock is not None else SystemClock()
        self._configuration = configuration or Configuration()

    def snapshot(self, source_id: str) -> SourceSnapshot:
        row = self._session.get(SourceStateRow, source_id, populate_existing=True)
        return (
            SourceSnapshot()
            if row is None
            else SourceSnapshot.model_validate(
                {
                    "sequence": row.accepted_sequence,
                    "checkpoint": row.checkpoint,
                    "last_full_count": row.last_full_count,
                    "last_good_evidence_reference": row.last_good_evidence_reference,
                    **(row.evidence_state or {}),
                }
            )
        )

    def receipt(self, run_id: UUID) -> RunReceipt | None:
        row = self._session.get(SourceRunRow, run_id, populate_existing=True)
        return RunReceipt.model_validate(row.receipt) if row is not None else None

    def _lock(self, source_id: str, run_id: UUID) -> None:
        source = self._session.scalar(
            select(SourceRow).where(SourceRow.id == source_id).with_for_update()
        )
        if source is None:
            raise IdentityConflict("source must be registered before collection")
        self._session.execute(
            select(func.pg_advisory_xact_lock(int.from_bytes(run_id.bytes[:8], signed=True)))
        )

    def _prior(self, run_id: UUID, source_id: str, request_hash: str) -> RunReceipt | None:
        prior = self.receipt(run_id)
        if prior is not None and (
            prior.source_id != source_id or prior.request_hash != request_hash
        ):
            raise IdempotencyConflict("run ID reused with different input")
        return prior

    def _save(self, receipt: RunReceipt) -> RunReceipt:
        self._session.add(
            SourceRunRow(
                id=receipt.id,
                source_id=receipt.source_id,
                status=receipt.status,
                accepted_sequence=receipt.accepted_sequence,
                receipt=receipt.model_dump(mode="json"),
            )
        )
        self._session.flush()
        return receipt

    def fail(self, failure: SourceFailure, request_hash: str) -> RunReceipt:
        with self._session.begin_nested():
            self._lock(failure.source_id, failure.run_id)
            prior = self._prior(failure.run_id, failure.source_id, request_hash)
            if prior is not None:
                return prior
            return self._save(
                RunReceipt(
                    id=failure.run_id,
                    source_id=failure.source_id,
                    recipe_version=failure.recipe.version,
                    status="failed",
                    request_hash=request_hash,
                    evidence_reference=failure.failed_evidence_reference,
                    reasons=(failure.error_code,),
                    failure=failure,
                    health=failure.health,
                    started_at=failure.started_at,
                    finished_at=failure.finished_at,
                )
            )

    def publish(
        self,
        run: CollectionRun,
        recipe: SourceRecipe,
        settings: SourceSettings,
        base_sequence: int,
        request_hash: str,
    ) -> RunReceipt:
        with self._session.begin_nested():
            remaining = remaining_seconds("database_publication")
            if remaining is not None:
                self._session.execute(
                    text("select set_config('statement_timeout', :timeout, true)"),
                    {"timeout": str(max(1, int(remaining * 1000)))},
                )
            self._lock(run.source_id, run.id)
            fence_execution()
            prior = self._prior(run.id, run.source_id, request_hash)
            if prior is not None:
                return prior
            snapshot = self.snapshot(run.source_id)
            tenders = (
                self._tenders
                if self._tenders is not None
                else SqlTenderRepository(
                    self._session,
                    SemanticTenderComparator(settings.normalization, settings.field_change_policy),
                )
            )
            with timed("health_validation", "source health evaluation"):
                decision = self._health.evaluate(
                    run,
                    settings,
                    snapshot,
                    tenders.for_source(run.source_id),
                    now=self._clock.now(),
                )
            active_recipe = self._session.get(ActiveRecipeRow, run.source_id)
            if active_recipe is not None and run.purpose != "fallback":
                revision = self._session.get(
                    RecipeRevisionRow, (run.source_id, active_recipe.version)
                )
                stored_recipe = None
                if revision is not None:
                    try:
                        stored_recipe = SourceRecipe.model_validate(
                            revision.revision.get("recipe")
                        ).model_dump(mode="json")
                    except (ValidationError, TypeError, ValueError):
                        pass
                if (
                    active_recipe.version != recipe.version
                    or revision is None
                    or stored_recipe != recipe.model_dump(mode="json")
                ):
                    decision = block_decision(
                        decision,
                        "recipe_superseded",
                        "Recipe differs from activated catalog revision.",
                    )
            row = self._session.get(SourceStateRow, run.source_id, populate_existing=True)
            if base_sequence != snapshot.sequence:
                decision = block_decision(
                    decision, "stale_baseline", "Accepted baseline advanced during collection."
                )
            if (
                row
                and row.last_finished_at
                and run.started_at.astimezone(UTC) < row.last_finished_at.astimezone(UTC)
            ):
                decision = block_decision(
                    decision,
                    "overlapping_or_stale_run",
                    "Run overlaps a previously accepted inventory.",
                )
            if recipe.source_id != run.source_id or recipe.version != run.recipe_version:
                decision = block_decision(
                    decision, "wrong_recipe", "Recipe identity differs from collection run."
                )
            reasons = tuple(dict.fromkeys(r.code for r in decision.reasons if r.severity != "info"))
            if not decision.publication_allowed:
                failure = SourceFailure(
                    run_id=run.id,
                    source_id=run.source_id,
                    recipe=recipe,
                    error_code="run_health_rejected",
                    error_detail=", ".join(reasons),
                    failed_evidence_reference=run.evidence_reference,
                    last_good_evidence_reference=snapshot.last_good_evidence_reference,
                    issues=run.quality_issues,
                    bad_output=run.raw_observations,
                    authorized_session_reference=run.session_reference
                    or settings.authorized_session_reference,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    health=decision,
                    diagnostics=run.diagnostics,
                )
                return self._save(
                    RunReceipt(
                        id=run.id,
                        source_id=run.source_id,
                        recipe_version=run.recipe_version,
                        status="rejected",
                        request_hash=request_hash,
                        evidence_reference=run.evidence_reference,
                        reasons=reasons,
                        failure=failure,
                        health=decision,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                    )
                )
            sequence = snapshot.sequence + 1
            seen: list[UUID] = []
            rematch: list[UUID] = []
            unique_candidates = {
                identity_of(candidate).model_dump_json(): candidate for candidate in run.candidates
            }
            for candidate in unique_candidates.values():
                previous = tenders.find(identity_of(candidate))
                identity = identity_of(candidate).model_dump_json()
                observation = TenderObservation(
                    id=uuid5(run.id, identity),
                    candidate=candidate,
                    observed_at=run.finished_at,
                    evidence_reference=run.evidence_reference,
                    source_run_id=run.id,
                )
                with timed("database_persistence", "tender observation write"):
                    result = tenders.observe(observation, settings.effective_rematch_fields)
                seen.append(result.tender.internal_id)
                if (
                    bool(candidate.document_urls)
                    or result.comparison.classification != "same"
                    or previous is not None
                    and (
                        previous.presence.missing_successful_runs > 0
                        or previous.quality_issues != result.tender.quality_issues
                        or tender_fields(previous) != tender_fields(result.tender)
                    )
                ):
                    rematch.append(result.tender.internal_id)
            # Replaceable policies cannot bypass the raw execution/coverage fence.
            absence = (
                decision.absence_evidence_allowed
                and run.purpose != "fallback"
                and run.complete
                and run.coverage == "full"
                and decision.completeness == "complete"
                and decision.overall_status == "healthy"
                and decision.freshness.status == "fresh"
                and not decision.malformed_count
                and not decision.duplicate_count
                and run.diagnostics.completion is not None
                and run.diagnostics.completion.observed
                and run.diagnostics.completion.kind in settings.calibration.completion_signals
                and settings.calibration.full_inventory_supported
                and len(run.raw_observations) == len(run.candidates)
                and len(unique_candidates) == len(run.candidates)
            )
            if decision.absence_evidence_allowed != absence:
                decision = decision.model_copy(
                    update={
                        "absence_evidence_allowed": absence,
                        "reasons": (
                            *decision.reasons,
                            HealthReason(
                                code="absence_storage_fence",
                                dimension="inventory",
                                severity="info",
                                detail=(
                                    "Raw coverage, completion or identity "
                                    "fence denied absence evidence."
                                ),
                                evidence_reference=run.evidence_reference,
                            ),
                        ),
                    }
                )
            remaining_seconds("database_publication")
            seen_ids = set(seen)
            missing_incremented = (
                sum(t.internal_id not in seen_ids for t in tenders.for_source(run.source_id))
                if absence
                else 0
            )
            tenders.account_presence(run.source_id, tuple(seen), sequence, absence)
            if seen:
                # Durable outbox shares publication, never invokes the matcher here.
                # Snapshot campaign identities: later creation cannot backfill this run.
                collection = self._session.get(OperationRow, run.id)
                account_targets = cast(
                    list[str],
                    collection.payload.get("account_campaign_ids", []) if collection else [],
                )
                campaigns = tuple(
                    campaign_id
                    for campaign_id, criteria in self._session.execute(
                        select(CampaignRow.id, CriteriaRow.criteria)
                        .join(
                            CriteriaRow, CriteriaRow.id == CampaignRow.current_criteria_version_id
                        )
                        .where(
                            CampaignRow.active,
                            CampaignRow.deleted_at.is_(None),
                            CriteriaRow.criteria["automatic_matching"].as_boolean().is_(True),
                        )
                    )
                    if account_campaign_allowed(self._session, campaign_id, run.source_id)
                    and (
                        self._session.get(AccountCampaignRow, campaign_id) is None
                        or str(campaign_id) in account_targets
                    )
                    and (
                        not (source_ids := criteria.get("source_ids"))
                        or (isinstance(source_ids, list) and run.source_id in source_ids)
                    )
                )
                # No matching work exists without an applicable campaign. Empty
                # children otherwise fence collection after a post-commit crash.
                for offset in range(0, len(seen) if campaigns else 0, 50):
                    self._session.add(
                        OperationRow(
                            id=uuid5(run.id, f"automatic-matching/{offset}"),
                            kind="automatic_matching",
                            source_id=run.source_id,
                            status="queued",
                            payload={
                                "source_run_id": str(run.id),
                                "tender_ids": [str(k) for k in seen[offset : offset + 50]],
                                "campaign_ids": [str(k) for k in campaigns],
                                "document_urls": {
                                    str(k): list(tender.document_urls)
                                    for k in seen[offset : offset + 50]
                                    if (tender := tenders.get(k)) is not None
                                },
                            },
                            result={},
                            created_at=self._clock.now(),
                        )
                    )
            if row is None:
                row = SourceStateRow(source_id=run.source_id, accepted_sequence=0)
                self._session.add(row)
            row.accepted_sequence = sequence
            checkpoint_advanced = (
                decision.overall_status == "healthy"
                and decision.completeness == "complete"
                and run.purpose != "fallback"
                and run.complete
                and run.coverage in ("full", "incremental")
            )
            row.checkpoint = run.checkpoint if checkpoint_advanced else snapshot.checkpoint
            row.last_good_evidence_reference = run.evidence_reference
            row.last_finished_at = run.finished_at
            anchors = snapshot.model_dump(
                mode="json",
                exclude={
                    "sequence",
                    "checkpoint",
                    "last_full_count",
                    "last_good_evidence_reference",
                },
            )
            if run.coverage == "incremental":
                anchors["last_incremental_at"] = run.finished_at.isoformat()
            if (
                checkpoint_advanced
                and decision.freshness.basis == "inventory_timestamp"
                and decision.freshness.timestamp is not None
            ):
                anchors["last_source_timestamp"] = decision.freshness.timestamp.isoformat()
            if absence:
                row.last_full_count = len(seen)
                anchors.update(
                    last_full_verified_at=run.finished_at.isoformat(),
                    last_full_freshness_at=decision.freshness.timestamp.isoformat()
                    if decision.freshness.timestamp
                    else None,
                    last_full_evidence_reference=run.evidence_reference,
                    last_full_policy=decision.policy,
                    last_full_freshness_basis=decision.freshness.basis,
                    accepted_full_runs=snapshot.accepted_full_runs + 1,
                )
            row.evidence_state = anchors
            return self._save(
                RunReceipt(
                    id=run.id,
                    source_id=run.source_id,
                    recipe_version=run.recipe_version,
                    status="accepted",
                    request_hash=request_hash,
                    evidence_reference=run.evidence_reference,
                    accepted_sequence=sequence,
                    tender_ids=tuple(seen),
                    rematch_tender_ids=tuple(rematch),
                    reasons=reasons,
                    health=decision,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    missing_incremented=missing_incremented,
                    checkpoint_advanced=checkpoint_advanced,
                    purpose=run.purpose,
                )
            )


class DatabasePublicationError(RuntimeError):
    """Connection loss at publication; a committed receipt determines persisted effects."""

    code = "database_unavailable"
    stage = "database_publication"
    publication_persisted: bool | None = None
    safe_recovery_action = (
        "Inspect the operation's source-run receipt before retrying; "
        "an accepted receipt means publication already committed."
    )

    def __init__(self, run: CollectionRun) -> None:
        super().__init__("Database connection failed during source-run publication")
        self.evidence_references = (run.evidence_reference,)


class TransactionalRunStore:
    """Production facade: each read/write owns a short session, outside recipe execution."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Clock | None = None,
        configuration: Configuration | None = None,
        document_processor: object | None = None,
    ) -> None:
        self.sessions = sessions
        self.clock = clock
        self.configuration = configuration
        self.document_processing_failed = False
        self.document_outcomes: dict[str, int] | None = None
        if document_processor is None:
            from tender_system.collection.documents import DocumentProcessor

            document_processor = DocumentProcessor(sessions)
        self.document_processor = document_processor

    def snapshot(self, source_id: str) -> SourceSnapshot:
        with self.sessions() as session:
            return SqlRunStore(session).snapshot(source_id)

    def receipt(self, run_id: UUID) -> RunReceipt | None:
        with self.sessions() as session:
            return SqlRunStore(session).receipt(run_id)

    def publish(
        self,
        run: CollectionRun,
        recipe: SourceRecipe,
        settings: SourceSettings,
        base_sequence: int,
        request_hash: str,
    ) -> RunReceipt:
        self.document_processing_failed = False
        self.document_outcomes = None
        try:
            with self.sessions.begin() as session:
                receipt = SqlRunStore(
                    session, clock=self.clock, configuration=self.configuration
                ).publish(run, recipe, settings, base_sequence, request_hash)
        except OperationalError as error:
            raise DatabasePublicationError(run) from error
        if receipt.status == "accepted":
            try:
                self.document_outcomes = self.document_processor.process(  # type: ignore[attr-defined]
                    receipt.tender_ids,
                    run.diagnostics.transport_attempts,
                    run.diagnostics.response_bytes,
                    run_id=run.id,
                )
            except Exception:
                self.document_processing_failed = True
                logging.getLogger(__name__).exception(
                    "Document processing interrupted after publication"
                )
        return receipt

    def fail(self, failure: SourceFailure, request_hash: str) -> RunReceipt:
        with self.sessions.begin() as session:
            return SqlRunStore(session).fail(failure, request_hash)


class SqlTenderRepository:
    """Atomic observation writes; no job creation or accepted-source-run accounting."""

    def __init__(self, session: Session, comparator: TenderComparator | None = None) -> None:
        self._session = session
        self._comparator = comparator if comparator is not None else SemanticTenderComparator()

    def _tender(self, row: TenderRow, observation: TenderObservationRow | None = None) -> Tender:
        if observation is None:
            observation = self._session.get(TenderObservationRow, row.latest_observation_id)
        if observation is None:
            raise ValueError("tender is missing its provenance observation")
        values = {
            field: getattr(row, field)
            for field in TenderCandidate.model_fields
            if field not in {"raw_data", "quality_issues"}
        }
        documents = sorted(self.documents(row.id), key=lambda d: d.url)
        return Tender.model_validate(
            {
                **values,
                "internal_id": row.id,
                "current_version_id": row.current_version_id,
                "first_seen_at": row.first_seen_at.astimezone(UTC),
                "last_seen_at": row.last_seen_at.astimezone(UTC),
                "latest_observation_id": row.latest_observation_id,
                "presence": TenderPresence(
                    missing_successful_runs=row.missing_successful_runs,
                    last_accounted_run_sequence=row.last_accounted_run_sequence,
                ),
                "raw_data": observation.raw_data,
                "quality_issues": observation.quality_issues,
                "document_text": "\n\n".join(
                    doc.extracted_text
                    for doc in documents
                    if doc.status == "success" and doc.extracted_text
                ),
                "documents_incomplete": bool(
                    set(row.document_urls)
                    - {
                        doc.url
                        for doc in documents
                        if doc.status == "success" and (doc.extracted_text or "").strip()
                    }
                )
                or any(
                    issue.get("field") == "document_urls" for issue in observation.quality_issues
                ),
            }
        )

    def get(self, tender_id: UUID) -> Tender | None:
        row = self._session.get(TenderRow, tender_id, populate_existing=True)
        return self._tender(row) if row is not None else None

    def documents(self, tender_id: UUID) -> tuple[TenderDocumentRow, ...]:
        """Return current attachment results; removed URLs remain stored for history."""
        tender = self._session.get(TenderRow, tender_id)
        if tender is None or not tender.document_urls:
            return ()
        rows = self._session.scalars(
            select(TenderDocumentRow).where(
                TenderDocumentRow.tender_id == tender_id,
                TenderDocumentRow.url.in_(tender.document_urls),
            )
        ).all()
        return tuple(rows)

    def get_many(self, keys: tuple[UUID, ...]) -> tuple[Tender, ...]:
        if not keys:
            return ()
        rows = self._session.execute(
            select(TenderRow, TenderObservationRow)
            .join(TenderObservationRow, TenderObservationRow.id == TenderRow.latest_observation_id)
            .where(TenderRow.id.in_(keys))
            .execution_options(populate_existing=True)
        )
        tenders = {row.id: self._tender(row, observation) for row, observation in rows}
        return tuple(tenders[key] for key in keys if key in tenders)

    def _find_row(self, identity: TenderIdentity) -> TenderRow | None:
        statement = select(TenderRow).where(TenderRow.source_id == identity.source_id)
        if identity.external_tender_id is not None:
            statement = statement.where(TenderRow.external_tender_id == identity.external_tender_id)
        else:
            statement = statement.where(
                TenderRow.external_tender_id.is_(None),
                TenderRow.fallback_kind == identity.fallback_kind,
                TenderRow.fallback_key == identity.fallback_key,
            )
        return self._session.execute(
            statement.execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def find(self, identity: TenderIdentity) -> Tender | None:
        row = self._find_row(identity)
        return self._tender(row) if row is not None else None

    def for_source(self, source_id: str) -> tuple[Tender, ...]:
        rows = self._session.execute(
            select(TenderRow, TenderObservationRow)
            .outerjoin(
                TenderObservationRow, TenderObservationRow.id == TenderRow.latest_observation_id
            )
            .where(TenderRow.source_id == source_id)
            .order_by(TenderRow.id)
            .execution_options(populate_existing=True)
        )
        return tuple(self._tender(row, observation) for row, observation in rows)

    def scan(
        self, after: UUID | None, limit: int, source_id: str | None = None
    ) -> tuple[Tender, ...]:
        if limit < 1:
            raise ValueError("scan limit must be positive")
        query = (
            select(TenderRow, TenderObservationRow)
            .outerjoin(
                TenderObservationRow, TenderObservationRow.id == TenderRow.latest_observation_id
            )
            .order_by(TenderRow.id)
            .limit(limit)
        )
        if source_id is not None:
            query = query.where(TenderRow.source_id == source_id)
        if after is not None:
            query = query.where(TenderRow.id > after)
        return tuple(
            self._tender(row, observation) for row, observation in self._session.execute(query)
        )

    def account_presence(
        self, source_id: str, seen: tuple[UUID, ...], sequence: int, full: bool
    ) -> None:
        """Only called after source-run acceptance under the source lock, same transaction."""
        seen_ids = set(seen)
        rows = self._session.scalars(select(TenderRow).where(TenderRow.source_id == source_id))
        for row in rows:
            if (
                row.last_accounted_run_sequence is not None
                and row.last_accounted_run_sequence >= sequence
            ):
                raise StaleObservation("presence sequence did not advance")
            if row.id in seen_ids:
                row.missing_successful_runs = 0
                row.last_accounted_run_sequence = sequence
            elif full:
                row.missing_successful_runs += 1
                row.last_accounted_run_sequence = sequence
        self._session.flush()

    def observe(
        self, observation: TenderObservation, rematch_fields: tuple[str, ...]
    ) -> TenderWriteResult:
        with self._session.begin_nested():
            return self._observe(observation, rematch_fields)

    def _observe(
        self, observation: TenderObservation, rematch_fields: tuple[str, ...]
    ) -> TenderWriteResult:
        candidate = observation.candidate
        fingerprint = observation_hash(observation)
        # Lock source before observation IDs: overlapping source batches must not deadlock.
        source = self._session.execute(
            select(SourceRow)
            .where(SourceRow.id == candidate.source_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if source is None:
            raise IdentityConflict("source must be registered before tender persistence")
        if source.has_stable_external_id != candidate.source_has_stable_external_id:
            raise IdentityConflict("candidate identity policy differs from registered source")
        lock_key = int.from_bytes(observation.id.bytes[:8], signed=True)
        self._session.execute(select(func.pg_advisory_xact_lock(lock_key)))
        existing = self._session.get(TenderObservationRow, observation.id, populate_existing=True)
        if existing is not None:
            if existing.payload_hash != fingerprint:
                raise IdempotencyConflict("observation ID was reused with different input/evidence")
            tender = self.get(existing.tender_id)
            assert tender is not None
            return TenderWriteResult(
                tender=tender,
                comparison=TenderComparison(
                    classification="same", changed_fields={}, requires_rematch=False
                ),
                replayed=True,
            )
        identity = identity_of(candidate)
        row = self._find_row(identity)
        stored = self._tender(row) if row is not None else None
        if stored is not None and observation.observed_at.astimezone(UTC) < stored.last_seen_at:
            raise StaleObservation("older observation cannot overwrite current tender state")
        with timed("version_comparison", "tender version comparison"):
            comparison = self._comparator.compare(stored, candidate, rematch_fields)
        candidate_state = state_of(candidate)
        if stored is None:
            if comparison.classification != "new" or comparison.changed_fields != candidate_state:
                raise ValueError("comparator must return full initial state for a new tender")
        else:
            if (
                comparison.classification == "new"
                or bool(comparison.changed_fields) != (comparison.classification == "changed")
                or set(comparison.changed_fields) - set(MEANINGFUL_FIELDS)
                or any(
                    candidate_state[name] != value
                    for name, value in comparison.changed_fields.items()
                )
            ):
                raise ValueError("comparator returned inconsistent tender diff")
        if row is None:
            row = TenderRow(
                id=uuid4(),
                **candidate.model_dump(exclude={"raw_data", "quality_issues"}),
                current_version_id=uuid4(),
                latest_observation_id=observation.id,
                first_seen_at=observation.observed_at,
                last_seen_at=observation.observed_at,
                missing_successful_runs=0,
            )
            row.document_urls = list(candidate.document_urls)
            self._session.add(row)
            self._session.flush()
            previous = None
        else:
            previous = row.current_version_id
            if comparison.classification == "changed":
                row.current_version_id = uuid4()
                for name in comparison.changed_fields:
                    value = getattr(candidate, name)
                    setattr(
                        row,
                        name,
                        list(value)
                        if name in {"document_urls", "product_items"} and value is not None
                        else value,
                    )
            row.last_seen_at = observation.observed_at
            row.latest_observation_id = observation.id
            # Accounting of trusted absent runs is Phase 3; an observed reappearance resets absence.
            row.missing_successful_runs = 0
        if comparison.classification != "same":
            self._session.add(
                TenderVersionRow(
                    id=row.current_version_id,
                    tender_id=row.id,
                    previous_version_id=previous,
                    changed_fields=comparison.changed_fields,
                    observed_at=observation.observed_at,
                    evidence_reference=observation.evidence_reference,
                    observation_id=observation.id,
                )
            )
            self._session.flush()
        accepted_state = {
            name: getattr(row, name)
            for name in TenderCandidate.model_fields
            if name not in {"raw_data", "quality_issues"}
        }
        state = state_of(TenderCandidate.model_validate(accepted_state))
        overrides = {
            name: value for name, value in state_of(candidate).items() if state[name] != value
        }
        self._session.add(
            TenderObservationRow(
                id=observation.id,
                tender_id=row.id,
                version_id=row.current_version_id,
                payload_hash=fingerprint,
                observed_at=observation.observed_at,
                evidence_reference=observation.evidence_reference,
                source_run_id=observation.source_run_id,
                raw_data=candidate.raw_data,
                quality_issues=[
                    issue.model_dump(mode="json") for issue in candidate.quality_issues
                ],
                normalization_overrides=overrides,
            )
        )
        self._session.flush()
        return TenderWriteResult(tender=self._tender(row), comparison=comparison)

    def history(self, tender_id: UUID) -> tuple[TenderVersion, ...]:
        rows = self._session.scalars(
            select(TenderVersionRow).where(TenderVersionRow.tender_id == tender_id)
        )
        versions = tuple(
            TenderVersion(
                id=row.id,
                tender_id=row.tender_id,
                previous_version_id=row.previous_version_id,
                changed_fields=row.changed_fields,
                observed_at=row.observed_at,
                evidence_reference=row.evidence_reference,
                observation_id=row.observation_id,
            )
            for row in rows
        )
        if not versions:
            return ()
        reconstruct_history(versions)
        by_previous = {version.previous_version_id: version for version in versions}
        ordered: list[TenderVersion] = []
        current = by_previous.get(None)
        while current is not None:
            ordered.append(current)
            current = by_previous.get(current.id)
        return tuple(ordered)

    def observations(self, tender_id: UUID) -> tuple[TenderObservation, ...]:
        versions = self.history(tender_id)
        rows = self._session.scalars(
            select(TenderObservationRow)
            .where(TenderObservationRow.tender_id == tender_id)
            .order_by(TenderObservationRow.observed_at, TenderObservationRow.id)
        )
        results: list[TenderObservation] = []
        for row in rows:
            state = reconstruct_history(versions, row.version_id)
            state.update(row.normalization_overrides)
            candidate = TenderCandidate.model_validate(
                {**state, "raw_data": row.raw_data, "quality_issues": row.quality_issues}
            )
            results.append(
                TenderObservation(
                    id=row.id,
                    candidate=candidate,
                    observed_at=row.observed_at,
                    evidence_reference=row.evidence_reference,
                    source_run_id=row.source_run_id,
                )
            )
        return tuple(results)


class CampaignRow(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint("deleted_at IS NULL OR NOT active", name="ck_campaign_deleted_inactive"),
        ForeignKeyConstraint(
            ["id", "current_criteria_version_id"],
            ["campaign_criteria.campaign_id", "campaign_criteria.id"],
            name="fk_campaign_current_criteria",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    active: Mapped[bool]
    current_criteria_version_id: Mapped[UUID]
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CriteriaRow(Base):
    __tablename__ = "campaign_criteria"
    __table_args__ = (
        ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name="fk_criteria_campaign"),
        UniqueConstraint("campaign_id", "id", name="uq_criteria_owner"),
        UniqueConstraint("campaign_id", "number", name="uq_criteria_number"),
        UniqueConstraint("previous_version_id", name="uq_criteria_no_fork"),
        ForeignKeyConstraint(
            ["campaign_id", "previous_version_id"],
            ["campaign_criteria.campaign_id", "campaign_criteria.id"],
            name="fk_criteria_previous_owner",
        ),
        CheckConstraint("number > 0", name="ck_criteria_number"),
        CheckConstraint(
            "previous_version_id IS NULL OR previous_version_id != id", name="ck_criteria_no_self"
        ),
        Index(
            "uq_criteria_initial",
            "campaign_id",
            unique=True,
            postgresql_where=text("previous_version_id IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    campaign_id: Mapped[UUID]
    previous_version_id: Mapped[UUID | None]
    number: Mapped[int]
    criteria: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


JOB_IDENTITY = (
    "tender_id",
    "tender_version_id",
    "campaign_id",
    "criteria_version_id",
    "matcher_implementation",
    "matcher_version",
    "settings_hash",
)


class MatchJobRow(Base):
    __tablename__ = "match_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tender_id", "tender_version_id"],
            ["tender_versions.tender_id", "tender_versions.id"],
            name="fk_job_tender_version",
        ),
        ForeignKeyConstraint(
            ["campaign_id", "criteria_version_id"],
            ["campaign_criteria.campaign_id", "campaign_criteria.id"],
            name="fk_job_criteria_owner",
        ),
        UniqueConstraint(*JOB_IDENTITY, name="uq_match_job_work"),
        UniqueConstraint("id", *JOB_IDENTITY, name="uq_match_job_result_owner"),
        CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')", name="ck_match_job_status"
        ),
        CheckConstraint("attempts >= 0 AND max_attempts > 0", name="ck_match_job_attempts"),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_match_job_lease",
        ),
        Index(
            "ix_match_jobs_pending",
            "available_at",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_match_jobs_expired", "lease_expires_at", postgresql_where=text("status = 'running'")
        ),
        Index("ix_match_jobs_campaign", "campaign_id", "criteria_version_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tender_id: Mapped[UUID]
    tender_version_id: Mapped[UUID]
    campaign_id: Mapped[UUID]
    criteria_version_id: Mapped[UUID]
    matcher_implementation: Mapped[str] = mapped_column(Text)
    matcher_version: Mapped[str] = mapped_column(Text)
    matcher_config: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    settings_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int]
    max_attempts: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatchSelectionRow(Base):
    __tablename__ = "match_selections"
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id"), primary_key=True)
    tender_id: Mapped[UUID] = mapped_column(ForeignKey("tenders.id"), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("match_jobs.id"), nullable=False)
    tender_version_id: Mapped[UUID] = mapped_column(nullable=False)
    criteria_version_id: Mapped[UUID] = mapped_column(nullable=False)


class MatchResultRow(Base):
    __tablename__ = "match_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", *JOB_IDENTITY],
            ["match_jobs.id", *[f"match_jobs.{name}" for name in JOB_IDENTITY]],
            name="fk_match_result_work",
        ),
        UniqueConstraint("job_id", name="uq_match_result_job"),
        Index("ix_match_results_campaign", "campaign_id", "created_at"),
        Index("ix_match_results_tender", "tender_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    job_id: Mapped[UUID]
    tender_id: Mapped[UUID]
    tender_version_id: Mapped[UUID]
    campaign_id: Mapped[UUID]
    criteria_version_id: Mapped[UUID]
    matcher_implementation: Mapped[str] = mapped_column(Text)
    matcher_version: Mapped[str] = mapped_column(Text)
    settings_hash: Mapped[str] = mapped_column(Text)
    outcome: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completion_token: Mapped[UUID]


class TelegramNotificationRow(Base):
    __tablename__ = "telegram_notifications"
    __table_args__ = (
        UniqueConstraint("campaign_id", "tender_id", "tender_version_id", name="uq_telegram_match"),
        CheckConstraint(
            "status IN ('pending', 'uncertain', 'delivered', 'superseded')",
            name="ck_telegram_status",
        ),
        Index("ix_telegram_pending", "available_at", postgresql_where=text("status = 'pending'")),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id"))
    tender_id: Mapped[UUID] = mapped_column(ForeignKey("tenders.id"))
    tender_version_id: Mapped[UUID]
    result_id: Mapped[UUID] = mapped_column(ForeignKey("match_results.id"))
    message: Mapped[str] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(Text)
    destination: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int]
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserRow(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN (\x27user\x27, \x27admin\x27)", name="ck_users_role"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failed_logins: Mapped[int] = mapped_column(default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSessionRow(Base):
    __tablename__ = "user_sessions"
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )


class AccountCampaignRow(Base):
    __tablename__ = "account_campaigns"
    __table_args__ = (
        CheckConstraint("interval_seconds >= 60", name="ck_account_campaign_interval"),
    )
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(nullable=False)
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountSourceRow(Base):
    __tablename__ = "account_sources"
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)


def account_campaign_allowed(session: Session, campaign_id: UUID, source_id: str) -> bool:
    """Only eligible shared sources may feed customer campaigns."""
    campaign_owner = session.get(AccountCampaignRow, campaign_id)
    source_owner = session.get(AccountSourceRow, source_id)
    if source_owner is not None and campaign_owner is None:
        return False
    if campaign_owner is not None:
        user = session.get(UserRow, campaign_owner.user_id)
        if user is None or not user.active:
            return False
        if source_id:
            from tender_system.operator.source_approval import eligible

            if not eligible(session, source_id):
                return False
    return True
