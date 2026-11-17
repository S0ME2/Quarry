"""Operator application services and safe operational projections."""

import os
import re
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tender_system.collection.source_runs import RunReceipt
from tender_system.collection.validation_evidence import fingerprint
from tender_system.core.configuration import Configuration, merge
from tender_system.core.domain import (
    CampaignCriteria,
    CriteriaVersion,
    ExecutionLimits,
    FixResult,
    HealthDecision,
    MatchResult,
    RecipeRevision,
    SourceRecipe,
    Tender,
)
from tender_system.core.ports import EvidenceStore
from tender_system.core.tenders import ConfigurableTenderValidity
from tender_system.operator.operator_contracts import (
    OperationView,
    SourceWrite,
)
from tender_system.operator.product_contracts import (
    CampaignDetail,
    CampaignView,
    CampaignWrite,
    Page,
    PageQuery,
    TenderView,
)
from tender_system.persistence.matching_storage import SqlCampaignRepository, SqlMatchJobRepository
from tender_system.persistence.recipe_storage import source_lock
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    CampaignRow,
    CriteriaRow,
    MatchJobRow,
    OperationRow,
    OperatorConfigRow,
    RecipeRevisionRow,
    SourceRow,
    SourceRunRow,
    SqlTenderRepository,
    TenderObservationRow,
    TenderRow,
    TenderVersionRow,
)

SECRET_NAMES = re.compile(r"authorization|cookie|password|secret|token|api.key|session", re.I)


class OperationRestriction(ValueError):
    """Application-authored diagnostic text safe for operator output."""

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        evidence_references: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.evidence_references = evidence_references


def operation_lock(session: Session, key: UUID) -> bool:
    return bool(
        session.scalar(
            text("select pg_try_advisory_xact_lock(:key)"),
            {"key": key.int % (2**63)},
        )
    )


def recover_operations(session: Session, now: datetime) -> int:
    """An active worker holds its advisory lock; disconnect releases ownership."""
    recovered = 0
    for row in session.scalars(
        select(OperationRow)
        .where(OperationRow.status == "running")
        .with_for_update(skip_locked=True)
    ):
        if operation_lock(session, row.id):
            receipt_row = session.get(SourceRunRow, row.id) if row.kind == "collection" else None
            receipt = RunReceipt.model_validate(receipt_row.receipt) if receipt_row else None
            origin_id = (
                row.payload.get("operation_id")
                if row.kind in {"activation", "source_setup", "recipe_generation"}
                else None
            )
            origin = session.get(OperationRow, UUID(str(origin_id))) if origin_id else None
            if row.kind == "automatic_matching":
                # Job leases may also be used by another operation. Requeue the
                # operation, but let matching honor each lease before recovery.
                row.status, row.error_code, row.finished_at = "queued", None, None
                row.result = {
                    **row.result,
                    "recovered_after_disconnect": True,
                    "message": "Worker disconnected; unspent evaluations queued for recovery. "
                    "Reserved provider requests are not repeated.",
                }
            elif receipt is not None:
                # Publication and final operation status use separate transactions. A dead
                # worker can leave an already committed receipt; never label it unwritten.
                row.status = "completed" if receipt.status == "accepted" else "failed"
                row.error_code = (
                    None
                    if receipt.status == "accepted"
                    else receipt.failure.error_code
                    if receipt.failure
                    else receipt.reasons[0]
                    if receipt.reasons
                    else "publication_rejected"
                )
                row.finished_at = now
                row.result = {
                    **row.result,
                    "run_id": str(receipt.id),
                    "stage": "database_publication",
                    "publication": receipt.status,
                    "records_processed": len(receipt.tender_ids),
                    "publication_persisted": receipt.status == "accepted",
                    "recovered_after_commit": True,
                    "evidence_reference": receipt.evidence_reference,
                    "message": "Worker disconnected after the source-run receipt committed.",
                    "safe_recovery_action": "Inspect the committed run; do not replay publication.",
                }
            elif (
                origin is not None
                and origin.status == "activated"
                and origin.result.get("activation_operation_id") == str(row.id)
                and origin.source_id == row.source_id
                and session.get(ActiveRecipeRow, row.source_id) is not None
            ):
                row.status, row.error_code, row.finished_at = "activated", None, now
                row.result = {
                    **row.result,
                    "stage": "activation",
                    "source_id": row.source_id,
                    "manual_only": origin.result.get("manual_only", False),
                    "source_activation_persisted": True,
                    "recovered_after_commit": True,
                    "message": "Worker disconnected after source activation committed.",
                    "safe_recovery_action": "Inspect the active source; do not repeat approval.",
                }
            else:
                unknown_activation_provenance = (
                    origin is not None
                    and origin.status == "activated"
                    and origin.result.get("activation_operation_id") is None
                    and origin.source_id == row.source_id
                )
                row.status, row.error_code, row.finished_at = "failed", "worker_interrupted", now
                row.result = {
                    **row.result,
                    "stage": row.result.get("stage") or row.kind,
                    "publication_persisted": False,
                    "source_activation_persisted": None if unknown_activation_provenance else False,
                    "message": "Legacy activation provenance is unknown; inspect the active source."
                    if unknown_activation_provenance
                    else "Worker disconnected before a committed result was recorded.",
                    "safe_recovery_action": "Inspect evidence and operation status before an "
                    "explicit retry; no retry occurred automatically.",
                }
            recovered += 1
    return recovered


