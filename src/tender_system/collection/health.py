"""Source-declared evidence semantics. No storage, adapter or model dependencies."""

from datetime import UTC, datetime

from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    CollectionRun,
    FreshnessDecision,
    HealthDecision,
    HealthDimension,
    HealthReason,
    SourceFailure,
    SourceSnapshot,
    Tender,
)
from tender_system.core.tenders import SemanticTenderComparator, identity_of


def evidence_freshness(evidence: FreshnessDecision, now: datetime) -> FreshnessDecision:
    """Age stored evidence using its declared semantics, never a newer source run."""
    age = None if evidence.timestamp is None else (now - evidence.timestamp).total_seconds()
    status = "unknown"
    if age is not None and evidence.max_age_seconds is not None:
        if age < 0 or age > evidence.max_age_seconds:
            status = "stale"
        elif evidence.aging_after_seconds is not None and age >= evidence.aging_after_seconds:
            status = "aging"
        else:
            status = "fresh"
    return FreshnessDecision.model_validate(
        {**evidence.model_dump(), "status": status, "age_seconds": age, "evaluated_at": now}
    )


def freshness(
    timestamp: datetime | None,
    settings: SourceSettings,
    observed_at: datetime | None,
    now: datetime,
    reference: str | None = None,
) -> FreshnessDecision:
    policy = settings.calibration.freshness
    age = (
        None
        if timestamp is None
        else (now.astimezone(UTC) - timestamp.astimezone(UTC)).total_seconds()
    )
    status = "unknown"
    if age is not None:
        if age < 0 or age > policy.max_age_seconds:
            status = "stale"
        elif policy.aging_after_seconds is not None and age >= policy.aging_after_seconds:
            status = "aging"
        else:
            status = "fresh"
    return FreshnessDecision.model_validate(
        dict(
            status=status,
            basis=policy.basis,
            timestamp=timestamp,
            observed_at=observed_at,
            age_seconds=age,
            evidence_reference=reference,
            evaluated_at=now,
            max_age_seconds=policy.max_age_seconds,
            aging_after_seconds=policy.aging_after_seconds,
            timestamp_meaning=policy.timestamp_meaning,
        )
    )


def source_freshness(
    snapshot: SourceSnapshot, settings: SourceSettings, now: datetime
) -> dict[str, FreshnessDecision]:
    full_at = (
        snapshot.last_full_freshness_at
        if (
            snapshot.last_full_policy == settings.calibration.version
            and snapshot.last_full_freshness_basis == settings.calibration.freshness.basis
        )
        else None
    )
    return {
        "full_inventory": freshness(
            full_at,
            settings,
            snapshot.last_full_verified_at,
            now,
            snapshot.last_full_evidence_reference,
        ),
        "incremental_liveness": freshness(
            snapshot.last_incremental_at, settings, snapshot.last_incremental_at, now
        ).model_copy(update={"basis": "observed"}),
    }


