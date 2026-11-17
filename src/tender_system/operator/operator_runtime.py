"""Durable workspace operations over bounded collection, validation and matching."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from time import perf_counter
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import httpx
from pydantic import JsonValue, ValidationError
from sqlalchemy import Text, case, exists, func, select, text
from sqlalchemy.orm import Session, aliased, sessionmaker

from tender_system.collection.adapters import FileEvidenceStore, FileSessionStore, HttpxAdapter
from tender_system.collection.browser import PlaywrightBrowserAdapter
from tender_system.collection.health import ConservativeRunHealth
from tender_system.collection.network import (
    PublicHTTPTransport,
    PublicReadOnlyHttp,
    UnsafeWebsite,
    public_acquisition,
    public_url,
)
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.validation_evidence import (
    CapturedBrowserSteps,
    CapturedResponseHttpAdapter,
    fingerprint,
)
from tender_system.collection.verification import BrowserWitnesses as BrowserWitnesses
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import (
    Configuration,
    ModelSettings,
    SourceCalibration,
    SourceOverride,
    SourceSettings,
    load_configuration,
    merge,
)
from tender_system.core.domain import (
    BoundedCollectionResult,
    CollectionRun,
    ExecutionContext,
    ExecutionLimits,
    FixResult,
    RecipeRevision,
    SourceFailure,
    SourceRecipe,
    SourceRecord,
    SourceSnapshot,
    TenderCandidate,
)
from tender_system.core.execution import (
    ExecutionDeadlineExceeded,
    effective_execution_limits,
    execution_budget,
    fence_execution,
    remaining_seconds,
    transport_usage,
)
from tender_system.core.ports import (
    BrowserAdapter,
    Clock,
    EvidenceStore,
    HttpAdapter,
    ModelClient,
    SampleVerifier,
)
from tender_system.core.tenders import comparison_state_of, identity_of
from tender_system.matching.model_providers import model_client
from tender_system.operator.operations import (
    OperationRestriction,
    OperatorService,
    coverage_status,
    document,
    effective_configuration,
    operation_lock,
    recover_operations,
)
from tender_system.persistence.recipe_storage import (
    SqlRecipeCatalog,
    source_lock,
)
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    OperationRow,
    OperatorConfigRow,
    RecipeRevisionRow,
    SourceRow,
    SqlSourceRepository,
    TransactionalRunStore,
)
from tender_system.repair.repair_adapters import RecipeRepairExecutors


class OperatorRuntime:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        config: Configuration,
        evidence: EvidenceStore | None = None,
        clock: Clock | None = None,
        browsers: Callable[[], BrowserAdapter] | None = None,
        models: Callable[..., ModelClient] = model_client,
        http: HttpAdapter | None = None,
        sample_verifier: SampleVerifier | None = None,
        public_url_check: Callable[[str], None] = public_url,
    ):
        self.sessions, self.config = sessions, config
        self.clock = clock or SystemClock()
        self.evidence = evidence or FileEvidenceStore(config.runtime.evidence_directory)
        self.http = http or HttpxAdapter()
        self.public_transport = HttpxAdapter(
            httpx.Client(
                follow_redirects=False,
                trust_env=False,
                transport=PublicHTTPTransport() if public_url_check is public_url else None,
            )
        )
        self.public_url_check = public_url_check
        self.public_http = PublicReadOnlyHttp(http or self.public_transport, public_url_check)
        self.sessions_store = FileSessionStore(config.runtime.session_directory)
        self.browsers = browsers or self.browser
        self.public_browsers = browsers or self.public_browser
        self.models = models
        self.sample_verifier = (
            sample_verifier
            if sample_verifier is not None
            else BrowserWitnesses(
                browsers or (lambda: self.browser(navigation_wait_until="domcontentloaded")),
                self.evidence,
            )
        )
        self.executors = RecipeRepairExecutors(
            lambda: self.http,
            self.browsers,
            self.evidence,
            self.clock,
            public_http=self.public_http,
            public_browsers=self.public_browsers,
        )
        self.normalizer = ConfigurableTenderNormalizer()
        self.catalog = SqlRecipeCatalog(sessions)
        self.stop = Event()
        self.thread: Thread | None = None
        self.last_healthy_poll: float | None = None
        self.poll_failed = False

    def validation_baseline(self, source_id: str) -> dict[str, JsonValue]:
        with self.sessions() as session:
            source = session.get(SourceRow, source_id)
            active = session.get(ActiveRecipeRow, source_id)
            return {
                "source_exists": source is not None,
                "active_recipe_version": active.version if active else None,
            }

    def validation_policy_fingerprint(self, settings: SourceSettings) -> str:
        return fingerprint(
            {"validation_contract_version": 3, "settings": settings.model_dump(mode="json")}
        )

    def validation_evidence(
        self,
        result: dict[str, JsonValue],
        recipe: SourceRecipe,
        source_id: str,
        settings: SourceSettings,
    ) -> dict[str, JsonValue]:
        reference = result.get("validation_evidence_reference")
        if not isinstance(reference, str):
            raise OperationRestriction(
                "recipe validation evidence is missing; validate the recipe first"
            )
        try:
            evidence = dict(self.evidence.get(reference))
        except (OSError, KeyError, ValueError, TypeError):
            raise OperationRestriction("recipe validation evidence is missing or corrupt") from None
        now = self.clock.now()
        created = evidence.get("created_at")
        if not isinstance(created, str):
            raise OperationRestriction("validation evidence timestamp is invalid")
        try:
            created_at = datetime.fromisoformat(created)
        except ValueError:
            raise OperationRestriction("validation evidence timestamp is invalid") from None
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise OperationRestriction("validation evidence timestamp is not timezone aware")
        age = now - created_at.astimezone(UTC)
        if age < timedelta(0) or age > timedelta(minutes=30):
            raise OperationRestriction("validation evidence is stale; validate the recipe again")
        if evidence.get("kind") != "recipe_validation" or evidence.get("smoke_only") is True:
            raise OperationRestriction("smoke or unsupported evidence cannot authorize approval")
        if evidence.get("schema_version") != 3:
            raise OperationRestriction("validation contract changed; validate the recipe again")
        if evidence.get("validation_passed") is not True:
            raise OperationRestriction("independent recipe validation did not pass")
        if evidence.get("recipe_fingerprint") != fingerprint(recipe.model_dump(mode="json")):
            raise OperationRestriction("recipe changed after validation; validate the recipe again")
        if evidence.get("policy_fingerprint") != self.validation_policy_fingerprint(settings):
            raise OperationRestriction(
                "source policy changed after validation; validate the recipe again"
            )
        if evidence.get("execution_limits") != result.get("execution_limits"):
            raise OperationRestriction("validation scope changed; validate the recipe again")
        scope = evidence.get("validation_scope")
        report = evidence.get("report")
        health = report.get("health") if isinstance(report, dict) else None
        raw_reasons = health.get("reasons") if isinstance(health, dict) else None
        reasons = raw_reasons if isinstance(raw_reasons, list) else []
        consistent_inventory = not any(
            isinstance(reason, dict)
            and reason.get("code")
            in {"pagination_consistency_unproven", "inventory_total_unobserved"}
            for reason in reasons
        )
        if (
            scope not in {"sampled", "incremental", "full"}
            or not isinstance(health, dict)
            or health.get("inventory_mode") != scope
            or evidence.get("inventory_authority")
            != (
                scope == "full"
                and health.get("publication_allowed") is True
                and health.get("completeness") == "complete"
                and health.get("malformed_count") == 0
                and health.get("duplicate_count") == 0
                and consistent_inventory
            )
        ):
            raise OperationRestriction("validation scope evidence is inconsistent; revalidate")
        try:
            captured_reference = evidence["execution_evidence_reference"]
            replay_reference = evidence["replay_evidence_reference"]
            if not isinstance(captured_reference, str) or not isinstance(replay_reference, str):
                raise ValueError("missing capture reference")
            captured = self.evidence.get(captured_reference)
            replay = self.evidence.get(replay_reference)
            raw = captured.get("observations")
            repeated = replay.get("observations")
            if (
                not isinstance(raw, list)
                or not isinstance(repeated, list)
                or len(raw) != evidence.get("observed_count")
                or raw != repeated
                or fingerprint(captured.get("recipe"))
                != fingerprint(recipe.model_dump(mode="json"))
                or fingerprint(replay.get("recipe")) != fingerprint(recipe.model_dump(mode="json"))
                or fingerprint(captured.get("settings"))
                != fingerprint(settings.model_dump(mode="json"))
                or not isinstance(report, dict)
                or any(
                    evidence.get(field) != report.get(field)
                    for field in (
                        "observed_count",
                        "validation_scope",
                        "inventory_authority",
                        "expected_count",
                        "approval_witnesses",
                        "execution_limits",
                        "inventory_snapshots",
                        "source_global_count",
                    )
                )
            ):
                raise ValueError("capture binding mismatch")
            diagnostics = captured.get("diagnostics")
            if not isinstance(diagnostics, dict) or diagnostics.get(
                "inventory_snapshots", []
            ) != evidence.get("inventory_snapshots"):
                raise ValueError("inventory snapshot binding mismatch")
            totals = diagnostics.get("inventory_totals", [])
            global_count = totals[-1] if isinstance(totals, list) and totals else None
            if global_count != evidence.get("source_global_count"):
                raise ValueError("source global count binding mismatch")
            source = SourceRecord(id=source_id, name=source_id)
            witnesses = []
            for item in raw[:3]:
                if not isinstance(item, dict):
                    raise ValueError("invalid captured observation")
                candidate = self.normalizer.normalize(item, source, settings).candidate
                if candidate is None:
                    raise ValueError("invalid captured witness")
                witnesses.append(self.candidate_state(candidate, settings))
            if witnesses != evidence.get("approval_witnesses"):
                raise ValueError("witness binding mismatch")
            sample_references = report.get("sample_evidence", [])
            if not isinstance(sample_references, list):
                raise ValueError("invalid independent evidence references")
            for sample_reference in sample_references:
                if not isinstance(sample_reference, str):
                    raise ValueError("invalid independent evidence reference")
                self.evidence.get(sample_reference)
        except (OSError, KeyError, ValueError, TypeError):
            raise OperationRestriction(
                "Validation capture/replay/witness binding is missing or inconsistent; "
                "validate again",
                stage="approval_evidence",
            ) from None
        current_baseline = self.validation_baseline(source_id)
        if evidence.get("baseline") != current_baseline:
            raise OperationRestriction(
                "source baseline changed after validation; validate the recipe again"
            )
        return evidence

    def fresh_source_check(
        self,
        key: UUID,
        source_id: str,
        recipe: SourceRecipe,
        settings: SourceSettings,
        validation: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        approved = validation.get("execution_limits")
        limits = ExecutionLimits.model_validate(approved) if approved else ExecutionLimits()
        sample_size = min(3, limits.max_tenders or 3)
        execution = self.executors(RecipeRevision(recipe=recipe), settings).execute(
            recipe,
            ExecutionContext(
                run_id=key, limits=limits.model_copy(update={"max_tenders": sample_size})
            ),
        )
        if isinstance(execution, SourceFailure):
            raise OperationRestriction(
                f"Approval fresh check failed ({execution.error_code}) at "
                f"recipe step {execution.failed_step or 'unknown'}; inspect captured evidence",
                stage="approval_fresh_check",
                evidence_references=(execution.failed_evidence_reference,),
            )
        requests = execution.diagnostics.transport_attempts
        references = [execution.evidence_reference]
        full_refresh = False
        current_settings = settings.model_copy(
            update={"calibration": SourceCalibration.model_validate(validation["calibration"])}
        )
        if validation.get("validation_scope") == "full":
            # A bounded fresh witness may stop before a recipe's count expression exists.
            # Only a full-authority validation can require a reconciled inventory count.
            global_count = validation.get("source_global_count")
            fresh_total = (
                execution.diagnostics.inventory_totals[-1]
                if execution.diagnostics.inventory_totals
                else None
            )
            snapshot_tokens = validation.get("inventory_snapshots", [])
            if (
                (global_count is not None and fresh_total is None)
                or (global_count is None and execution.expected_count is None)
                or (bool(snapshot_tokens) and not execution.diagnostics.inventory_snapshots)
                or (
                    current_settings.calibration.freshness.basis == "inventory_timestamp"
                    and (
                        isinstance(execution, BoundedCollectionResult)
                        or execution.data_as_of is None
                    )
                )
            ):
                remaining_requests = limits.max_requests - requests
                remaining_bytes = (
                    limits.max_total_response_bytes - execution.diagnostics.response_bytes
                )
                if remaining_requests < 1 or remaining_bytes < 1:
                    raise OperationRestriction(
                        "Approval inventory check exhausted its request budget; "
                        "revalidate with a larger budget",
                        stage="approval_fresh_check",
                        evidence_references=tuple(references),
                    )
                full_refresh = True
                execution = self.executors(RecipeRevision(recipe=recipe), settings).execute(
                    recipe,
                    ExecutionContext(
                        run_id=uuid5(key, "full-inventory-fresh-check"),
                        limits=limits.model_copy(
                            update={
                                "max_requests": remaining_requests,
                                "max_total_response_bytes": remaining_bytes,
                            }
                        ),
                    ),
                )
                if isinstance(execution, SourceFailure):
                    raise OperationRestriction(
                        "Full inventory fresh check failed; "
                        "revalidate within the configured budgets",
                        stage="approval_fresh_check",
                        evidence_references=(execution.failed_evidence_reference,),
                    )
                requests += execution.diagnostics.transport_attempts
                references.append(execution.evidence_reference)
                observed = len(execution.raw_observations)
                if observed != validation.get("observed_count"):
                    raise OperationRestriction(
                        "Full inventory count changed: "
                        f"validated {validation.get('observed_count')}, "
                        f"fresh {observed}; validate again before approval",
                        stage="approval_fresh_check",
                        evidence_references=tuple(references),
                    )
            expected = (
                global_count if global_count is not None else validation.get("expected_count")
            )
            actual = (
                execution.diagnostics.inventory_totals[-1]
                if global_count is not None and execution.diagnostics.inventory_totals
                else execution.expected_count
            )
            if actual != expected:
                raise OperationRestriction(
                    f"Source declared inventory total changed: validated {expected}, "
                    f"fresh {actual}; validate again before approval",
                    stage="approval_fresh_check",
                    evidence_references=tuple(references),
                )
            if not isinstance(snapshot_tokens, list) or set(snapshot_tokens) != set(
                execution.diagnostics.inventory_snapshots
            ):
                raise OperationRestriction(
                    "Source inventory snapshot changed since full validation; validate again",
                    stage="approval_fresh_check",
                    evidence_references=tuple(references),
                )
        source = SourceRecord(id=source_id, name=source_id)
        candidates = tuple(
            item.candidate
            for raw in (
                execution.observations
                if isinstance(execution, BoundedCollectionResult)
                else execution.raw_observations
            )
            if (item := self.normalizer.normalize(raw, source, settings)).candidate is not None
        )
        validated = validation.get("approval_witnesses")
        checked_run = (
            execution.sampled_run() if isinstance(execution, BoundedCollectionResult) else execution
        ).model_copy(update={"candidates": candidates})
        current_health = ConservativeRunHealth().evaluate(
            checked_run,
            current_settings,
            SourceSnapshot(),
            (),
            now=self.clock.now(),
        )
        freshness_errors = [
            reason.code
            for reason in current_health.reasons
            if reason.dimension == "freshness" and reason.severity == "error"
        ]
        if freshness_errors:
            raise OperationRestriction(
                f"Fresh source evidence violates freshness policy ({', '.join(freshness_errors)}); "
                "validate fresh source evidence before approval",
                stage="approval_fresh_check",
                evidence_references=tuple(references),
            )
        quality_errors = [
            reason.code
            for reason in current_health.reasons
            if reason.severity == "error"
            and reason.dimension
            in {"normalization", "extraction", "transport", "session", "content"}
        ]
        if quality_errors:
            raise OperationRestriction(
                f"Fresh source evidence violates source policy ({', '.join(quality_errors)}); "
                "inspect captured source/normalization evidence and validate again",
                stage="approval_fresh_check",
                evidence_references=tuple(references),
            )
        if full_refresh and isinstance(execution, CollectionRun):
            if not current_health.publication_allowed or current_health.completeness != "complete":
                raise OperationRestriction(
                    "Fresh full inventory failed health/completion checks; validate again",
                    stage="approval_fresh_check",
                    evidence_references=tuple(references),
                )
        current = [self.candidate_state(item, settings) for item in candidates[:sample_size]]
        if not isinstance(validated, list) or current != validated:
            raise OperationRestriction(
                "Fresh source identity/content differs from validated witnesses; validate again",
                stage="approval_fresh_check",
                evidence_references=tuple(references),
            )
        return {
            "records_requested": sample_size,
            "records_processed": len(candidates),
            "http_requests": requests,
            "evidence_references": [reference for reference in references],
        }

    @staticmethod
    def candidate_state(
        candidate: TenderCandidate, settings: SourceSettings
    ) -> dict[str, JsonValue]:
        return {
            "identity": identity_of(candidate).model_dump(mode="json"),
            "state": comparison_state_of(
                candidate, settings.normalization, settings.field_change_policy
            ),
        }

    def source_settings(self, config: Configuration, source_id: str) -> SourceSettings:
        settings = config.for_source(source_id)
        return settings

    def browser(
        self,
        *,
        navigation_wait_until: Literal["load", "domcontentloaded"] = "load",
        request_guard: Callable[[str, str], None] | None = None,
        request_http: HttpAdapter | None = None,
    ) -> BrowserAdapter:
        if not self.config.runtime.browser_enabled:
            raise OperationRestriction("browser disabled by QUARRY_BROWSER_ENABLED")
        remaining = remaining_seconds("browser_startup")
        timeout = self.config.runtime.browser_timeout_seconds
        return PlaywrightBrowserAdapter(
            self.sessions_store,
            min(timeout, remaining or timeout),
            navigation_wait_until=navigation_wait_until,
            request_guard=request_guard,
            request_http=request_http,
        )

    def public_browser(self) -> BrowserAdapter:
        def guard(url: str, method: str) -> None:
            if method not in {"GET", "HEAD"}:
                raise UnsafeWebsite(
                    "Automatic browser collection cannot send mutation/RPC requests"
                )
            self.public_url_check(url)

        return self.browser(
            navigation_wait_until="domcontentloaded",
            request_guard=guard,
            request_http=self.public_transport,
        )

    def generation_model(
        self, settings: SourceSettings, config: Configuration
    ) -> tuple[ModelSettings, ModelClient]:
        from tender_system.operator.ai_settings import assignment

        model = settings.fixer_model
        if model.provider == "unconfigured":
            model = config.for_campaign("onboarding").model
            model = model.model_copy(
                update={
                    "max_output_tokens": 8000,
                    "max_attempts": 2,
                    "timeout_seconds": 90,
                }
            )
        with self.sessions() as session:
            model = assignment(session, "recipe_generation", model)
        if model.provider == "groq":
            # Large generation/repair prompts can consume a whole minute's quota.
            model = model.model_copy(
                update={"request_interval_seconds": max(model.request_interval_seconds, 61)}
            )
        return model, self.model_for_settings(model)

    def model_for_settings(self, model: ModelSettings) -> ModelClient:
        if model.provider.startswith("registry/"):
            from tender_system.operator.ai_settings import registry_client

            with self.sessions() as session:
                return registry_client(session, model)
        return self.models(model)

    def start(self) -> None:
        if not self.config.runtime.operator_worker_enabled:
            return
        self.thread = Thread(target=self.loop, daemon=True, name="quarry-operator")
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        if isinstance(self.http, HttpxAdapter) and (not self.thread or not self.thread.is_alive()):
            self.http.close()
        if not self.thread or not self.thread.is_alive():
            self.public_transport.close()

    def loop(self) -> None:
        import logging

        while not self.stop.is_set():
            failed = False
            try:
                self.schedule_due()
            except Exception as error:
                failed = True
                self.poll_failed = True
                logging.getLogger(__name__).error(
                    "Monitoring scheduler unavailable: %s", type(error).__name__
                )
            try:
                self.tick()
            except Exception as error:
                failed = True
                self.poll_failed = True
                logging.getLogger(__name__).error(
                    "Operator worker unavailable: %s", type(error).__name__
                )
            self.poll_failed = failed
            if not failed:
                self.last_healthy_poll = perf_counter()
            self.stop.wait(self.config.runtime.worker_poll_seconds)

    def healthy(self) -> bool:
        """Report a live worker with a recent successful scheduler and queue poll."""
        return bool(
            self.thread
            and self.thread.is_alive()
            and not self.stop.is_set()
            and not self.poll_failed
            and self.last_healthy_poll is not None
            and perf_counter() - self.last_healthy_poll
            <= self.config.runtime.operation_timeout_seconds
            + 2 * self.config.runtime.worker_poll_seconds
        )

    def schedule_due(self) -> int:
        """Enqueue at most one overdue collection per eligible source atomically."""
        if self.config.runtime.manual_only:
            return 0
        now = self.clock.now()
        with self.sessions.begin() as session:
            # Serialize schedulers across API processes; transaction release handles crashes.
            if not session.scalar(
                text("select pg_try_advisory_xact_lock(:key)"), {"key": 5189062794521001}
            ):
                return 0
            svc = OperatorService(session, self.config, now)
            from tender_system.operator.account_monitoring import schedule_accounts
            from tender_system.persistence.storage import AccountSourceRow

            count = schedule_accounts(session, svc, now)
            private_sources = set(session.scalars(select(AccountSourceRow.source_id)))
            for source_id in session.scalars(select(ActiveRecipeRow.source_id)).all():
                if source_id in private_sources:
                    continue
                control = svc.control(source_id)
                if not control.get("enabled", True) or control.get("manual_only", False):
                    continue
                active = session.scalar(
                    select(OperationRow.id)
                    .where(
                        OperationRow.kind == "collection",
                        OperationRow.source_id == source_id,
                        OperationRow.status.in_(("queued", "running")),
                    )
                    .limit(1)
                )
                if active is not None:
                    continue
                last = session.scalar(
                    select(OperationRow.created_at)
                    .where(
                        OperationRow.kind == "collection",
                        OperationRow.source_id == source_id,
                        OperationRow.payload["scheduled"].as_boolean().is_(True),
                    )
                    .order_by(OperationRow.created_at.desc())
                    .limit(1)
                )
                interval = svc.config.for_source(source_id).check_interval_seconds
                if last is not None and last + timedelta(seconds=interval) > now:
                    continue
                from tender_system.persistence.matching_storage import SqlCampaignRepository

                campaigns = SqlCampaignRepository(session)
                keywords: list[str] = []
                for campaign in campaigns.active():
                    criteria = campaigns.criteria(campaign.current_criteria_version_id).criteria
                    if criteria.source_ids and source_id not in criteria.source_ids:
                        continue
                    if not criteria.include_keywords:
                        keywords = []
                        break  # An unrestricted campaign requires normal collection.
                    keywords.extend(criteria.include_keywords)
                payload: dict[str, JsonValue] = {
                    "scheduled": True,
                    "search_keywords": list(dict.fromkeys(keywords)),
                }
                limits = control.get("approved_execution_limits")
                if isinstance(limits, dict):
                    payload["limits"] = limits
                svc.enqueue("collection", source_id, payload)
                count += 1
            return count

    def tick(self, operation_id: UUID | None = None) -> bool:
        with self.sessions.begin() as recovery:
            recover_operations(recovery, self.clock.now())
        # Dedicated ownership transaction survives stage commits and is released on disconnect.
        with self.sessions.begin() as ownership:
            processed = self._tick(operation_id, ownership)
        if processed and operation_id is not None:
            # On-demand scraping dispatches its outbox even when polling is disabled.
            # Additional bounded chunks remain visible/resumable in Activity.
            try:
                with self.sessions() as session:
                    children = tuple(
                        session.scalars(
                            select(OperationRow.id)
                            .where(
                                OperationRow.kind == "automatic_matching",
                                OperationRow.status == "queued",
                                OperationRow.payload["source_run_id"].as_string()
                                == str(operation_id),
                            )
                            .order_by(OperationRow.created_at, OperationRow.id)
                            .limit(10)
                        )
                    )
                for child in children:
                    self.tick(child)
            except Exception as error:
                import logging

                logging.getLogger(__name__).error(
                    "Automatic matching dispatch failed; publication preserved: %s",
                    type(error).__name__,
                )
                # Durable queued operations remain available for explicit resume.
        try:
            from tender_system.operator.telegram import dispatch_notifications

            dispatch_notifications(self.sessions, self.clock.now())
        except Exception as error:
            import logging

            logging.getLogger(__name__).error(
                "Telegram dispatch unavailable; matches preserved: %s", type(error).__name__
            )
        from tender_system.operator.monitoring import refresh_monitoring

        refresh_monitoring(self.sessions, self.clock.now())
        return processed

    def _tick(self, operation_id: UUID | None, ownership: Session) -> bool:
        now = self.clock.now()
        with self.sessions.begin() as session:
            config = effective_configuration(session, self.config)
            parent = aliased(OperationRow)
            # Collection owns document processing until its operation finishes.
            parent_running = exists(
                select(parent.id).where(
                    parent.kind == "collection",
                    parent.status == "running",
                    parent.id.cast(Text) == OperationRow.payload["source_run_id"].as_string(),
                )
            )
            queued = select(OperationRow).where(
                OperationRow.status == "queued",
                (OperationRow.kind != "automatic_matching") | ~parent_running,
                OperationRow.result["next_attempt_at"].as_float().is_(None)
                | (OperationRow.result["next_attempt_at"].as_float() <= now.timestamp()),
            )
            if operation_id is not None:
                queued = queued.where(OperationRow.id == operation_id)
            # Apply the candidate bound to sources, not individual operations.
            # One locked source must not occupy the entire polling window.
            ranked = queued.with_only_columns(
                OperationRow.id,
                func.row_number()
                .over(
                    partition_by=case(
                        (
                            OperationRow.kind.in_(
                                ("collection", "automatic_matching", "recipe_generation")
                            ),
                            OperationRow.source_id,
                        ),
                        else_=OperationRow.id.cast(Text),
                    ),
                    order_by=(
                        case((OperationRow.kind == "automatic_matching", 0), else_=1),
                        OperationRow.created_at,
                        OperationRow.id,
                    ),
                )
                .label("position"),
            ).subquery()
            rows = session.scalars(
                select(OperationRow)
                .where(OperationRow.id.in_(select(ranked.c.id).where(ranked.c.position == 1)))
                .order_by(OperationRow.created_at, OperationRow.id)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
            row = None
            for candidate in rows:
                if candidate.kind == "collection" and session.scalar(
                    select(OperationRow.id)
                    .where(
                        OperationRow.kind == "automatic_matching",
                        OperationRow.source_id == candidate.source_id,
                        OperationRow.status.in_(("queued", "running")),
                    )
                    .limit(1)
                ):
                    continue
                if candidate.kind in (
                    "collection",
                    "automatic_matching",
                    "recipe_generation",
                ) and not ownership.scalar(
                    text("select pg_try_advisory_xact_lock(:namespace, hashtext(:source))"),
                    {"namespace": 1364546124, "source": candidate.source_id},
                ):
                    continue
                if operation_lock(ownership, candidate.id):
                    row = candidate
                    break
            if row is None:
                return False
            row.status, row.started_at, row.finished_at = "running", now, None
            key, kind, source_id, payload = row.id, row.kind, row.source_id, dict(row.payload)
        interrupted: BaseException | None = None
        try:
            duration = config.runtime.operation_timeout_seconds
            limits = payload.get("limits")
            if isinstance(limits, dict):
                duration = min(
                    duration, ExecutionLimits.model_validate(limits).max_duration_seconds
                )

            def assert_ownership() -> None:
                # Uses the original lock connection: a lost connection cannot be replaced silently.
                status = ownership.scalar(select(OperationRow.status).where(OperationRow.id == key))
                if status != "running":
                    raise OperationRestriction("operation no longer owned by this worker")

            with execution_budget(
                duration,
                before_write=assert_ownership,
                limits=ExecutionLimits.model_validate(limits) if isinstance(limits, dict) else None,
            ):
                result, state = self.execute(key, kind, source_id, payload, config)
            error_code = result.get("error_code") if state == "failed" else None
        except BaseException as error:
            interrupted = error if not isinstance(error, Exception) else None
            # Provider codes are safe contracts; exception messages may contain credentials.
            error_code = (
                "worker_interrupted"
                if interrupted
                else "database_unavailable"
                if type(error).__name__ == "OperationalError"
                else "approval_restriction"
                if kind == "activation" and isinstance(error, OperationRestriction)
                else getattr(error, "code", type(error).__name__)
            )
            result = {
                "stage": getattr(error, "stage", None) or kind,
                "message": str(error)
                if isinstance(
                    error, (OperationRestriction, ExecutionDeadlineExceeded, UnsafeWebsite)
                )
                else "Stage failed; inspect error code and referenced evidence.",
                "evidence_references": list(getattr(error, "evidence_references", ())),
            }
            for field in (
                "http_status",
                "input_tokens",
                "output_tokens",
                "attempts",
                "publication_persisted",
                "safe_recovery_action",
            ):
                value = getattr(error, field, None)
                if value is not None or (
                    field == "publication_persisted" and hasattr(error, field)
                ):
                    result[field] = value
            state = "failed"
        with self.sessions.begin() as session:
            finished = session.get(OperationRow, key)
            if finished and finished.status == "running":
                finished.result, finished.status = {**finished.result, **result}, state
                finished.error_code, finished.finished_at = (
                    str(error_code) if error_code else None,
                    None if state == "queued" else self.clock.now(),
                )
        if interrupted is not None:
            raise interrupted
        return True

    def execute(
        self,
        key: UUID,
        kind: str,
        source_id: str | None,
        payload: dict[str, JsonValue],
        config: Configuration,
    ) -> tuple[dict[str, JsonValue], str]:
        if kind == "automatic_matching":
            from tender_system.operator.automatic_matching import execute_automatic

            return execute_automatic(self, key, payload)
        if kind == "web_matching":
            from tender_system.operator.web_matching import execute_matching

            return execute_matching(self, payload)
        if source_id is None:
            raise OperationRestriction("source required")
        from tender_system.persistence.storage import AccountSourceRow, UserRow

        with self.sessions() as session:
            owner = session.get(AccountSourceRow, source_id)
            if owner is not None and kind != "collection":
                account = session.get(UserRow, owner.user_id)
                if account is None or not account.active:
                    raise OperationRestriction("Source owner account unavailable")
        settings = self.source_settings(config, source_id)
        if kind == "recipe_generation":
            with public_acquisition(ExecutionLimits.model_validate(payload["limits"])) as usage:
                result, status = self.generate_source(key, source_id, payload, config)
                result["acquisition"] = {
                    "requests": usage.requests,
                    "response_bytes": usage.response_bytes,
                }
                return result, status
        if kind == "collection":
            # Pin recipe, approval and policy together; release locks before network work.
            with self.sessions.begin() as session:
                source_lock(session, source_id)
                from tender_system.operator.source_approval import eligible

                if (
                    payload.get("account_scheduled") or payload.get("account_campaign_ids")
                ) and not eligible(session, source_id):
                    raise OperationRestriction("source is not approved for monitoring")
                session.execute(
                    select(SourceRow).where(SourceRow.id == source_id).with_for_update()
                )
                source = SqlSourceRepository(session).get(source_id)
                svc = OperatorService(
                    session,
                    self.config,
                    self.clock.now(),
                )
                control = svc.control(source_id)
                if not control.get("enabled", True):
                    raise OperationRestriction("source disabled")
                config = svc.config
                settings = self.source_settings(config, source_id)
                active = session.get(ActiveRecipeRow, source_id)
                row = (
                    session.get(RecipeRevisionRow, (source_id, active.version)) if active else None
                )
                revision = RecipeRevision.model_validate(row.revision) if row else None
            if source is None or revision is None:
                raise OperationRestriction("active source recipe required")
            requested = (
                ExecutionLimits.model_validate(payload["limits"]) if payload.get("limits") else None
            )
            approved = control.get("approved_execution_limits")
            if approved:
                scope = ExecutionLimits.model_validate(approved)
                if scope.max_tenders is not None and (
                    requested is None or requested.max_tenders is None
                ):
                    raise OperationRestriction(
                        "Sample-validated recipes require a tender limit; specify --max-tenders"
                    )
            with execution_budget(config.runtime.operation_timeout_seconds, limits=requested):
                from tender_system.collection.source_runs import SourceRunService

                store = TransactionalRunStore(self.sessions, clock=self.clock, configuration=config)
                receipt = SourceRunService(
                    self.executors(revision, settings),
                    self.normalizer,
                    store,
                    self.clock,
                ).run(
                    key,
                    revision.recipe,
                    source,
                    settings,
                    requested,
                    search_keywords=tuple(
                        str(k) for k in cast(list[JsonValue], payload.get("search_keywords", []))
                    )
                    if payload.get("scheduled")
                    else (),
                )
            return {
                "run_id": str(receipt.id),
                "collection_status": "failed"
                if receipt.status != "accepted"
                else (
                    "bounded_success"
                    if requested and requested.max_tenders is not None
                    else "success"
                ),
                "coverage_status": coverage_status(receipt.health),
                "records_processed": len(receipt.tender_ids),
                "document_processing_failed": store.document_processing_failed,
                "document_outcomes": cast(JsonValue, store.document_outcomes),
                "requested_max_tenders": requested.max_tenders if requested else None,
                "http_requests": receipt.health.diagnostics.transport_attempts
                if receipt.health
                else 0,
                "duration_seconds": (receipt.finished_at - receipt.started_at).total_seconds()
                if receipt.finished_at and receipt.started_at
                else None,
                "health": receipt.health.model_dump(mode="json") if receipt.health else None,
                "publication": receipt.status,
                "evidence_reference": receipt.evidence_reference,
                "error_code": receipt.failure.error_code
                if receipt.failure
                else (
                    receipt.reasons[0] if receipt.status != "accepted" and receipt.reasons else None
                ),
                "message": receipt.failure.error_detail
                if receipt.failure
                else "; ".join(receipt.reasons)
                if receipt.status != "accepted"
                else None,
            }, "completed" if receipt.status == "accepted" else "failed"
        if kind in {"recipe_validation", "source_setup"}:
            limits = (
                ExecutionLimits.model_validate(payload["limits"]) if payload.get("limits") else None
            )
            origin_id = UUID(str(payload["operation_id"]))
            with self.sessions.begin() as session:
                origin = session.scalar(
                    select(OperationRow).where(OperationRow.id == origin_id).with_for_update()
                )
                if (
                    origin is None
                    or origin.source_id != source_id
                    or origin.result.get("manual_recipe_import") is not True
                    or origin.status not in {"awaiting_approval", "failed"}
                ):
                    raise OperationRestriction("recipe import is not available for validation")
                proposal = FixResult.model_validate(origin.result["proposal"])
                revalidation = origin.result.get("scope_revalidation") is True
                replacement_baseline = origin.result.get("replacement_baseline_version")
                origin.result = {
                    **origin.result,
                    "validation_passed": False,
                    "validation_operation_id": str(key),
                }
                origin.result.pop("validation_evidence_reference", None)
                origin.result.pop("validation_invalidated_reason", None)
                origin.status = "failed"
            result = self.test_proposal(
                key,
                source_id,
                proposal,
                settings,
                limits,
                revalidation=revalidation,
                replacement_baseline=str(replacement_baseline) if replacement_baseline else None,
            )
            with self.sessions.begin() as session:
                origin = session.scalar(
                    select(OperationRow).where(OperationRow.id == origin_id).with_for_update()
                )
                if (
                    origin is None
                    or origin.source_id != source_id
                    or origin.result.get("manual_recipe_import") is not True
                    or origin.status not in {"awaiting_approval", "failed"}
                ):
                    raise OperationRestriction("recipe import changed during validation")
                if origin.result.get("validation_operation_id") != str(key):
                    raise OperationRestriction("validation superseded by another attempt")
                current = FixResult.model_validate(origin.result["proposal"])
                current_recipe = current.updated_recipe
                result_recipe = proposal.updated_recipe
                if (
                    current_recipe is None
                    or result_recipe is None
                    or fingerprint(current_recipe.model_dump(mode="json"))
                    != fingerprint(result_recipe.model_dump(mode="json"))
                ):
                    raise OperationRestriction("recipe changed during validation")
                origin.result = {**origin.result, **result}
                origin.status = "awaiting_approval" if result["validation_passed"] else "failed"
            if kind == "source_setup" and result["validation_passed"]:
                with self.sessions.begin() as session:
                    operation = session.get(OperationRow, key)
                    assert operation is not None
                    operation.result = {**result, "stage": "activation"}
                # Reuse fresh checks and atomic version publication under the same budget.
                activated, status = self.execute(key, "activation", source_id, payload, config)
                activated["duration_seconds"] = float(str(result["duration_seconds"])) + float(
                    str(activated["duration_seconds"])
                )
                return activated, status
            return {**result, "operation_id": str(origin_id)}, (
                "completed" if result["validation_passed"] else "failed"
            )
        if kind == "recipe_smoke":
            origin_id = UUID(str(payload["operation_id"]))
            max_tenders = payload.get("max_tenders")
            if type(max_tenders) is not int or not 1 <= max_tenders <= 100:
                raise OperationRestriction("smoke tender limit must be between 1 and 100")
            with self.sessions() as session:
                origin = session.get(OperationRow, origin_id)
                if (
                    origin is None
                    or origin.source_id != source_id
                    or origin.result.get("manual_recipe_import") is not True
                    or origin.status not in {"awaiting_approval", "failed"}
                ):
                    raise OperationRestriction("recipe import is not available for smoke execution")
                proposal = FixResult.model_validate(origin.result["proposal"])
            smoke_recipe = proposal.updated_recipe
            if smoke_recipe is None or smoke_recipe.source_id != source_id:
                raise OperationRestriction("proposal changed source identity")
            execution = self.executors(RecipeRevision(recipe=smoke_recipe), settings).execute(
                smoke_recipe,
                ExecutionContext(run_id=key),
                max_observations=max_tenders,
            )
            raw = (
                execution.observations
                if isinstance(execution, BoundedCollectionResult)
                else execution.raw_observations
                if isinstance(execution, CollectionRun)
                else execution.bad_output
            )
            source = SourceRecord(id=source_id, name=source_id)
            candidates = [
                item.candidate
                for observation in raw
                if (item := self.normalizer.normalize(observation, source, settings)).candidate
                is not None
            ]
            return {
                "error_code": execution.error_code
                if isinstance(execution, SourceFailure)
                else None,
                "stage": "recipe_execution",
                "smoke_only": True,
                "full_validation": False,
                "failed_step": execution.failed_step
                if isinstance(execution, SourceFailure)
                else None,
                "validation_passed": False,
                "requested_max_tenders": max_tenders,
                "observations_collected": len(raw),
                "execution_complete": isinstance(execution, CollectionRun) and execution.complete,
                "stop_reason": execution.stop_reason
                if isinstance(execution, BoundedCollectionResult)
                else "recipe_completed_before_bound"
                if isinstance(execution, CollectionRun)
                else execution.error_code,
                "evidence_reference": execution.evidence_reference
                if isinstance(execution, (CollectionRun, BoundedCollectionResult))
                else execution.failed_evidence_reference,
                "diagnostics": execution.diagnostics.model_dump(mode="json"),
                "samples": [
                    candidate.model_dump(mode="json", exclude={"raw_data"})
                    for candidate in candidates[:5]
                ],
            }, "failed" if isinstance(execution, SourceFailure) or len(candidates) != len(
                raw
            ) else "completed"
        if kind == "activation":
            approval_started = self.clock.now()
            with self.sessions() as session:
                origin = session.get(OperationRow, UUID(str(payload["operation_id"])))
                if origin is None or origin.status != "awaiting_approval":
                    raise OperationRestriction("proposal no longer pending")
                proposal = FixResult.model_validate(origin.result["proposal"])
                url = str(origin.result["url"])
                origin_id = origin.id
                manual_import = origin.result.get(
                    "manual_recipe_import"
                ) is True and not origin.result.get("automatic_recipe")
                evidence_pointer = origin.result
            activation_recipe = proposal.updated_recipe
            if activation_recipe is None:
                raise OperationRestriction("proposal changed source identity")
            validation = self.validation_evidence(
                evidence_pointer, activation_recipe, source_id, settings
            )
            fresh_check = self.fresh_source_check(
                uuid5(key, "approval-source-check"),
                source_id,
                activation_recipe,
                settings,
                validation,
            )
            report = validation.get("report")
            if not isinstance(report, dict):
                raise OperationRestriction("validation report is missing or corrupt")
            result = dict(report)
            result["validation_http_requests"] = result.get("http_requests")
            result["validation_duration_seconds"] = result.get("duration_seconds")
            result.update(fresh_check)
            result["approval_fresh_check"] = "passed"
            remaining_seconds("approval")
            self.activate(
                source_id,
                proposal,
                url,
                settings,
                origin_id=origin_id,
                activation_id=key,
                manual_only=manual_import,
                validation_reference=str(evidence_pointer["validation_evidence_reference"]),
                validation_policy_fingerprint=str(validation["policy_fingerprint"]),
                execution_limits=validation.get("execution_limits"),
                validated_calibration=SourceCalibration.model_validate(validation["calibration"]),
            )
            approval_duration = (self.clock.now() - approval_started).total_seconds()
            timings = result.get("timings_seconds")
            result["timings_seconds"] = {
                **(timings if isinstance(timings, dict) else {}),
                "activation": approval_duration,
            }
            result["duration_seconds"] = approval_duration
            return {**result, "source_id": source_id, "manual_only": manual_import}, "activated"
        raise OperationRestriction("unsupported operation")

    def generate_source(
        self, key: UUID, source_id: str, payload: dict[str, JsonValue], config: Configuration
    ) -> tuple[dict[str, JsonValue], str]:
        from tender_system.repair.recipe_generation import RecipeGenerator

        limits = ExecutionLimits.model_validate(payload["limits"])
        with self.sessions.begin() as session:
            source_lock(session, source_id)
            draft = session.get(OperatorConfigRow, "web/source/" + source_id)
            if draft is None or session.get(ActiveRecipeRow, source_id) is not None:
                raise OperationRestriction("Automatic generation requires a new website draft")
            url = str(draft.value["url"])
            config = effective_configuration(session, self.config)
        settings = config.for_source(source_id).model_copy(update={"public_readonly": True})
        if settings.authorized_session_reference:
            raise UnsafeWebsite("Automatic onboarding supports anonymous sources without sessions")
        model_settings, client = self.generation_model(settings, config)
        generator = RecipeGenerator(
            self.public_http, self.public_browsers, client, model_settings, self.evidence
        )
        proposal = None
        feedback: JsonValue = None

        def schema_feedback(error: ValidationError) -> dict[str, JsonValue]:
            return {
                "schema_errors": [
                    {"location": str(item["loc"]), "message": item["msg"]}
                    for item in error.errors(
                        include_input=False, include_context=False, include_url=False
                    )
                ]
            }

        with (
            execution_budget(
                min(settings.investigation.timeout_seconds, limits.max_duration_seconds),
                limits=limits,
            ),
            transport_usage() as usage,
        ):
            try:
                proposal = generator.generate(key, source_id, url, settings)
            except ValidationError as error:
                feedback = schema_feedback(error)
            discovery: dict[str, JsonValue] = {
                "requests": usage.requests,
                "response_bytes": usage.response_bytes,
            }
        result: dict[str, JsonValue] = {}
        state = "failed"
        for _attempt in range(3):
            if proposal is None:
                if generator.revisions >= 2:
                    break
                try:
                    proposal = generator.revise(key, source_id, feedback)
                except ValidationError as error:
                    feedback = schema_feedback(error)
                    continue
                except Exception as error:
                    error.evidence_references = tuple(generator.references)  # type: ignore[attr-defined]
                    raise
            assert proposal.updated_recipe is not None
            with self.sessions.begin() as session:
                svc = OperatorService(session, self.config, self.clock.now())
                source_lock(session, source_id)
                control = svc.control(source_id)
                override = control.get("override")
                svc.write(
                    "source/" + source_id,
                    {
                        **control,
                        "manual_only": False,
                        "override": {
                            **(override if isinstance(override, dict) else {}),
                            "public_readonly": True,
                        },
                    },
                )
                for previous in session.scalars(
                    select(OperationRow).where(
                        OperationRow.source_id == source_id,
                        OperationRow.kind == "onboarding",
                        OperationRow.status.in_(("awaiting_approval", "failed")),
                    )
                ):
                    if previous.result.get("automatic_recipe"):
                        previous.status = "rejected"
                imported = svc.import_recipe(proposal.updated_recipe, url)
                origin = session.get(OperationRow, imported.id)
                assert origin is not None
                origin.result = {
                    **origin.result,
                    "automatic_recipe": True,
                    "proposal": proposal.model_dump(mode="json"),
                    "investigation": discovery,
                }
                current = session.get(OperationRow, key)
                if current is not None:
                    current.payload = {**current.payload, "operation_id": str(imported.id)}
                config = effective_configuration(session, self.config)
            result, state = self.execute(
                key,
                "source_setup",
                source_id,
                {"operation_id": str(imported.id), "limits": limits.model_dump(mode="json")},
                config,
            )
            if state == "activated" or result.get("stage") not in {
                "recipe_execution",
                "normalization",
            }:
                break
            if result.get("error_code") in {
                "request_budget_exceeded",
                "response_budget_exceeded",
                "operation_timeout",
            }:
                break
            feedback = {
                "stage": result.get("stage"),
                "error_code": result.get("error_code"),
                "failed_step": result.get("failed_step"),
                "normalization_errors": result.get("normalization_errors"),
            }
            proposal = None
        if not result:
            raise OperationRestriction(
                "Generated recipe remains schema-invalid after bounded revisions",
                evidence_references=tuple(generator.references),
            )
        return {
            **result,
            "investigation": discovery,
            "recipe_revisions": generator.revisions,
            "investigation_evidence_references": list(generator.references),
        }, state

    def test_proposal(
        self,
        key: UUID,
        source_id: str,
        proposal: FixResult,
        settings: SourceSettings,
        limits: ExecutionLimits | None = None,
        *,
        revalidation: bool = False,
        replacement_baseline: str | None = None,
    ) -> dict[str, JsonValue]:
        limits = effective_execution_limits(limits)
        duration = (
            limits.max_duration_seconds if limits else self.config.runtime.operation_timeout_seconds
        )
        with execution_budget(duration, limits=limits):
            result = self._test_proposal(
                key,
                source_id,
                proposal,
                settings,
                limits,
                revalidation=revalidation,
                replacement_baseline=replacement_baseline,
            )
            result.setdefault(
                "recipe_status", "valid" if result["validation_passed"] else "invalid"
            )
            return result

    def _test_proposal(
        self,
        key: UUID,
        source_id: str,
        proposal: FixResult,
        settings: SourceSettings,
        limits: ExecutionLimits | None = None,
        *,
        revalidation: bool = False,
        replacement_baseline: str | None = None,
    ) -> dict[str, JsonValue]:
        recipe = proposal.updated_recipe
        if recipe is None or recipe.source_id != source_id:
            raise OperationRestriction("proposal changed source identity")
        source = SourceRecord(id=source_id, name=source_id)
        baseline = self.validation_baseline(source_id)
        active = self.catalog.active(source_id) if revalidation else None
        baseline_allowed = baseline == {"source_exists": False, "active_recipe_version": None}
        if replacement_baseline is not None:
            baseline_allowed = baseline == {
                "source_exists": True,
                "active_recipe_version": replacement_baseline,
            }
            if not baseline_allowed:
                raise OperationRestriction("Active recipe changed; start a new revision proposal")
        if revalidation:
            baseline_allowed = bool(
                baseline.get("source_exists")
                and active is not None
                and fingerprint(active.recipe.model_dump(mode="json"))
                == fingerprint(recipe.model_dump(mode="json"))
            )
            if not baseline_allowed:
                raise OperationRestriction(
                    "scope revalidation requires the unchanged active recipe"
                )
        recipe_hash = fingerprint(recipe.model_dump(mode="json"))
        policy_hash = self.validation_policy_fingerprint(settings)
        started = perf_counter()
        execution = self.executors(RecipeRevision(recipe=recipe), settings).execute(
            recipe, ExecutionContext(run_id=key, limits=limits)
        )
        timings = {"extraction": perf_counter() - started}
        if isinstance(execution, SourceFailure):
            detail = (
                f"Recipe execution timed out at {execution.failed_step}; "
                f"inspect evidence reference {execution.failed_evidence_reference}."
                if execution.error_code == "TimeoutError"
                else (
                    f"Recipe execution failed at {execution.failed_step} "
                    f"({execution.error_code}); inspect evidence reference "
                    f"{execution.failed_evidence_reference}."
                )
            )
            return {
                "validation_passed": False,
                "error_code": execution.error_code,
                "stage": "recipe_execution",
                "timings_seconds": dict(timings),
                "recipe_status": "invalid",
                "failure_category": (
                    "http_timeout"
                    if "http_request" in (execution.failed_step or "")
                    else "browser_timeout"
                )
                if execution.error_code == "TimeoutError"
                else "recipe_execution_error",
                "replay_passed": False,
                "warnings": [detail],
                "evidence_reference": execution.failed_evidence_reference,
                "evidence_references": [execution.failed_evidence_reference],
                "failed_step": execution.failed_step,
                "diagnostics": execution.diagnostics.model_dump(mode="json"),
                "samples": [],
            }
        if limits is not None and limits.max_tenders is not None:
            execution = execution.model_copy(
                update={"coverage": "sampled", "complete": False, "checkpoint": None}
            )
        try:
            captured = self.evidence.get(execution.evidence_reference)
            if fingerprint(recipe.model_dump(mode="json")) != fingerprint(
                captured.get("recipe")
            ) or fingerprint(settings.model_dump(mode="json")) != fingerprint(
                captured.get("settings")
            ):
                raise OperationRestriction("captured execution recipe/settings changed")
            replay_adapter = CapturedResponseHttpAdapter(captured.get("http_exchanges"))
            browser_replay = CapturedBrowserSteps(captured.get("browser_steps", []))
        except (OSError, KeyError, ValueError, TypeError):
            return {
                "validation_passed": False,
                "replay_passed": False,
                "error_code": "replay_evidence_invalid",
                "stage": "offline_replay",
                "warnings": ["Captured HTTP validation evidence is missing or invalid"],
                "evidence_reference": execution.evidence_reference,
                "samples": [],
            }
        stage_started = perf_counter()
        try:
            normalized = [
                self.normalizer.normalize(raw, source, settings)
                for raw in execution.raw_observations
            ]
        except Exception as error:
            return {
                "validation_passed": False,
                "replay_passed": False,
                "error_code": "normalization_error",
                "stage": "normalization",
                "error_type": type(error).__name__,
                "evidence_reference": execution.evidence_reference,
                "warnings": ["Normalization failed; retained raw execution evidence is available."],
                "samples": [],
            }
        candidates = tuple(item.candidate for item in normalized if item.candidate is not None)
        run = execution.model_copy(
            update={
                "candidates": candidates,
                "quality_issues": tuple(issue for item in normalized for issue in item.issues),
            }
        )
        # Validation may attest the bounded incremental window without claiming site exhaustion.
        supported_terminal = recipe.completion_signal and recipe.completion_signal.kind in {
            "total_reconciled",
            "final_page",
            "end_marker",
            "next_cursor_absent",
            "empty_terminal_page",
        }
        if (
            settings.calibration.version == "unconfigured"
            or (revalidation and settings.calibration.version == "onboarding-v1")
        ) and (execution.coverage in {"incremental", "sampled"} or supported_terminal):
            previous_freshness = settings.calibration.freshness
            settings = settings.model_copy(
                update={
                    "calibration": settings.calibration.model_copy(
                        update={
                            "version": "onboarding-v1",
                            "status": "provisional",
                            "evidence_references": (execution.evidence_reference,),
                            "expected_mode": recipe.coverage
                            if isinstance(recipe.coverage, str)
                            else "either",
                            "full_inventory_supported": execution.coverage == "full"
                            and bool(supported_terminal),
                            "completion_signals": (recipe.completion_signal.kind,)
                            if supported_terminal and recipe.completion_signal
                            else (),
                            "freshness": previous_freshness.model_copy(
                                update={"unknown_policy": "observe_only"}
                            )
                            if previous_freshness.basis == "unknown"
                            and (
                                execution.coverage in {"incremental", "sampled"}
                                or supported_terminal
                            )
                            else previous_freshness,
                        }
                    )
                }
            )
        health = ConservativeRunHealth().evaluate(
            run, settings, SourceSnapshot(), (), now=self.clock.now()
        )
        timings["normalization_and_health"] = perf_counter() - stage_started
        stage_started = perf_counter()
        try:
            remaining_seconds("independent_verification")
            verifier = self.sample_verifier
            if settings.public_readonly:
                from tender_system.repair.recipe_generation import GeneratedRecipeWitnesses

                model_settings, client = self.generation_model(settings, self.config)
                verifier = GeneratedRecipeWitnesses(
                    self.public_browsers,
                    client,
                    model_settings,
                    self.normalizer,
                    self.evidence,
                    BrowserWitnesses(self.public_browsers, self.evidence),
                )
            samples = verifier.verify(run, source, settings)
            remaining_seconds("offline_replay")
        except ExecutionDeadlineExceeded as error:
            error.evidence_references = (execution.evidence_reference,)
            raise
        timings["independent_verification"] = perf_counter() - stage_started
        stage_started = perf_counter()
        identities = [candidate.external_tender_id for candidate in candidates]
        try:
            replay = self.executors(RecipeRevision(recipe=recipe), settings).execute(
                recipe,
                ExecutionContext(run_id=uuid5(key, "onboarding-replay"), limits=limits),
                http_adapter=replay_adapter,
                browser_replay=browser_replay,
            )
            replay_adapter.assert_exhausted()
            browser_replay.assert_exhausted()
        except (OSError, KeyError, ValueError, TypeError):
            replay = None
        replay_passed = False
        if isinstance(replay, CollectionRun):
            if limits is not None and limits.max_tenders is not None:
                replay = replay.model_copy(
                    update={"coverage": "sampled", "complete": False, "checkpoint": None}
                )
            repeated = tuple(
                item.candidate
                for raw in replay.raw_observations
                if (item := self.normalizer.normalize(raw, source, settings)).candidate is not None
            )

            def states(items: tuple[TenderCandidate, ...]) -> list[str]:
                return sorted(
                    json.dumps(
                        {
                            "identity": identity_of(item).model_dump(mode="json"),
                            "state": comparison_state_of(
                                item, settings.normalization, settings.field_change_policy
                            ),
                        },
                        sort_keys=True,
                    )
                    for item in items
                )

            replay_health = ConservativeRunHealth().evaluate(
                replay.model_copy(update={"candidates": repeated}),
                settings,
                SourceSnapshot(),
                (),
                now=self.clock.now(),
            )
            replay_passed = (
                states(candidates) == states(repeated)
                and replay_health.publication_allowed
                and (replay.coverage == "sampled" or replay_health.completeness == "complete")
            )
        timings["offline_replay"] = perf_counter() - stage_started
        passed = (
            bool(candidates)
            and baseline_allowed
            and baseline == self.validation_baseline(source_id)
            and health.publication_allowed
            and (run.coverage == "sampled" or health.completeness == "complete")
            and all(identities)
            and len(set(identities)) == len(identities)
            and not samples.mismatches
            and replay_passed
            and (
                not settings.repair_checks.require_samples
                or samples.checked >= min(settings.repair_checks.sample_size, len(candidates))
            )
        )
        approval_witnesses = [
            self.candidate_state(candidate, settings) for candidate in candidates[:3]
        ]
        inventory_authority = (
            run.coverage == "full"
            and health.publication_allowed
            and health.completeness == "complete"
            and health.malformed_count == 0
            and health.duplicate_count == 0
            and not any(
                reason.code in {"pagination_consistency_unproven", "inventory_total_unobserved"}
                for reason in health.reasons
            )
        )
        report = document(
            {
                "validation_passed": passed,
                "recipe_status": "valid" if passed else "invalid",
                "coverage_status": coverage_status(health),
                "timings_seconds": timings,
                "verification": {
                    "checked": samples.checked,
                    "required": min(settings.repair_checks.sample_size, len(candidates))
                    if settings.repair_checks.require_samples
                    else 0,
                    "issues": [reason.model_dump(mode="json") for reason in samples.mismatches],
                    "evidence_references": list(samples.evidence_references),
                },
                "error_code": None
                if passed
                else "source_health_restriction"
                if not health.publication_allowed
                else "independent_verification_failed"
                if samples.mismatches
                or (
                    settings.repair_checks.require_samples
                    and samples.checked < min(settings.repair_checks.sample_size, len(candidates))
                )
                else "replay_mismatch"
                if not replay_passed
                else "validation_restricted",
                "stage": "normalization" if health.malformed_count else "validation",
                # Engine diagnostics only; independent witness values never feed repair.
                "normalization_errors": [
                    {"field": issue.field, "code": issue.code}
                    for item in normalized
                    for issue in item.issues
                    if issue.severity == "error"
                ],
                "execution_limits": limits.model_dump(mode="json") if limits else None,
                "validation_scope": run.coverage,
                "inventory_authority": inventory_authority,
                "observed_count": len(run.raw_observations),
                "inventory_snapshots": list(run.diagnostics.inventory_snapshots),
                "source_global_count": run.diagnostics.inventory_totals[-1]
                if run.diagnostics.inventory_totals
                else None,
                "observations_collected": len(run.raw_observations),
                "http_requests": run.diagnostics.transport_attempts,
                "duration_seconds": (self.clock.now() - run.started_at).total_seconds(),
                "replay_passed": replay_passed,
                "expected_count": execution.expected_count,
                "provisional_calibration": settings.calibration.model_dump(mode="json"),
                "health": health.model_dump(mode="json"),
                "fields": sorted({field for raw in execution.raw_observations for field in raw}),
                "ids": identities[:10],
                "approval_witnesses": approval_witnesses,
                "pagination": recipe.completion_signal.model_dump(mode="json")
                if recipe.completion_signal
                else None,
                "completeness_confidence": "verified extraction and independent samples"
                if passed
                else "insufficient evidence",
                "samples": [
                    candidate.model_dump(mode="json", exclude={"raw_data"})
                    for candidate in candidates[:5]
                ],
                "warnings": [reason.detail for reason in samples.mismatches]
                + [
                    reason.detail
                    for reason in health.reasons
                    if reason.severity in {"warning", "error"}
                ]
                + (["Offline replay mismatch"] if not replay_passed else [])
                + (
                    ["No independent detail witness; activation blocked"]
                    if settings.repair_checks.require_samples and samples.checked == 0
                    else []
                ),
                "evidence_reference": execution.evidence_reference,
                "replay_evidence_reference": replay.evidence_reference
                if isinstance(replay, CollectionRun)
                else None,
                "sample_evidence": list(samples.evidence_references),
                "model": {
                    "provider": settings.fixer_model.provider,
                    "model": settings.fixer_model.model,
                },
            }
        )
        validation_record = {
            "schema_version": 3,
            "validation_scope": run.coverage,
            "inventory_authority": inventory_authority,
            "observed_count": len(run.raw_observations),
            "inventory_snapshots": list(run.diagnostics.inventory_snapshots),
            "source_global_count": run.diagnostics.inventory_totals[-1]
            if run.diagnostics.inventory_totals
            else None,
            "execution_limits": limits.model_dump(mode="json") if limits else None,
            "calibration": settings.calibration.model_dump(mode="json"),
            "kind": "recipe_validation",
            "smoke_only": False,
            "created_at": self.clock.now().astimezone(UTC).isoformat(),
            "recipe_fingerprint": recipe_hash,
            "policy_fingerprint": policy_hash,
            "baseline": baseline,
            "execution_evidence_reference": execution.evidence_reference,
            "replay_evidence_reference": report.get("replay_evidence_reference"),
            "validation_passed": passed,
            "expected_count": execution.expected_count,
            "approval_witnesses": approval_witnesses,
            "report": report,
        }
        reference = self.evidence.put(
            uuid5(key, "full-recipe-validation"),
            cast(dict[str, JsonValue], validation_record),
        )
        return {
            **report,
            "validation_evidence_reference": reference,
            "validated_recipe_fingerprint": recipe_hash,
        }

    def activate(
        self,
        source_id: str,
        proposal: FixResult,
        url: str,
        settings: SourceSettings,
        *,
        origin_id: UUID | None = None,
        activation_id: UUID | None = None,
        manual_only: bool = False,
        validation_reference: str | None = None,
        validation_policy_fingerprint: str | None = None,
        execution_limits: JsonValue = None,
        validated_calibration: SourceCalibration | None = None,
    ) -> None:
        if not proposal.updated_recipe:
            raise OperationRestriction("recipe required")
        remaining_seconds("activation")
        revision = RecipeRevision(recipe=proposal.updated_recipe)
        with self.sessions.begin() as session:
            source_lock(session, source_id)
            # Source configuration uses this row lock; share it before checking
            # effective policy or reading controls for an existing-source upgrade.
            existing_source = session.scalar(
                select(SourceRow).where(SourceRow.id == source_id).with_for_update()
            )
            remaining_seconds("activation")
            fence_execution()
            scope_revalidation = False
            replacement_baseline: JsonValue = None
            if origin_id:
                origin = session.scalar(
                    select(OperationRow).where(OperationRow.id == origin_id).with_for_update()
                )
                if origin is None or origin.status != "awaiting_approval":
                    raise OperationRestriction("proposal no longer pending")
                scope_revalidation = origin.result.get("scope_revalidation") is True
                replacement_baseline = origin.result.get("replacement_baseline_version")
                if (
                    origin.result.get("validation_evidence_reference") != validation_reference
                    or validation_reference is None
                    or validation_policy_fingerprint != self.validation_policy_fingerprint(settings)
                    or fingerprint(proposal.updated_recipe.model_dump(mode="json"))
                    != origin.result.get("validated_recipe_fingerprint")
                    or origin.result.get("execution_limits") != execution_limits
                    or self.validation_policy_fingerprint(
                        self.source_settings(
                            effective_configuration(session, self.config), source_id
                        )
                    )
                    != validation_policy_fingerprint
                ):
                    raise OperationRestriction("validated proposal changed before activation")
                # A full fresh fallback can outlast the evidence freshness window.
                # Recheck the binding at the atomic publication boundary.
                self.validation_evidence(
                    origin.result, proposal.updated_recipe, source_id, settings
                )
                origin.status = "activated"
                origin.result = {
                    **origin.result,
                    "manual_only": manual_only,
                    "activation_operation_id": str(activation_id) if activation_id else None,
                }
            existing = existing_source is not None
            if existing:
                active = session.get(ActiveRecipeRow, source_id)
                current = (
                    session.get(RecipeRevisionRow, (source_id, active.version)) if active else None
                )
                if replacement_baseline is not None:
                    if active is None or active.version != replacement_baseline:
                        raise OperationRestriction("Active recipe changed before revision approval")
                    if (
                        session.get(RecipeRevisionRow, (source_id, revision.recipe.version))
                        is not None
                    ):
                        raise OperationRestriction(
                            "Recipe version already exists; create a new revision"
                        )
                    session.add(
                        RecipeRevisionRow(
                            source_id=source_id,
                            version=revision.recipe.version,
                            revision=revision.model_dump(mode="json"),
                            created_at=self.clock.now(),
                        )
                    )
                    session.flush()
                    active.version = revision.recipe.version
                elif (
                    not scope_revalidation
                    or current is None
                    or fingerprint(
                        RecipeRevision.model_validate(current.revision).recipe.model_dump(
                            mode="json"
                        )
                    )
                    != fingerprint(proposal.updated_recipe.model_dump(mode="json"))
                ):
                    raise OperationRestriction(
                        "active recipe changed before collection scope approval"
                    )
            elif scope_revalidation or replacement_baseline is not None:
                raise OperationRestriction("source removed before collection scope approval")
            else:
                SqlSourceRepository(session).add(
                    SourceRecord(id=source_id, name=urlsplit_name(url))
                )
                session.add(
                    RecipeRevisionRow(
                        source_id=source_id,
                        version=revision.recipe.version,
                        revision=revision.model_dump(mode="json"),
                        created_at=self.clock.now(),
                    )
                )
                session.flush()
                session.add(ActiveRecipeRow(source_id=source_id, version=revision.recipe.version))
            svc = OperatorService(
                session,
                self.config,
                self.clock.now(),
            )
            recipe = proposal.updated_recipe
            calibration = validated_calibration or SourceCalibration(
                version="onboarding-v1",
                status="provisional",
                evidence_references=proposal.investigation_evidence_references
                or ("operator-approved-onboarding",),
                full_inventory_supported=recipe.coverage == "full"
                and recipe.completion_signal is not None
                and (
                    not execution_limits
                    or not isinstance(execution_limits, dict)
                    or execution_limits.get("max_tenders") is None
                ),
                expected_mode=recipe.coverage if isinstance(recipe.coverage, str) else "either",
                completion_signals=(recipe.completion_signal.kind,)
                if recipe.completion_signal
                else (),
                freshness=settings.calibration.freshness.model_copy(
                    update={"unknown_policy": "observe_only"}
                )
                if settings.calibration.freshness.basis == "unknown"
                else settings.calibration.freshness,
            )
            control = svc.control(source_id)
            override = control.get("override")
            svc.write(
                f"source/{source_id}",
                {
                    **control,
                    "enabled": control.get("enabled", not manual_only),
                    "approved_execution_limits": execution_limits,
                    "manual_only": control.get("manual_only", manual_only),
                    "override": {
                        **(override if isinstance(override, dict) else {}),
                        "calibration": calibration.model_dump(mode="json"),
                    },
                },
            )

    def register_catalog(self, root: Path) -> None:
        for path in sorted(root.glob("*/source.json")):
            source = SourceRecord.model_validate_json(path.read_bytes())
            recipe = SourceRecipe.model_validate_json(path.with_name("recipe.json").read_bytes())
            with self.sessions.begin() as session:
                if not session.get(SourceRow, source.id):
                    SqlSourceRepository(session).add(source)
                if not session.get(RecipeRevisionRow, (source.id, recipe.version)):
                    revision = RecipeRevision(recipe=recipe)
                    session.add(
                        RecipeRevisionRow(
                            source_id=source.id,
                            version=recipe.version,
                            revision=revision.model_dump(mode="json"),
                            created_at=self.clock.now(),
                        )
                    )
                    session.flush()
                if not session.get(ActiveRecipeRow, source.id):
                    session.add(ActiveRecipeRow(source_id=source.id, version=recipe.version))
                if not session.get(OperatorConfigRow, f"source/{source.id}"):
                    settings_path = path.with_name("config.toml")
                    local = (
                        load_configuration(settings_path) if settings_path.exists() else self.config
                    )
                    override = merge(
                        local.global_settings.source.model_dump(mode="json", exclude_unset=True),
                        local.sources.get(source.id, SourceOverride()).model_dump(
                            mode="json", exclude_unset=True
                        ),
                    )
                    svc = OperatorService(
                        session,
                        self.config,
                        self.clock.now(),
                    )
                    svc.write(
                        f"source/{source.id}",
                        {
                            "override": cast(JsonValue, override),
                            "enabled": True,
                            "manual_only": True,
                        },
                    )


def urlsplit_name(url: str) -> str:

    return urlsplit(url).hostname or "New source"