def sanitized(value: Any) -> JsonValue:
    """Never deliver credential-bearing keys, URL queries or provisioned secret values."""
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]"
                if SECRET_NAMES.search(str(key))
                and not (
                    key in {"maximum_output_tokens", "input_token_estimate"}
                    and (item is None or type(item) is int and item >= 0)
                )
                else sanitized(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitized(item) for item in value]
    if isinstance(value, str):
        for key, secret in os.environ.items():
            if SECRET_NAMES.search(key) and len(secret) >= 8:
                value = value.replace(secret, "[redacted]")
        if value.startswith(("https://", "http://")):
            parsed = urlsplit(value)
            value = urlunsplit(
                (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
            )
        return re.sub(r"(?i)(bearer|basic)\s+\S+", r"\1 [redacted]", value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def document(value: Any) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], sanitized(value))


def coverage_status(health: HealthDecision | None) -> str:
    """Coverage describes evidence, independently of whether collection succeeded."""
    if health is None:
        return "uncertain"
    if (
        health.publication_allowed
        and health.inventory_mode == "full"
        and health.completeness == "complete"
        and health.dimensions.get("inventory") == "pass"
        and not health.malformed_count
        and not health.duplicate_count
    ):
        return "verified_full_inventory"
    return "incomplete" if health.completeness == "incomplete" else "uncertain"


def effective_configuration(session: Session, base: Configuration) -> Configuration:
    data = base.model_dump(by_alias=True)
    # Omitted scoped fields must remain omitted across repeated resolution.
    data["sources"] = {
        key: value.model_dump(exclude_unset=True) for key, value in base.sources.items()
    }
    data["campaigns"] = {
        key: value.model_dump(exclude_unset=True) for key, value in base.campaigns.items()
    }
    for row in session.scalars(select(OperatorConfigRow)):
        if row.key.startswith("source/"):
            key = row.key.removeprefix("source/")
            data["sources"][key] = merge(
                data["sources"].get(key, {}), cast(dict[str, object], row.value.get("override", {}))
            )
        elif row.key.startswith("model/"):
            slot = row.key.removeprefix("model/")
            path = {
                "matcher": ("campaign", "model"),
                "stronger_matcher": ("campaign", "routing", "stronger_model"),
            }.get(slot)
            if path is None:
                continue
            target = data["global"]
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = merge(target.get(path[-1]) or {}, cast(dict[str, object], row.value))
            if slot == "matcher":
                data["global"]["campaign"]["matcher_adapter"] = (
                    "rules" if row.value["provider"] == "unconfigured" else "intelligent"
                )
    if not base.runtime.ai_semantic_enabled:
        data["global"]["campaign"]["matcher_adapter"] = "rules"
        data["global"]["campaign"]["routing"]["semantic_enabled"] = False
    return Configuration.model_validate(data)


def operation_view(row: OperationRow) -> OperationView:
    return OperationView(
        id=row.id,
        kind=row.kind,
        source_id=row.source_id,
        status=row.status,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
        result=document(row.result),
    )