class ConservativeRunHealth:
    def evaluate(
        self,
        run: CollectionRun,
        settings: SourceSettings,
        snapshot: SourceSnapshot,
        previous: tuple[Tender, ...],
        *,
        now: datetime | None = None,
    ) -> HealthDecision:
        now = run.finished_at if now is None else now
        policy = settings.calibration
        reasons: list[HealthReason] = []
        recorded: set[tuple[str, str, str]] = set()
        dimensions: dict[str, str] = {
            name: "pass"
            for name in (
                "transport",
                "session",
                "extraction",
                "inventory",
                "pagination",
                "normalization",
                "content",
                "volume",
                "freshness",
                "publication",
            )
        }
        if not run.diagnostics.transport_attempts and not run.diagnostics.pages:
            dimensions["transport"] = "not_applicable"
        if not policy.session_expiry_signatures and not any(
            s.kind == "session_expired" for s in run.diagnostics.content_signals
        ):
            dimensions["session"] = "not_applicable"
        if not policy.minimum_pages and not run.diagnostics.pages:
            dimensions["pagination"] = "not_applicable"

        def reason(
            code: str,
            dimension: str,
            detail: str,
            severity: str = "error",
            observed: object = None,
            threshold: object = None,
        ) -> None:
            key = (code, dimension, severity)
            if key in recorded:
                return
            recorded.add(key)
            reasons.append(
                HealthReason.model_validate(
                    dict(
                        code=code,
                        dimension=dimension,
                        detail=detail,
                        severity=severity,
                        observed=observed,
                        threshold=threshold,
                        evidence_reference=run.evidence_reference,
                    )
                )
            )
            if severity == "error":
                dimensions[dimension] = "fail"
            elif severity == "warning" and dimensions[dimension] == "pass":
                dimensions[dimension] = "warning"

        raw = len(run.raw_observations)
        count = len(run.candidates)
        malformed = max(0, raw - count)
        if count > raw:
            reason(
                "normalization_count_invalid",
                "normalization",
                "Normalized items exceed retained observations.",
            )
        identities: dict[str, Tender | object] = {}
        duplicates = 0
        conflicting = False
        for candidate in run.candidates:
            key = identity_of(candidate).model_dump_json()
            if key in identities:
                duplicates += 1
                if identities[key] != candidate:
                    conflicting = True
            identities[key] = candidate
            if candidate.source_id != run.source_id:
                reason("wrong_source", "normalization", "Candidate belongs to another source.")
            if any(
                getattr(candidate, field) in (None, "", ())
                for field in settings.health.required_fields
            ):
                reason(
                    "missing_required_fields", "normalization", "Required canonical field missing."
                )
            if any(
                all(getattr(candidate, field) in (None, "", ()) for field in group)
                for group in settings.health.required_any_of
            ):
                reason(
                    "missing_required_content",
                    "normalization",
                    "No canonical field is present in a required alternative group.",
                )
            if candidate.quality_issues:
                reason(
                    "data_quality_issues",
                    "normalization",
                    "Canonical values contain observable parse failures.",
                    "error" if settings.health.reject_quality_issues else "warning",
                )
        malformed_ratio = malformed / raw if raw else 0
        duplicate_ratio = duplicates / count if count else 0
        if malformed:
            reason(
                "normalization_failed",
                "normalization",
                "Malformed observations were retained in raw evidence.",
                "error" if malformed_ratio > policy.maximum_malformed_ratio else "warning",
                malformed_ratio,
                policy.maximum_malformed_ratio,
            )
        if duplicates:
            reason(
                "duplicate_identity",
                "normalization",
                "Repeated identities in this inventory.",
                "error"
                if conflicting or duplicate_ratio > policy.maximum_duplicate_ratio
                else "warning",
                duplicate_ratio,
                policy.maximum_duplicate_ratio,
            )
        if count < settings.health.minimum_count:
            reason(
                "below_minimum_count",
                "volume",
                "Below source-configured viable count.",
                observed=count,
                threshold=settings.health.minimum_count,
            )
        if (
            run.coverage != "sampled"
            and run.expected_count is not None
            and run.expected_count != raw
        ):
            reason(
                "expected_count_mismatch",
                "inventory",
                "Source total does not reconcile with raw observations.",
                observed=raw,
                threshold=run.expected_count,
            )
        if (
            run.coverage != "sampled"
            and policy.require_reported_total
            and run.expected_count is None
        ):
            reason(
                "reported_total_missing",
                "inventory",
                "Source contract requires a reconciled reported total.",
            )
        if (
            run.coverage == "full"
            and snapshot.accepted_full_runs > 0
            and snapshot.last_full_count
            and count
            and count / snapshot.last_full_count < settings.health.minimum_retained_ratio
        ):
            reason(
                "suspicious_count_drop",
                "volume",
                "Inventory collapsed relative to previous verified full inventory.",
                observed=count / snapshot.last_full_count,
                threshold=settings.health.minimum_retained_ratio,
            )
        if (
            run.coverage != "sampled"
            and policy.expected_mode != "either"
            and policy.expected_mode != run.coverage
        ):
            reason(
                "unexpected_inventory_mode",
                "inventory",
                "Recipe coverage differs from source policy.",
            )
        if run.coverage != "sampled" and len(run.diagnostics.pages) < policy.minimum_pages:
            reason(
                "insufficient_pages",
                "pagination",
                "Inventory traversal below source-declared page minimum.",
                observed=len(run.diagnostics.pages),
                threshold=policy.minimum_pages,
            )
        if len(set(run.diagnostics.pages)) != len(run.diagnostics.pages):
            reason(
                "repeated_page_or_cursor",
                "pagination",
                "An inventory page/cursor repeated; exhaustion is unproven.",
            )
        if len(set(run.diagnostics.inventory_totals)) > 1:
            reason(
                "inventory_total_changed",
                "inventory",
                "Reported source totals changed during traversal; inventory reconciliation "
                "is unreliable. Retry a stable snapshot or use an explicit bounded sample.",
                "error" if run.coverage == "full" else "warning",
                list(run.diagnostics.inventory_totals),
            )
        if len(set(run.diagnostics.inventory_snapshots)) > 1:
            reason(
                "inventory_snapshot_changed",
                "inventory",
                "Source snapshot changed between inventory pages; retry a consistent snapshot.",
                "error" if run.coverage == "full" else "warning",
            )
        if len(run.diagnostics.pages) > 1 and len(run.diagnostics.inventory_snapshots) != len(
            run.diagnostics.pages
        ):
            reason(
                "pagination_consistency_unproven",
                "pagination",
                "Multipage traversal has no source snapshot token. Stable reported totals "
                "do not prove a consistent mutable inventory; absence is disabled.",
                "warning",
            )
        if (
            len(run.diagnostics.pages) > 1
            and run.expected_count is not None
            and len(run.diagnostics.inventory_totals) != len(run.diagnostics.pages)
        ):
            reason(
                "inventory_total_unobserved",
                "inventory",
                "Declared count was not observed on every inventory page; delayed scope counts "
                "cannot prove stable source totals. Declare inventory_total at the source boundary "
                "or retain non-authoritative publication with absence disabled.",
                "warning",
            )
        for content_signal in run.diagnostics.content_signals:
            if content_signal.kind != "valid_empty":
                reason(
                    "session_expired_signature"
                    if content_signal.kind == "session_expired"
                    else "maintenance_signature",
                    "session" if content_signal.kind == "session_expired" else "content",
                    f"Source signature {content_signal.signature!r} "
                    f"observed at {content_signal.reference}.",
                )
        prior = {identity_of(tender).model_dump_json(): tender for tender in previous}
        comparator = SemanticTenderComparator(settings.normalization, settings.field_change_policy)
        changes: dict[str, int] = {}
        sample = 0
        for candidate in run.candidates:
            stored = prior.get(identity_of(candidate).model_dump_json())
            if stored is not None:
                sample += 1
                for field in comparator.compare(stored, candidate, ()).changed_fields:
                    # Lifecycle transitions can legitimately affect an entire old batch.
                    if field != "status":
                        changes[field] = changes.get(field, 0) + 1
            text = " ".join(
                value for value in (candidate.title, candidate.description) if value
            ).casefold()
            if any(
                pattern.casefold() in text for pattern in settings.health.forbidden_text_patterns
            ):
                reason(
                    "suspected_login_content", "session", "Canonical content contains a login gate."
                )
        threshold = settings.health.maximum_changed_ratio
        if (
            threshold is not None
            and sample >= settings.health.minimum_change_sample
            and any(n / sample >= threshold for n in changes.values())
        ):
            reason(
                "suspicious_mass_field_change",
                "volume",
                "A field changed across an implausible share of sampled identities.",
                observed=max(changes.values()) / sample,
                threshold=threshold,
            )

        completeness = "unknown"
        signal = run.diagnostics.completion
        if run.coverage == "sampled":
            completeness = "incomplete"
            dimensions["inventory"] = "unknown"
            reason(
                "sampled_inventory",
                "inventory",
                "Operator bounded sample; full inventory and absence are unproven.",
                "info",
            )
        elif not run.complete:
            completeness = "incomplete"
            reason(
                "incomplete_run",
                "inventory",
                "Execution explicitly declared an incomplete traversal.",
            )
        elif run.coverage == "incremental":
            if (run.expected_count is not None and run.expected_count == raw) or (
                signal is not None and signal.observed and signal.kind in policy.completion_signals
            ):
                completeness = "complete"
                dimensions["inventory"] = "unknown"
                reason(
                    "incremental_window_complete",
                    "inventory",
                    "The incremental window reconciles or has an authorized terminal signal; "
                    "website exhaustion is unclaimed.",
                    "info",
                    raw,
                    run.expected_count,
                )
            else:
                reason(
                    "incremental_window_unreconciled",
                    "inventory",
                    "Incremental publication requires the selected window to reconcile.",
                    observed=raw,
                    threshold=run.expected_count,
                )
                dimensions["inventory"] = "unknown"
        elif signal is None or signal.kind not in policy.completion_signals:
            reason(
                "completion_signal_unproven",
                "inventory",
                "No registered positive source exhaustion signal.",
            )
            dimensions["inventory"] = "unknown"
        elif not signal.observed:
            completeness = "incomplete"
            reason(
                "completion_signal_not_observed",
                "inventory",
                "Registered terminal condition did not hold.",
            )
        elif run.coverage == "full" and not policy.full_inventory_supported:
            reason(
                "full_inventory_unsupported",
                "inventory",
                "Source contract cannot prove full inventory coverage.",
            )
        else:
            completeness = "complete"
            reason(
                "full_inventory_complete" if run.coverage == "full" else "incremental_complete",
                "inventory",
                f"Positive {signal.kind} signal at {signal.reference}.",
                "info",
                signal.value,
            )

        empty = "not_empty"
        if not count:
            signed_empty = policy.empty_policy == "signature" and any(
                s.kind == "valid_empty" for s in run.diagnostics.content_signals
            )
            reported_empty = policy.empty_policy == "reported_zero" and run.expected_count == 0
            if (
                raw == 0
                and completeness == "complete"
                and settings.health.allow_empty
                and not any(s.kind != "valid_empty" for s in run.diagnostics.content_signals)
                and (signed_empty or reported_empty)
            ):
                empty = "valid_empty"
                reason(
                    "valid_empty_terminal_state",
                    "inventory",
                    "Explicit source empty evidence and positive completion agree.",
                    "info",
                )
            else:
                empty = (
                    "suspicious_empty"
                    if raw or any(s.kind != "valid_empty" for s in run.diagnostics.content_signals)
                    else "unknown_empty"
                )
                reason(
                    "unexpected_empty_run",
                    "extraction",
                    "Zero parsed items lack trusted source empty evidence.",
                )

        basis = policy.freshness.basis
        timestamp = (
            run.finished_at
            if basis == "observed"
            else run.data_as_of
            if basis == "inventory_timestamp"
            else None
        )
        fresh = freshness(timestamp, settings, run.finished_at, now, run.evidence_reference)
        if fresh.status == "unknown":
            reason(
                "missing_freshness_evidence",
                "freshness",
                "Source contract establishes no usable freshness timestamp.",
                "error" if policy.freshness.unknown_policy == "block" else "warning",
            )
        elif fresh.status == "stale":
            reason(
                "stale_or_future_source_data",
                "freshness",
                "Evidence timestamp lies outside its source freshness window.",
                observed=fresh.age_seconds,
                threshold=policy.freshness.max_age_seconds,
            )
        elif fresh.status == "aging":
            reason(
                "source_data_aging",
                "freshness",
                "Evidence is aging; publication may proceed but absence is forbidden.",
                "warning",
                fresh.age_seconds,
                policy.freshness.aging_after_seconds,
            )
        if (
            basis == "inventory_timestamp"
            and run.data_as_of is not None
            and snapshot.last_source_timestamp is not None
            and run.data_as_of <= snapshot.last_source_timestamp
        ):
            reason(
                "source_timestamp_not_advanced",
                "freshness",
                "Previously accepted source timestamp replay cannot create new evidence.",
            )
        # Legacy max-age policy remains a stricter gate, never upgrades timestamp semantics.
        if settings.health.max_data_age_seconds is not None:
            if run.data_as_of is None:
                reason(
                    "missing_freshness_evidence",
                    "freshness",
                    "Legacy source age gate requires a timestamp.",
                )
            elif (
                not 0
                <= (now - run.data_as_of).total_seconds()
                <= settings.health.max_data_age_seconds
            ):
                reason(
                    "stale_or_future_source_data",
                    "freshness",
                    "Legacy configured source age gate exceeded.",
                )
        if snapshot.accepted_full_runs == 0:
            reason(
                "provisional_cold_start",
                "volume",
                "No verified full baseline; source structural invariants govern cold start.",
                "info",
            )
        errors = [r for r in reasons if r.severity == "error"]
        warnings = [r for r in reasons if r.severity == "warning"]
        status = "quarantined" if errors else "degraded" if warnings else "healthy"
        if (
            errors
            and completeness == "unknown"
            and all(
                r.code
                in {
                    "completion_signal_unproven",
                    "full_inventory_unsupported",
                    "missing_freshness_evidence",
                }
                for r in errors
            )
        ):
            status = "unknown"
        dimensions["publication"] = "fail" if errors else "pass"
        allowed = not errors
        absence = (
            allowed
            and not warnings
            and run.complete
            and completeness == "complete"
            and run.coverage == "full"
            and policy.full_inventory_supported
            and not malformed
            and not duplicates
            and fresh.status == "fresh"
        )
        if not absence:
            reason(
                "absence_not_authorized",
                "inventory",
                "Absence requires healthy, fresh, complete full inventory "
                "without lost observations.",
                "info",
            )
        return HealthDecision.model_validate(
            dict(
                source_id=run.source_id,
                run_id=run.id,
                policy=policy.version,
                calibration=policy.status,
                overall_status=status,
                dimensions=dimensions,
                completeness=completeness,
                inventory_mode=run.coverage,
                empty_classification=empty,
                freshness=fresh,
                publication_allowed=allowed,
                absence_evidence_allowed=absence,
                raw_count=raw,
                normalized_count=count,
                malformed_count=malformed,
                duplicate_count=duplicates,
                diagnostics=run.diagnostics,
                reasons=tuple(reasons),
            )
        )


