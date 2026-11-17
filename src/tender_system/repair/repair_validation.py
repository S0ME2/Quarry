"""Independent dry-run validation. No model claims, canonical writes or activation access."""

import json
from collections.abc import Callable, Mapping
from typing import cast
from uuid import UUID, uuid5

from bs4 import BeautifulSoup
from pydantic import JsonValue

from tender_system.collection.health import ConservativeRunHealth
from tender_system.collection.recipes import RecipeExecutor
from tender_system.collection.validation_evidence import (
    CapturedBrowserSteps,
    CapturedResponseHttpAdapter,
)
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    BoundedCollectionResult,
    CollectionRun,
    DataQualityIssue,
    ExecutionContext,
    FixResult,
    Reason,
    RecipeRevision,
    RepairValidation,
    SampleVerification,
    SourceFailure,
    SourceRecord,
    TenderCandidate,
)
from tender_system.core.ports import (
    BrowserAdapter,
    Clock,
    EvidenceStore,
    HttpAdapter,
    RepairEnvironment,
    SampleVerifier,
    TenderNormalizer,
)
from tender_system.core.tenders import MEANINGFUL_FIELDS, comparison_state_of, identity_of, state_of


def dry_run(
    environment: RepairEnvironment,
    normalizer: TenderNormalizer,
    revision: RecipeRevision,
    failure: SourceFailure,
    source: SourceRecord,
    settings: SourceSettings,
    run_id: UUID,
    http_adapter: HttpAdapter | None = None,
    browser_replay: CapturedBrowserSteps | None = None,
) -> CollectionRun | SourceFailure:
    snapshot, _ = environment.baseline(source.id)
    executor = cast(RecipeExecutor, environment.executor(revision, settings))
    result = executor.execute(
        revision.recipe,
        ExecutionContext(run_id=run_id, values={"checkpoint": snapshot.checkpoint}),
        http_adapter=http_adapter,
        browser_replay=browser_replay,
    )
    if isinstance(result, SourceFailure):
        return result
    if (
        result.id != run_id
        or result.source_id != source.id
        or result.recipe_version != revision.recipe.version
    ):
        raise ValueError("repair executor changed run identity")
    candidates: list[TenderCandidate] = []
    issues: list[DataQualityIssue] = []
    for raw in result.raw_observations:
        normalized = normalizer.normalize(raw, source, settings)
        issues.extend(normalized.issues)
        if normalized.candidate is not None:
            candidates.append(normalized.candidate)
    return result.model_copy(
        update={"candidates": tuple(candidates), "quality_issues": tuple(issues)}
    )


class BrowserFieldVerifier:
    """Operator-owned independent detail selectors; model proposals cannot alter these."""

    def __init__(
        self,
        browser_factory: Callable[[], BrowserAdapter],
        selectors: Mapping[str, str],
        normalizer: TenderNormalizer,
        evidence: EvidenceStore,
    ) -> None:
        if "title" not in selectors or "external_tender_id" not in selectors:
            raise ValueError("sample selectors must independently verify title and stable identity")
        self.browser_factory, self.selectors = browser_factory, dict(selectors)
        self.normalizer, self.evidence = normalizer, evidence

    def verify(
        self, run: CollectionRun, source: SourceRecord, settings: SourceSettings
    ) -> SampleVerification:
        checked, differences, references = 0, [], []
        browser = self.browser_factory()
        try:
            session_reference = run.session_reference or settings.authorized_session_reference
            if session_reference:
                browser.restore_session(session_reference)
            for candidate in run.candidates[: settings.repair_checks.sample_size]:
                if candidate.source_url is None:
                    continue
                browser.navigate(candidate.source_url)
                soup = BeautifulSoup(browser.content(), "html.parser")
                raw: dict[str, JsonValue] = {"source_url": candidate.source_url}
                for field, selector in self.selectors.items():
                    element = soup.select_one(selector)
                    if element is None:
                        differences.append(
                            Reason(
                                code="sample_field_missing",
                                detail=f"No witness for {field}",
                                field=field,
                            )
                        )
                    else:
                        raw[field] = element.get_text(" ", strip=True)
                expected = self.normalizer.normalize(raw, source, settings)
                if expected.candidate is None or expected.issues:
                    differences.append(
                        Reason(
                            code="sample_invalid",
                            detail="Source sample could not be normalized independently",
                        )
                    )
                else:
                    checked += 1
                    actual_state, expected_state = state_of(candidate), state_of(expected.candidate)
                    for field in self.selectors:
                        if actual_state.get(field) != expected_state.get(field):
                            differences.append(
                                Reason(
                                    code="sample_mismatch",
                                    detail=f"Independent source witness disagrees for {field}",
                                    field=field,
                                )
                            )
                references.append(self.evidence.put(run.id, {"kind": "repair_sample", "raw": raw}))
        finally:
            browser.close()
        return SampleVerification(
            checked=checked, mismatches=tuple(differences), evidence_references=tuple(references)
        )