class OperatorService:
    def __init__(self, session: Session, config: Configuration, now: datetime):
        self.session, self.base, self.now = session, config, now
        self.config = effective_configuration(session, config)

    def write(self, key: str, value: dict[str, JsonValue]) -> None:
        self.session.execute(
            insert(OperatorConfigRow)
            .values(key=key, value=value, updated_at=self.now)
            .on_conflict_do_update(
                index_elements=[OperatorConfigRow.key],
                set_={"value": value, "updated_at": self.now},
            )
        )

    def source(self, key: str) -> SourceRow:
        row = self.session.get(SourceRow, key)
        if row is None:
            raise LookupError("source unavailable")
        return row

    def control(self, key: str) -> dict[str, JsonValue]:
        row = self.session.get(OperatorConfigRow, f"source/{key}", populate_existing=True)
        return dict(row.value) if row else {}

    def source_view(self, row: SourceRow) -> dict[str, JsonValue]:
        settings, control = self.config.for_source(row.id), self.control(row.id)
        runs = self.run_receipts(row.id)
        last = runs[0] if runs else None
        good = next((run for run in runs if run.status == "accepted"), None)
        active = self.session.get(ActiveRecipeRow, row.id)
        revision = self.session.get(RecipeRevisionRow, (row.id, active.version)) if active else None
        recipe = RecipeRevision.model_validate(revision.revision).recipe if revision else None
        manual = self.base.runtime.manual_only or bool(control.get("manual_only", False))
        health = last.health if last else None
        enabled = bool(control.get("enabled", True))
        return document(
            {
                "id": row.id,
                "name": row.name,
                "enabled": enabled,
                "health": health.overall_status
                if health
                else "failed"
                if last and last.status == "failed"
                else "unknown",
                "collection_status": "failed"
                if last and last.status != "accepted"
                else (
                    "bounded_success"
                    if health and health.inventory_mode == "sampled"
                    else "success"
                    if last
                    else "not_run"
                ),
                "coverage_status": coverage_status(health),
                "freshness_status": health.freshness.status if health else "unknown",
                "last_success": good.finished_at if good else None,
                "manual_only": manual,
                "source_manual_only": bool(control.get("manual_only", False)),
                "global_manual_only": self.base.runtime.manual_only,
                "validation_sample_limits": control.get("approved_execution_limits"),
                "tender_count": self.session.scalar(
                    select(func.count()).select_from(TenderRow).where(TenderRow.source_id == row.id)
                )
                or 0,
                "recipe_version": recipe.version if recipe else None,
                "acquisition": settings.acquisition_adapter,
                "strategy": [step.type for step in recipe.steps] if recipe else [],
                "browser_fallback": settings.browser_fallback_enabled,
                "absence_accounting": health.absence_evidence_allowed if health else False,
                "warnings": [
                    r.model_dump(mode="json") for r in health.reasons if r.severity != "info"
                ]
                if health
                else list(last.reasons)
                if last
                else ["No collection evidence yet"],
                "authentication_strategy": "managed session"
                if settings.authorized_session_reference
                else "public / recipe-managed",
            }
        )

    def sources(self) -> dict[str, JsonValue]:
        return {
            "items": [
                self.source_view(row)
                for row in self.session.scalars(select(SourceRow).order_by(SourceRow.name))
            ]
        }

    def source_detail(self, key: str) -> dict[str, JsonValue]:
        source = self.source(key)
        active = self.session.get(ActiveRecipeRow, key)
        revisions = list(
            self.session.scalars(
                select(RecipeRevisionRow)
                .where(RecipeRevisionRow.source_id == key)
                .order_by(RecipeRevisionRow.created_at.desc())
            )
        )
        return document(
            {
                **self.source_view(source),
                "configuration": self.config.for_source(key).model_dump(mode="json"),
                "configured_override": self.base.sources[key].model_dump(
                    mode="json", exclude_unset=True
                )
                if key in self.base.sources
                else {},
                "operator_override": self.control(key).get("override", {}),
                "recipe": next(
                    (r.revision for r in revisions if active and r.version == active.version), None
                ),
                "recipe_versions": [
                    {"version": r.version, "created_at": r.created_at, "revision": r.revision}
                    for r in revisions
                ],
                "runs": [self.run_view(r) for r in self.run_receipts(key)[:50]],
            }
        )

    def configure_source(self, key: str, body: SourceWrite) -> dict[str, JsonValue]:
        if (
            self.session.get(SourceRow, key) is None
            and self.session.get(OperatorConfigRow, "web/source/" + key) is None
        ):
            raise LookupError("source unavailable")
        source_lock(self.session, key)
        # Serialize concurrent edits to the same source, including validation and collection state.
        self.session.execute(select(SourceRow).where(SourceRow.id == key).with_for_update())
        value = self.control(key)
        if body.enabled is not None:
            value["enabled"] = body.enabled
        if "manual_only" in body.model_fields_set:
            value["manual_only"] = body.manual_only
        if body.configuration:
            value["override"] = cast(
                JsonValue,
                merge(
                    cast(dict[str, object], value.get("override", {})),
                    body.configuration.model_dump(mode="json", exclude_unset=True),
                ),
            )
        self.write(f"source/{key}", value)
        self.config = effective_configuration(self.session, self.base)
        return self.source_detail(key) if self.session.get(SourceRow, key) else value

    def run_receipts(self, source: str | None = None) -> list[RunReceipt]:
        query = select(SourceRunRow)
        if source:
            query = query.where(SourceRunRow.source_id == source)
        runs = [RunReceipt.model_validate(row.receipt) for row in self.session.scalars(query)]
        runs.sort(
            key=lambda run: (
                run.finished_at or run.started_at or datetime.min.replace(tzinfo=self.now.tzinfo),
                str(run.id),
            ),
            reverse=True,
        )
        return runs

    def run_view(self, run: RunReceipt) -> dict[str, JsonValue]:
        versions = self.session.scalars(
            select(TenderVersionRow).where(
                TenderVersionRow.observation_id.in_(
                    select(TenderObservationRow.id).where(
                        TenderObservationRow.source_run_id == run.id
                    )
                )
            )
        )
        rows = list(versions)
        return document(
            {
                **run.model_dump(mode="json"),
                "source_name": self.source(run.source_id).name,
                "duration_seconds": (run.finished_at - run.started_at).total_seconds()
                if run.finished_at and run.started_at
                else None,
                "records_seen": run.health.raw_count if run.health else None,
                "records_new": sum(row.previous_version_id is None for row in rows),
                "records_changed": sum(row.previous_version_id is not None for row in rows),
            }
        )

    def runs(self, source: str | None = None) -> dict[str, JsonValue]:
        return {
            "items": [self.run_view(run) for run in self.run_receipts(source)[:100]],
            "operations": [
                v.model_dump(mode="json")
                for v in self.operations("collection")
                if v.status in {"queued", "running", "failed"}
            ],
        }

    def run(self, key: UUID) -> dict[str, JsonValue]:
        row = self.session.get(SourceRunRow, key)
        if row is None:
            raise LookupError("run unavailable")
        return self.run_view(RunReceipt.model_validate(row.receipt))

    def evidence(self, key: UUID, store: EvidenceStore | None) -> dict[str, JsonValue]:
        run = self.run(key)
        metadata: dict[str, JsonValue] = {
            "reference": run["evidence_reference"],
            "available": False,
        }
        if store:
            try:
                stored = store.get(str(run["evidence_reference"]))
                metadata.update(available=True, kind=stored.get("kind"), fields=list(stored))
            except (ValueError, FileNotFoundError, KeyError):
                pass
        return metadata

    def enqueue(
        self, kind: str, source_id: str | None, payload: dict[str, JsonValue]
    ) -> OperationView:
        row = OperationRow(
            id=uuid4(),
            kind=kind,
            source_id=source_id,
            status="queued",
            payload=payload,
            result={},
            created_at=self.now,
        )
        self.session.add(row)
        self.session.flush()
        return operation_view(row)

    def collect(self, key: str, limits: ExecutionLimits | None = None) -> OperationView:
        self.source(key)
        if not self.control(key).get("enabled", True):
            raise ValueError("enable source before collection")
        if self.session.get(ActiveRecipeRow, key) is None:
            raise ValueError("Source has no working recipe; save and validate a recipe first")
        return self.enqueue(
            "collection", key, {"limits": limits.model_dump(mode="json")} if limits else {}
        )

    def import_recipe(
        self,
        recipe: SourceRecipe,
        url: str,
        *,
        revalidate_source: str | None = None,
        replace_active_source: bool = False,
    ) -> OperationView:
        """Stage an external recipe for explicit, independently revalidated approval."""
        source_lock(self.session, recipe.source_id)
        active = self.session.get(ActiveRecipeRow, recipe.source_id)
        if replace_active_source:
            if (
                revalidate_source is not None
                or active is None
                or self.session.get(SourceRow, recipe.source_id) is None
            ):
                raise OperationRestriction("Select an existing approved source for revision")
            if self.session.get(RecipeRevisionRow, (recipe.source_id, recipe.version)) is not None:
                raise OperationRestriction(
                    "Use a new recipe version; existing revisions are immutable"
                )
        elif revalidate_source is not None:
            revision = (
                self.session.get(RecipeRevisionRow, (recipe.source_id, active.version))
                if active
                else None
            )
            if (
                revalidate_source != recipe.source_id
                or self.session.get(SourceRow, recipe.source_id) is None
                or revision is None
                or fingerprint(
                    RecipeRevision.model_validate(revision.revision).recipe.model_dump(mode="json")
                )
                != fingerprint(recipe.model_dump(mode="json"))
            ):
                raise OperationRestriction(
                    "scope revalidation requires the unchanged active source recipe"
                )
        elif self.session.get(SourceRow, recipe.source_id) is not None:
            raise ValueError(
                "source already registered; use --revalidate SOURCE_ID for the unchanged recipe"
            )
        if (
            revalidate_source is None
            and not replace_active_source
            and (
                active is not None
                or self.session.scalar(
                    select(RecipeRevisionRow.source_id)
                    .where(RecipeRevisionRow.source_id == recipe.source_id)
                    .limit(1)
                )
            )
        ):
            raise ValueError("source already has a recipe")
        pending = self.session.scalars(
            select(OperationRow).where(
                OperationRow.source_id == recipe.source_id,
                OperationRow.status.in_(("awaiting_approval", "queued", "running")),
            )
        )
        if any(row.result.get("manual_recipe_import") is True for row in pending):
            raise ValueError("source already has a pending recipe import")
        proposal = FixResult(
            status="fixed",
            updated_recipe=recipe,
            explanation=(
                "Externally generated declarative recipe; revalidation required at approval."
            ),
            tests_performed=("recipe_schema_validation",),
            confidence=0,
        )
        row = OperationRow(
            id=uuid4(),
            kind="onboarding",
            source_id=recipe.source_id,
            status="awaiting_approval",
            payload={"manual_recipe_import": True},
            result={
                "proposal": proposal.model_dump(mode="json"),
                "url": url,
                "validation_passed": False,
                "manual_recipe_import": True,
                "scope_revalidation": revalidate_source is not None,
                "replacement_baseline_version": active.version
                if replace_active_source and active
                else None,
                "recipe_schema_validated": True,
            },
            created_at=self.now,
        )
        self.session.add(row)
        self.session.flush()
        return operation_view(row)

    def replace_recipe(self, key: UUID, recipe: SourceRecipe, url: str) -> OperationView:
        source_lock(self.session, recipe.source_id)
        row = self.session.scalar(
            select(OperationRow).where(OperationRow.id == key).with_for_update()
        )
        if (
            row is None
            or row.source_id != recipe.source_id
            or row.result.get("manual_recipe_import") is not True
            or row.status not in {"awaiting_approval", "failed"}
            or (
                self.session.get(SourceRow, recipe.source_id) is not None
                and not row.result.get("scope_revalidation")
                and not row.result.get("replacement_baseline_version")
            )
        ):
            raise OperationRestriction(
                "only a pending import with the same source identity can be replaced"
            )
        busy = self.session.scalar(
            select(OperationRow.id)
            .where(
                OperationRow.source_id == recipe.source_id,
                OperationRow.status.in_(("queued", "running")),
            )
            .limit(1)
        )
        if busy is not None:
            raise OperationRestriction(
                "finish pending source operations before replacing the recipe"
            )
        proposal = FixResult.model_validate(row.result["proposal"])
        scope_revalidation = False
        replacement_baseline: str | None = None
        active = self.session.get(ActiveRecipeRow, recipe.source_id)
        if active:
            prior = proposal.updated_recipe
            baseline = row.result.get("replacement_baseline_version") or (
                prior.version if prior else None
            )
            if active.version != baseline:
                raise OperationRestriction(
                    "Active recipe changed; reject this draft and start a new revision"
                )
            revision = self.session.get(RecipeRevisionRow, (recipe.source_id, active.version))
            assert revision is not None
            current = RecipeRevision.model_validate(revision.revision).recipe
            scope_revalidation = fingerprint(current.model_dump(mode="json")) == fingerprint(
                recipe.model_dump(mode="json")
            )
            if not scope_revalidation:
                if (
                    self.session.get(RecipeRevisionRow, (recipe.source_id, recipe.version))
                    is not None
                ):
                    raise OperationRestriction(
                        "Use a new recipe version; existing revisions are immutable"
                    )
                replacement_baseline = active.version
        row.result = {
            "proposal": proposal.model_copy(update={"updated_recipe": recipe}).model_dump(
                mode="json"
            ),
            "url": url,
            "manual_recipe_import": True,
            "recipe_schema_validated": True,
            "validation_passed": False,
            "scope_revalidation": scope_revalidation,
            "replacement_baseline_version": replacement_baseline,
        }
        row.status, row.error_code, row.finished_at = "awaiting_approval", None, None
        self.session.flush()
        return operation_view(row)

    def operations(self, kind: str | None = None) -> tuple[OperationView, ...]:
        query = select(OperationRow).order_by(OperationRow.created_at.desc()).limit(100)
        if kind:
            query = query.where(OperationRow.kind == kind)
        return tuple(operation_view(row) for row in self.session.scalars(query))

    def operation(self, key: UUID) -> OperationView:
        row = self.session.get(OperationRow, key)
        if row is None:
            raise LookupError("operation unavailable")
        return operation_view(row)

    def stored_tenders(
        self, limit: int = 20, after: UUID | None = None, source: str | None = None
    ) -> dict[str, JsonValue]:
        """Operator catalog inspection before matching; bounded repository scan."""
        if not 1 <= limit <= 100:
            raise ValueError("tender limit must be between 1 and 100")
        items = SqlTenderRepository(self.session).scan(after, limit, source_id=source)
        return document(
            {
                "items": [
                    {
                        **item.model_dump(mode="json", exclude={"raw_data"}),
                        "id": str(item.internal_id),
                    }
                    for item in items
                ],
                "next_after": str(items[-1].internal_id) if len(items) == limit else None,
            }
        )

    def generated_recipe(self, key: UUID) -> SourceRecipe:
        """Authorized export of the canonical validated proposal, preserving URL parameters."""
        row = self.session.get(OperationRow, key)
        if row is None:
            raise LookupError("operation unavailable")
        proposal = row.result.get("proposal")
        if not isinstance(proposal, dict) or not proposal.get("updated_recipe"):
            raise ValueError("operation has no generated recipe")
        return SourceRecipe.model_validate(proposal["updated_recipe"])

    def act(self, key: UUID, action: str) -> OperationView:
        row = self.session.scalar(
            select(OperationRow).where(OperationRow.id == key).with_for_update()
        )
        if row is None:
            raise LookupError("operation unavailable")
        if row.status in {"queued", "running", "activated"}:
            raise ValueError("operation cannot change in current state")
        if action == "approve":
            if row.status != "awaiting_approval" or not row.result.get("validation_passed"):
                raise ValueError("only validated proposals can be approved")
            if row.kind == "onboarding":
                pending = self.session.scalar(
                    select(OperationRow.id)
                    .where(
                        OperationRow.kind == "activation",
                        OperationRow.payload["operation_id"].as_string() == str(row.id),
                        OperationRow.status.in_(("queued", "running")),
                    )
                    .limit(1)
                )
                if pending is not None:
                    raise OperationRestriction(
                        f"Approval is already queued or running as operation {pending}; "
                        "inspect it first"
                    )
                return self.enqueue("activation", row.source_id, {"operation_id": str(row.id)})
            raise ValueError("operation is not a recipe import")
        if action in {"reject", "escalate"}:
            row.status = "rejected" if action == "reject" else "escalated"
            return operation_view(row)
        if action == "retry":
            return self.enqueue(row.kind, row.source_id, row.payload)
        raise ValueError("unsupported action")

    def write_campaign(self, body: CampaignWrite) -> CampaignView:
        from tender_system.core.clock import SystemClock
        from tender_system.matching.matching import CampaignService, MatchingPlanner
        from tender_system.persistence.matching_storage import catalog_lock

        catalog_lock(self.session)
        planner = MatchingPlanner(
            SqlCampaignRepository(self.session),
            SqlMatchJobRepository(self.session),
            SqlTenderRepository(self.session),
            self.config,
            automatic=False,
        )
        key = uuid4()
        CampaignService(SqlCampaignRepository(self.session), planner, SystemClock()).create(
            key, uuid4(), body.criteria, active=body.active
        )
        return self.campaign(key)

    def campaign_ids(self) -> tuple[UUID, ...]:
        query = select(CampaignRow.id).where(CampaignRow.deleted_at.is_(None))
        return tuple(self.session.scalars(query.order_by(CampaignRow.id)))

    def campaign(self, campaign_id: UUID) -> CampaignView:
        campaign = SqlCampaignRepository(self.session).get(campaign_id)
        if campaign is None:
            raise LookupError("campaign unavailable")
        criteria = SqlCampaignRepository(self.session).criteria(
            campaign.current_criteria_version_id
        )
        counts = dict(
            self.session.execute(
                select(MatchJobRow.status, func.count())
                .where(
                    MatchJobRow.campaign_id == campaign_id,
                    MatchJobRow.criteria_version_id == campaign.current_criteria_version_id,
                )
                .group_by(MatchJobRow.status)
            ).all()
        )
        technical_failures = sum(
            result.criteria_version_id == campaign.current_criteria_version_id
            and uncertainty_kind(result.outcome) == "technical_failure"
            and result.tender_version_id == self.tender_view(result.tender_id).version_id
            for result in SqlMatchJobRepository(self.session).results(campaign_id=campaign_id)
        )
        view = CampaignView(
            **campaign.model_dump(),
            criteria=criteria.criteria,
            criteria_number=criteria.number,
            processing=counts.get("pending", 0) + counts.get("running", 0),
            failed=counts.get("failed", 0) + technical_failures,
        )
        return view

    def campaigns(self, query: PageQuery) -> Page[CampaignView]:
        items = [self.campaign(key) for key in self.campaign_ids()]
        return Page(
            items=tuple(items[query.offset : query.offset + query.limit]),
            total=len(items),
            offset=query.offset,
            limit=query.limit,
        )

    def campaign_detail(self, key: UUID) -> CampaignDetail:
        campaign = self.campaign(key)
        rows = self.session.scalars(
            select(CriteriaRow)
            .where(CriteriaRow.campaign_id == key)
            .order_by(CriteriaRow.number.desc())
        )
        return CampaignDetail(
            campaign=campaign,
            versions=tuple(
                CriteriaVersion(
                    id=row.id,
                    campaign_id=row.campaign_id,
                    criteria=CampaignCriteria.model_validate(row.criteria),
                    created_at=row.created_at,
                    previous_version_id=row.previous_version_id,
                    number=row.number,
                )
                for row in rows
            ),
        )

    def tender_view(self, key: UUID) -> TenderView:
        tender = SqlTenderRepository(self.session).get(key)
        if tender is None:
            raise LookupError("tender unavailable")
        return self._tender_view(tender)

    def tender_views(self, keys: tuple[UUID, ...]) -> list[TenderView]:
        return [
            self._tender_view(tender) for tender in SqlTenderRepository(self.session).get_many(keys)
        ]

    def _tender_view(self, tender: Tender) -> TenderView:
        validity = ConfigurableTenderValidity().evaluate(
            tender, self.config.for_source(tender.source_id), self.now
        )
        view = TenderView(
            id=tender.internal_id,
            version_id=tender.current_version_id,
            title=tender.title,
            description=tender.description,
            buyer=tender.buyer,
            location=tender.location,
            value=tender.value,
            currency=tender.currency,
            deadline=tender.deadline,
            published_at=tender.published_at,
            observed_at=tender.last_seen_at,
            source=tender.source_id,
            source_url=tender.source_url,
            status=tender.status,
            valid=validity.matchable,
            validity_reasons=validity.reasons,
        )
        return view


def uncertainty_kind(outcome: MatchResult) -> str:
    if outcome.result != "unsure":
        return outcome.result
    if any(call.error_code for call in outcome.model_calls) or any(
        reason.code in {"model_failure", "model_unavailable"}
        for reason in (*outcome.reasons, *outcome.uncertainties)
    ):
        return "technical_failure"
    return "uncertain"