def block_decision(decision: HealthDecision, code: str, detail: str) -> HealthDecision:
    return decision.model_copy(
        update={
            "overall_status": "rejected",
            "publication_allowed": False,
            "absence_evidence_allowed": False,
            "dimensions": {**decision.dimensions, "publication": "fail"},
            "reasons": (
                *decision.reasons,
                HealthReason(code=code, dimension="publication", detail=detail, severity="error"),
            ),
        }
    )


def failure_health(
    failure: SourceFailure, settings: SourceSettings, now: datetime
) -> HealthDecision:
    dimension = (
        "transport"
        if failure.failed_step and "http_request" in failure.failed_step
        else "extraction"
    )
    reasons = [
        HealthReason(
            code=failure.error_code,
            dimension=dimension,
            detail=failure.error_detail,
            severity="error",
            evidence_reference=failure.failed_evidence_reference,
        )
    ]
    dimensions: dict[str, HealthDimension] = {
        dimension: "fail",
        "inventory": "fail",
        "publication": "fail",
    }
    for signal in failure.diagnostics.content_signals:
        if signal.kind == "valid_empty":
            continue
        signal_dimension = "session" if signal.kind == "session_expired" else "content"
        dimensions[signal_dimension] = "fail"
        reasons.append(
            HealthReason(
                code="session_expired_signature"
                if signal.kind == "session_expired"
                else "maintenance_signature",
                dimension=signal_dimension,
                severity="error",
                detail=f"Source signature {signal.signature!r} observed at {signal.reference}.",
                evidence_reference=failure.failed_evidence_reference,
            )
        )
    return HealthDecision(
        source_id=failure.source_id,
        run_id=failure.run_id,
        policy=settings.calibration.version,
        calibration=settings.calibration.status,
        overall_status="rejected",
        dimensions=dimensions,
        completeness="incomplete",
        inventory_mode=failure.recipe.coverage
        if isinstance(failure.recipe.coverage, str)
        else "unknown",
        empty_classification="suspicious_empty"
        if not failure.diagnostics.raw_count
        else "not_empty",
        freshness=freshness(None, settings, now, now, failure.failed_evidence_reference),
        publication_allowed=False,
        absence_evidence_allowed=False,
        raw_count=failure.diagnostics.raw_count,
        normalized_count=0,
        malformed_count=0,
        duplicate_count=0,
        diagnostics=failure.diagnostics,
        reasons=tuple(reasons),
    )
