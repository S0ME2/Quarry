"""Pure tender comparison, linked history reconstruction and validity policies."""

import unicodedata
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from pydantic import JsonValue, ValidationError

from tender_system.core.configuration import (
    BUSINESS_FIELDS,
    FieldChangePolicy,
    NormalizationSettings,
    SourceSettings,
)
from tender_system.core.domain import (
    Reason,
    Tender,
    TenderCandidate,
    TenderComparison,
    TenderIdentity,
    TenderVersion,
    ValidityDecision,
)

IDENTITY_FIELDS = (
    "source_id",
    "external_tender_id",
    "source_has_stable_external_id",
    "fallback_kind",
    "fallback_key",
)
MEANINGFUL_FIELDS = BUSINESS_FIELDS
STATE_FIELDS = (*IDENTITY_FIELDS, *MEANINGFUL_FIELDS)
TEXT_FIELDS = {"title", "description", "buyer", "location", "currency", "status"}


class HistoryCorruption(ValueError):
    pass


class IdentityConflict(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class StaleObservation(ValueError):
    pass


def identity_of(tender: TenderCandidate) -> TenderIdentity:
    return TenderIdentity.model_validate(tender.model_dump(include=set(IDENTITY_FIELDS)))


def state_of(tender: TenderCandidate) -> dict[str, JsonValue]:
    state = cast(dict[str, JsonValue], tender.model_dump(mode="json", include=set(STATE_FIELDS)))
    for name in ("published_at", "deadline"):
        value = getattr(tender, name)
        if value is not None:
            state[name] = value.astimezone(UTC).isoformat()
    return state


def normalized_text(value: str, *, case_sensitive: bool = False) -> str:
    text = " ".join(unicodedata.normalize("NFC", value).split())
    return text if case_sensitive else text.casefold()


def comparison_state_of(
    tender: TenderCandidate,
    settings: NormalizationSettings,
    policy: dict[str, FieldChangePolicy],
) -> dict[str, JsonValue]:
    """Stable semantic state for change detection and independent repair replay."""
    state = state_of(tender)
    for name in MEANINGFUL_FIELDS:
        if policy.get(name) in {"IGNORE", "VOLATILE"}:
            state.pop(name)
        elif name in TEXT_FIELDS and isinstance(state[name], str):
            state[name] = normalized_text(
                str(state[name]), case_sensitive=name in settings.case_sensitive_fields
            )
    if tender.value is not None and "value" in state:
        amount = format(tender.value, "f")
        state["value"] = amount.rstrip("0").rstrip(".") if "." in amount else amount
    return state


class SemanticTenderComparator:
    def __init__(
        self,
        settings: NormalizationSettings | None = None,
        field_change_policy: dict[str, FieldChangePolicy] | None = None,
    ) -> None:
        self._settings = settings or NormalizationSettings()
        self._policy = field_change_policy or {}

    def compare(
        self, stored: Tender | None, candidate: TenderCandidate, rematch_fields: tuple[str, ...]
    ) -> TenderComparison:
        if set(rematch_fields) - set(MEANINGFUL_FIELDS):
            raise ValueError("unknown rematch field")
        if stored is None:
            return TenderComparison(
                classification="new", changed_fields=state_of(candidate), requires_rematch=True
            )
        if identity_of(stored) != identity_of(candidate):
            raise IdentityConflict("comparison cannot change source identity")
        state = state_of(candidate)
        before_state = comparison_state_of(stored, self._settings, self._policy)
        after_state = comparison_state_of(candidate, self._settings, self._policy)
        changes: dict[str, JsonValue] = {}
        for name in MEANINGFUL_FIELDS:
            if name not in after_state:
                continue
            if before_state[name] != after_state[name]:
                changes[name] = state[name]
        return TenderComparison(
            classification="changed" if changes else "same",
            changed_fields=changes,
            requires_rematch=any(
                self._policy.get(name, "REMATCH" if name in rematch_fields else "TRACK")
                == "REMATCH"
                for name in changes
            ),
        )


def reconstruct_history(
    versions: tuple[TenderVersion, ...], target_version_id: UUID | None = None
) -> dict[str, JsonValue]:
    """Validate the entire chain, then return the requested/current business state."""
    if not versions:
        raise HistoryCorruption("history is empty")
    by_id = {version.id: version for version in versions}
    if len(by_id) != len(versions) or len({version.tender_id for version in versions}) != 1:
        raise HistoryCorruption("duplicate versions or mixed tender ownership")
    roots = [version for version in versions if version.previous_version_id is None]
    # Pre-product histories remain immutable and valid. No other missing field is allowed.
    root_fields = set(roots[0].changed_fields) if len(roots) == 1 else set()
    if len(roots) != 1 or root_fields not in (
        set(STATE_FIELDS),
        set(STATE_FIELDS) - {"product_items"},
    ):
        raise HistoryCorruption("history requires exactly one full initial state")
    next_version: dict[UUID, TenderVersion] = {}
    for version in versions:
        previous = version.previous_version_id
        if previous is not None:
            if previous not in by_id or previous in next_version:
                raise HistoryCorruption("dangling previous version or fork")
            if not version.changed_fields or set(version.changed_fields) - set(MEANINGFUL_FIELDS):
                raise HistoryCorruption("later versions require meaningful nonempty diffs")
            next_version[previous] = version
    current: TenderVersion | None = roots[0]
    state: dict[str, JsonValue] = {}
    target: dict[str, JsonValue] | None = None
    visited: set[UUID] = set()
    while current is not None:
        if current.id in visited:
            raise HistoryCorruption("version cycle")
        visited.add(current.id)
        state.update(deepcopy(current.changed_fields))
        try:
            TenderCandidate.model_validate(state)
        except ValidationError:
            raise HistoryCorruption("version contains invalid tender values") from None
        if current.id == target_version_id:
            target = deepcopy(state)
        current = next_version.get(current.id)
    if len(visited) != len(versions):
        raise HistoryCorruption("disconnected history or cycle")
    if target_version_id is not None and target is None:
        raise HistoryCorruption("requested version is not in history")
    return target if target is not None else state


class ConfigurableTenderValidity:
    def evaluate(self, tender: Tender, settings: SourceSettings, now: datetime) -> ValidityDecision:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("validity evaluation requires timezone-aware current time")
        now = now.astimezone(UTC)
        policy = settings.validity
        reasons: list[Reason] = []
        if (
            policy.exclude_expired
            and tender.deadline is not None
            and tender.deadline.astimezone(UTC) <= now
        ):
            reasons.append(Reason(code="expired", detail="Deadline has passed", field="deadline"))
        status = normalized_text(tender.status or "")
        if status in {normalized_text(value) for value in policy.excluded_statuses}:
            reasons.append(
                Reason(code="excluded_status", detail="Status is excluded", field="status")
            )
        if tender.presence.missing_successful_runs >= policy.missing_successful_runs:
            reasons.append(Reason(code="missing", detail="Accepted-run missing threshold reached"))
        if not policy.age_fallback_only or tender.deadline is None:
            age_start = (tender.published_at or tender.first_seen_at).astimezone(UTC)
            if policy.max_age_days is not None and now - age_start >= timedelta(
                days=policy.max_age_days
            ):
                reasons.append(Reason(code="too_old", detail="Configured maximum age reached"))
            if policy.max_unseen_days is not None and now - tender.last_seen_at.astimezone(
                UTC
            ) >= timedelta(days=policy.max_unseen_days):
                reasons.append(Reason(code="unseen", detail="Configured last-seen limit reached"))
        return ValidityDecision(matchable=not reasons, reasons=tuple(reasons))