class IndependentRepairValidator:
    def __init__(
        self,
        environment: RepairEnvironment,
        normalizer: TenderNormalizer,
        source: SourceRecord,
        settings: SourceSettings,
        samples: SampleVerifier,
        evidence: EvidenceStore,
        clock: Clock,
    ) -> None:
        self.environment, self.normalizer, self.source, self.settings = (
            environment,
            normalizer,
            source,
            settings,
        )
        self.samples, self.evidence, self.clock = samples, evidence, clock

    def validate(
        self, failure: SourceFailure, proposal: FixResult, execution: CollectionRun | SourceFailure
    ) -> RepairValidation:
        checks: list[Reason] = []
        refs = [
            execution.failed_evidence_reference
            if isinstance(execution, SourceFailure)
            else execution.evidence_reference
        ]
        if isinstance(execution, SourceFailure) or proposal.updated_recipe is None:
            return RepairValidation(
                passed=False,
                checks=(
                    Reason(code="execution_failed", detail="Independent recipe execution failed"),
                ),
                confidence=0,
                evidence_references=tuple(refs),
            )
        recipe = proposal.updated_recipe
        if (
            recipe.source_id != failure.source_id
            or execution.source_id != failure.source_id
            or execution.recipe_version != recipe.version
        ):
            checks.append(
                Reason(code="identity_changed", detail="Repair changed source/recipe identity")
            )
        snapshot, previous = self.environment.baseline(failure.source_id)
        health = ConservativeRunHealth().evaluate(
            execution, self.settings, snapshot, previous, now=self.clock.now()
        )
        refs.append(
            self.evidence.put(
                execution.id, {"kind": "repair_health", "health": health.model_dump(mode="json")}
            )
        )
        if not health.publication_allowed or health.overall_status != "healthy":
            checks.extend(
                Reason(code=r.code, detail=r.detail) for r in health.reasons if r.severity != "info"
            )
        if self.settings.repair_checks.require_complete and health.completeness != "complete":
            checks.append(
                Reason(code="repair_incomplete", detail="Repair lacks positive completion evidence")
            )
        old = {identity_of(t).model_dump_json(): t for t in previous}
        new = {identity_of(t).model_dump_json(): t for t in execution.candidates}
        shared = old.keys() & new.keys()
        old_states = {
            key: comparison_state_of(
                value, self.settings.normalization, self.settings.field_change_policy
            )
            for key, value in old.items()
        }
        new_states = {
            key: comparison_state_of(
                value, self.settings.normalization, self.settings.field_change_policy
            )
            for key, value in new.items()
        }
        if old and execution.coverage == "full":
            loss = len(old.keys() - new.keys()) / len(old)
            if loss > self.settings.repair_checks.maximum_identity_loss_ratio:
                checks.append(
                    Reason(
                        code="repair_identity_loss",
                        detail=f"Identity loss {loss:.3f} exceeds policy",
                    )
                )
        if shared:
            for field in MEANINGFUL_FIELDS:
                if self.settings.field_change_policy.get(field) in {"IGNORE", "VOLATILE"}:
                    continue
                changes = sum(
                    old_states[key][field] != new_states[key][field] for key in shared
                ) / len(shared)
                if changes > self.settings.repair_checks.maximum_field_change_ratio:
                    checks.append(
                        Reason(
                            code="repair_mass_field_change",
                            detail=f"Changed ratio {changes:.3f} exceeds policy",
                            field=field,
                        )
                    )
        sample = self.samples.verify(execution, self.source, self.settings)
        refs.extend(sample.evidence_references)
        checks.extend(sample.mismatches)
        required = min(self.settings.repair_checks.sample_size, len(execution.candidates))
        if self.settings.repair_checks.require_samples and sample.checked < required:
            checks.append(
                Reason(
                    code="sample_evidence_missing", detail="Independent source samples insufficient"
                )
            )
        fresh_limit = min(
            self.settings.repair_checks.sample_size, max(1, len(execution.candidates))
        )
        executor = cast(
            RecipeExecutor,
            self.environment.executor(
                RecipeRevision(recipe=recipe, plugin_changes=proposal.plugin_changes),
                self.settings,
            ),
        )
        fresh = executor.execute(
            recipe,
            ExecutionContext(run_id=uuid5(execution.id, "fresh-sample")),
            max_observations=fresh_limit,
        )
        fresh_raw = (
            fresh.observations
            if isinstance(fresh, BoundedCollectionResult)
            else fresh.raw_observations
            if isinstance(fresh, CollectionRun)
            else ()
        )
        fresh_candidates = tuple(
            item.candidate
            for raw in fresh_raw
            if (item := self.normalizer.normalize(raw, self.source, self.settings)).candidate
            is not None
        )
        if isinstance(fresh, SourceFailure) or len(fresh_candidates) < required:
            checks.append(
                Reason(
                    code="repair_fresh_sample_failed", detail="Fresh bounded source check failed"
                )
            )
        elif [
            json.dumps(
                comparison_state_of(
                    item, self.settings.normalization, self.settings.field_change_policy
                ),
                sort_keys=True,
            )
            for item in fresh_candidates[:required]
        ] != [
            json.dumps(
                comparison_state_of(
                    item, self.settings.normalization, self.settings.field_change_policy
                ),
                sort_keys=True,
            )
            for item in execution.candidates[:required]
        ]:
            checks.append(
                Reason(
                    code="repair_fresh_sample_changed",
                    detail="Fresh bounded source samples differ from the validated extraction",
                )
            )
        try:
            captured = self.evidence.get(execution.evidence_reference)
            replay_adapter = CapturedResponseHttpAdapter(captured.get("http_exchanges"))
            browser_replay = CapturedBrowserSteps(captured.get("browser_steps", []))
            replay = dry_run(
                self.environment,
                self.normalizer,
                RecipeRevision(recipe=recipe, plugin_changes=proposal.plugin_changes),
                failure,
                self.source,
                self.settings,
                uuid5(execution.id, "replay"),
                http_adapter=replay_adapter,
                browser_replay=browser_replay,
            )
            replay_adapter.assert_exhausted()
            browser_replay.assert_exhausted()
        except (OSError, KeyError, ValueError, TypeError):
            replay = None
        if isinstance(replay, SourceFailure):
            checks.append(Reason(code="repair_replay_failed", detail="Independent replay failed"))
        elif isinstance(replay, CollectionRun):
            refs.append(replay.evidence_reference)
            first = sorted(
                json.dumps(
                    comparison_state_of(
                        t, self.settings.normalization, self.settings.field_change_policy
                    ),
                    sort_keys=True,
                )
                for t in execution.candidates
            )
            repeated = sorted(
                json.dumps(
                    comparison_state_of(
                        t, self.settings.normalization, self.settings.field_change_policy
                    ),
                    sort_keys=True,
                )
                for t in replay.candidates
            )
            replay_health = ConservativeRunHealth().evaluate(
                replay, self.settings, snapshot, previous, now=self.clock.now()
            )
            if (
                first != repeated
                or not replay_health.publication_allowed
                or replay_health.completeness != health.completeness
            ):
                checks.append(
                    Reason(
                        code="repair_replay_changed",
                        detail="Replay changed normalized observations or health/completion",
                    )
                )
        else:
            checks.append(
                Reason(
                    code="repair_replay_unavailable",
                    detail="Captured responses could not be replayed offline",
                )
            )
        passed = not checks
        if passed:
            checks.append(
                Reason(
                    code="independent_validation_passed",
                    detail="Execution, normalization, health, history, samples and replay passed",
                )
            )
        return RepairValidation(
            baseline_sequence=snapshot.sequence,
            passed=passed,
            checks=tuple(checks),
            confidence=1 if passed else 0,
            evidence_references=tuple(refs),
        )
