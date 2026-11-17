"""Typed sibling scopes. Partial overrides merge only within their own scope."""

import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from tender_system.core.domain import (
    CompletionKind,
    Contract,
    NonEmpty,
    RepairActivationMode,
    Score,
)
from tender_system.core.environment import (
    SAFE_DEFAULTS,
    RuntimeSettings,
    configuration_error,
    load_environment,
)

PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
PositiveInt = Annotated[int, Field(gt=0)]
FieldChangePolicy = Literal["IGNORE", "VOLATILE", "TRACK", "REMATCH"]
# Only normalized business fields can enter history. Other source values stay raw evidence.
BUSINESS_FIELDS = (
    "source_url",
    "title",
    "description",
    "buyer",
    "location",
    "published_at",
    "deadline",
    "value",
    "currency",
    "status",
    "document_urls",
    "product_items",
)


class RetrySettings(Contract):
    max_attempts: PositiveInt = 3
    backoff_seconds: PositiveSeconds = 1


class ModelSettings(Contract):
    provider: NonEmpty = "unconfigured"
    model: NonEmpty = "unconfigured"
    timeout_seconds: PositiveSeconds = 30
    api_key_env: NonEmpty | None = None
    structured_output: Literal["json", "schema"] = "json"
    max_output_tokens: PositiveInt | None = None
    max_attempts: PositiveInt = 3
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    request_interval_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0

    # A byte bound is conservative for multilingual input; context includes output/headroom.
    max_input_bytes: PositiveInt = 11000
    context_window_tokens: PositiveInt = 32768

    @property
    def credential_env(self) -> str | None:
        return self.api_key_env or {
            "groq": "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "gemma": "GEMMA_API_KEY",
        }.get(self.provider)

    def resolve_api_key(self, environ: Mapping[str, str] | None = None) -> SecretStr | None:
        if self.credential_env is None:
            return None
        environment = os.environ if environ is None else environ
        value = environment.get(self.credential_env)
        if not value:
            raise ValueError(f"missing secret environment variable: {self.credential_env}")
        return SecretStr(value)


class InvestigationPolicy(Contract):
    timeout_seconds: PositiveSeconds = 120
    max_actions: PositiveInt = 30
    max_model_requests: Annotated[int, Field(ge=1, le=5)] = 3
    max_input_bytes: Annotated[int, Field(ge=1000, le=16000)] = 16000
    max_output_tokens: Annotated[int, Field(ge=1, le=500)] = 500
    allow_session_inspection: bool = False
    allow_network_request_details: bool = False
    allow_session_changes: bool = False
    max_response_bytes: PositiveInt = 1000000


class RepairChecks(Contract):
    timeout_seconds: PositiveSeconds = 120
    sample_size: PositiveInt = 3
    require_samples: bool = True
    maximum_field_change_ratio: Score = 0.5
    maximum_identity_loss_ratio: Score = 0.25
    require_complete: bool = True


class RepairPolicy(Contract):
    mode: RepairActivationMode = RepairActivationMode.HUMAN
    generated_code_requires_approval: bool = True
    limited_allowed_changes: tuple[
        Literal[
            "selectors",
            "mapping",
            "transforms",
            "acquisition",
            "custom_code",
            "pagination",
            "navigation",
            "authentication",
            "inventory",
        ],
        ...,
    ] = ("selectors", "mapping", "transforms")
    minimum_confidence: Score = 0.9


class ValiditySettings(Contract):
    exclude_expired: bool = True
    excluded_statuses: tuple[str, ...] = ("closed", "completed", "cancelled", "canceled")
    missing_successful_runs: Annotated[int, Field(ge=2)] = 3
    max_age_days: PositiveInt | None = None
    max_unseen_days: PositiveInt | None = None
    age_fallback_only: bool = True


class ValidityOverride(Contract):
    exclude_expired: bool | None = None
    excluded_statuses: tuple[str, ...] | None = None
    missing_successful_runs: Annotated[int, Field(ge=2)] | None = None
    max_age_days: PositiveInt | None = None
    max_unseen_days: PositiveInt | None = None
    age_fallback_only: bool | None = None


class IdentitySettings(Contract):
    url_is_reliable: bool = False
    url_canonicalizer: NonEmpty = "conservative"
    drop_query_parameters: tuple[str, ...] = ()
    remove_fragment: bool = False
    fingerprint_fields: tuple[NonEmpty, ...] = ()

    @field_validator("fingerprint_fields")
    @classmethod
    def unique_inputs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("fingerprint inputs must be unique")
        return values


class NormalizationSettings(Contract):
    field_mapping: dict[str, NonEmpty] = Field(default_factory=dict)
    null_values: tuple[str, ...] = ("",)
    date_formats: dict[str, tuple[NonEmpty, ...]] = Field(default_factory=dict)
    date_only_deadline: Literal["end_of_day", "start_of_day", "reject"] = "end_of_day"
    ambiguous_time_fold: Literal[0, 1] | None = None
    decimal_separator: Literal[".", ","] = "."
    thousands_separator: Literal[".", ",", " "] | None = None
    money_prefixes: tuple[NonEmpty, ...] = ()
    money_suffixes: tuple[NonEmpty, ...] = ()
    status_mapping: dict[str, str] = Field(default_factory=dict)
    collapse_whitespace: bool = True
    case_sensitive_fields: tuple[str, ...] = ()
    parse_failure_policy: Literal["reject", "null_with_issue"] = "reject"

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        fields = {
            "external_tender_id",
            "source_url",
            "title",
            "description",
            "buyer",
            "location",
            "published_at",
            "deadline",
            "value",
            "currency",
            "status",
            "document_urls",
            "product_items",
        }
        if set(self.field_mapping) - fields:
            raise ValueError("unknown tender mapping field")
        if set(self.date_formats) - {"published_at", "deadline"}:
            raise ValueError("date formats require a date field")
        for formats in self.date_formats.values():
            for pattern in formats:
                has_year = any(token in pattern for token in ("%Y", "%y"))
                calendar = any(token in pattern for token in ("%m", "%b", "%B")) and "%d" in pattern
                if not (has_year and calendar):
                    raise ValueError("date formats must specify a complete calendar date")
        if set(self.case_sensitive_fields) - fields:
            raise ValueError("unknown case-sensitive field")
        if self.thousands_separator == self.decimal_separator:
            raise ValueError("decimal and thousands separators must differ")
        return self


class HealthSettings(Contract):
    allow_empty: bool = False
    minimum_count: Annotated[int, Field(ge=0)] = 0
    minimum_retained_ratio: Score = 0.5
    required_fields: tuple[NonEmpty, ...] = ("title",)
    # An unused extension must not change fingerprints of pre-existing source runs.
    required_any_of: tuple[tuple[NonEmpty, ...], ...] = Field(
        default=(), exclude_if=lambda groups: not groups
    )
    reject_quality_issues: bool = True
    max_executed_steps: PositiveInt = 100000
    maximum_changed_ratio: Score | None = 0.9
    minimum_change_sample: PositiveInt = 5
    forbidden_text_patterns: tuple[NonEmpty, ...] = (
        "please log in to continue",
        "sign in to continue",
    )
    max_data_age_seconds: PositiveSeconds | None = None

    @field_validator("required_fields")
    @classmethod
    def known_health_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        from tender_system.core.domain import TenderCandidate

        if set(values) - set(TenderCandidate.model_fields):
            raise ValueError("unknown health field")
        return values

    @field_validator("required_any_of")
    @classmethod
    def known_field_groups(cls, groups: tuple[tuple[str, ...], ...]) -> tuple[tuple[str, ...], ...]:
        for group in groups:
            if not group:
                raise ValueError("required field groups must not be empty")
            cls.known_health_fields(group)
        return groups


class HealthOverride(Contract):
    allow_empty: bool | None = None
    minimum_count: Annotated[int, Field(ge=0)] | None = None
    minimum_retained_ratio: Score | None = None
    required_fields: tuple[NonEmpty, ...] | None = None
    required_any_of: tuple[tuple[NonEmpty, ...], ...] | None = None
    reject_quality_issues: bool | None = None
    max_executed_steps: PositiveInt | None = None
    maximum_changed_ratio: Score | None = None
    minimum_change_sample: PositiveInt | None = None
    forbidden_text_patterns: tuple[NonEmpty, ...] | None = None
    max_data_age_seconds: PositiveSeconds | None = None


class FreshnessPolicy(Contract):
    basis: Literal["observed", "inventory_timestamp", "unknown"] = "unknown"
    max_age_seconds: PositiveSeconds = 86400
    aging_after_seconds: PositiveSeconds | None = None
    unknown_policy: Literal["block", "observe_only"] = "block"
    timestamp_meaning: NonEmpty | None = None

    @model_validator(mode="after")
    def meaningful_timestamp(self) -> Self:
        if self.basis != "unknown" and not self.timestamp_meaning:
            raise ValueError("freshness basis requires source-declared timestamp meaning")
        if (
            self.aging_after_seconds is not None
            and self.aging_after_seconds >= self.max_age_seconds
        ):
            raise ValueError("aging boundary must be below stale boundary")
        return self


class FreshnessOverride(Contract):
    basis: Literal["observed", "inventory_timestamp", "unknown"] | None = None
    max_age_seconds: PositiveSeconds | None = None
    aging_after_seconds: PositiveSeconds | None = None
    unknown_policy: Literal["block", "observe_only"] | None = None
    timestamp_meaning: NonEmpty | None = None


class SourceCalibration(Contract):
    version: NonEmpty = "unconfigured"
    status: Literal["provisional", "calibrated"] = "provisional"
    evidence_references: tuple[NonEmpty, ...] = ()
    full_inventory_supported: bool = False
    expected_mode: Literal["full", "incremental", "either"] = "either"
    completion_signals: tuple[CompletionKind, ...] = ()
    minimum_pages: Annotated[int, Field(ge=0)] = 0
    require_reported_total: bool = False
    empty_policy: Literal["block", "reported_zero", "signature"] = "block"
    maximum_malformed_ratio: Score = 0
    maximum_duplicate_ratio: Score = 0
    session_expiry_signatures: tuple[NonEmpty, ...] = ()
    maintenance_signatures: tuple[NonEmpty, ...] = ()
    empty_signatures: tuple[NonEmpty, ...] = ()
    freshness: FreshnessPolicy = Field(default_factory=FreshnessPolicy)

    @model_validator(mode="after")
    def honest_calibration(self) -> Self:
        if self.status == "calibrated" and not self.evidence_references:
            raise ValueError("calibrated policy requires evidence references")
        if self.full_inventory_supported and not self.completion_signals:
            raise ValueError("full-inventory capability requires positive completion criteria")
        if self.empty_policy == "signature" and not self.empty_signatures:
            raise ValueError("signature empty policy requires known empty signatures")
        return self


class CalibrationOverride(Contract):
    version: NonEmpty | None = None
    status: Literal["provisional", "calibrated"] | None = None
    evidence_references: tuple[NonEmpty, ...] | None = None
    full_inventory_supported: bool | None = None
    expected_mode: Literal["full", "incremental", "either"] | None = None
    completion_signals: tuple[CompletionKind, ...] | None = None
    minimum_pages: Annotated[int, Field(ge=0)] | None = None
    require_reported_total: bool | None = None
    empty_policy: Literal["block", "reported_zero", "signature"] | None = None
    maximum_malformed_ratio: Score | None = None
    maximum_duplicate_ratio: Score | None = None
    session_expiry_signatures: tuple[NonEmpty, ...] | None = None
    maintenance_signatures: tuple[NonEmpty, ...] | None = None
    empty_signatures: tuple[NonEmpty, ...] | None = None
    freshness: FreshnessOverride = Field(default_factory=FreshnessOverride)


class SourceSettings(Contract):
    public_readonly: bool = False
    timezone: NonEmpty = "UTC"
    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    normalization: NormalizationSettings = Field(default_factory=NormalizationSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    calibration: SourceCalibration = Field(default_factory=SourceCalibration)
    check_interval_seconds: PositiveSeconds = 14400
    timeout_seconds: PositiveSeconds = 30
    retry: RetrySettings = Field(default_factory=RetrySettings)
    acquisition_adapter: NonEmpty = "http"
    browser_adapter: NonEmpty = "playwright"
    authorized_session_reference: NonEmpty | None = None
    browser_fallback_enabled: bool = False
    fallback_recipe_reference: str | None = None
    batch_size: PositiveInt = 100
    budget_limit: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 10
    fixer_model: ModelSettings = Field(default_factory=ModelSettings)
    stronger_fixer_model: ModelSettings = Field(default_factory=ModelSettings)
    fixer_escalation_enabled: bool = True
    repair: RepairPolicy = Field(default_factory=RepairPolicy)
    investigation: InvestigationPolicy = Field(default_factory=InvestigationPolicy)
    repair_checks: RepairChecks = Field(default_factory=RepairChecks)
    fallback_max_runs: PositiveInt = 3
    fallback_max_age_seconds: PositiveSeconds = 86400
    validity: ValiditySettings = Field(default_factory=ValiditySettings)
    rematch_fields: tuple[str, ...] = (
        "title",
        "description",
        "deadline",
        "value",
        "status",
        "product_items",
    )
    field_change_policy: dict[NonEmpty, FieldChangePolicy] = Field(default_factory=dict)

    @field_validator("field_change_policy")
    @classmethod
    def known_policy_fields(
        cls, values: dict[str, FieldChangePolicy]
    ) -> dict[str, FieldChangePolicy]:
        if any(
            name not in BUSINESS_FIELDS and policy in {"TRACK", "REMATCH"}
            for name, policy in values.items()
        ):
            raise ValueError("tracked fields must be normalized business fields")
        return values

    @property
    def effective_rematch_fields(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in BUSINESS_FIELDS
            if self.field_change_policy.get(
                name, "REMATCH" if name in self.rematch_fields else "TRACK"
            )
            == "REMATCH"
        )

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown source timezone") from None
        return value

    @field_validator("rematch_fields")
    @classmethod
    def known_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        allowed = set(BUSINESS_FIELDS)
        if len(values) != len(set(values)) or set(values) - allowed:
            raise ValueError("rematch fields must be unique meaningful tender fields")
        return values


class ModeThresholds(Contract):
    match: Score = 0.7
    no_match: Score = 0.3
    confidence: Score = 0.65

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.no_match >= self.match:
            raise ValueError("no-match threshold must be below match threshold")
        return self


class ThresholdPolicy(Contract):
    broad: ModeThresholds = Field(
        default_factory=lambda: ModeThresholds(match=0.5, no_match=0.15, confidence=0.5)
    )
    balanced: ModeThresholds = Field(default_factory=ModeThresholds)
    strict: ModeThresholds = Field(
        default_factory=lambda: ModeThresholds(match=0.9, no_match=0.5, confidence=0.85)
    )


class MatcherRouting(Contract):
    semantic_enabled: bool = True
    escalation_enabled: bool = False
    stronger_model: ModelSettings | None = None
    max_model_calls: Annotated[int, Field(ge=0, le=2)] = 2
    exclude_expired: bool = True
    excluded_statuses: tuple[NonEmpty, ...] = ("closed", "completed", "cancelled", "canceled")

    @model_validator(mode="after")
    def configured_escalation(self) -> Self:
        if self.escalation_enabled and self.stronger_model is None:
            raise ValueError("escalation requires a stronger model configuration")
        return self


class UncertaintySettings(Contract):
    enabled: bool = True
    required_fields: tuple[NonEmpty, ...] = ()
    abstain_on_conflicts: bool = True
    adjacent_modes: tuple[Literal["broad", "balanced", "strict"], ...] = ("broad",)
    partial_modes: tuple[Literal["broad", "balanced", "strict"], ...] = ("broad", "balanced")
    inferred_modes: tuple[Literal["broad", "balanced", "strict"], ...] = ("broad",)
    borderline_margin: Score = 0.05
    escalate_provider_failures: bool = False

    @field_validator("required_fields")
    @classmethod
    def known_fields(cls, fields: tuple[str, ...]) -> tuple[str, ...]:
        if set(fields) - set(BUSINESS_FIELDS):
            raise ValueError("unknown required tender field")
        return fields


class CampaignSettings(Contract):
    validity: ValidityOverride = Field(default_factory=ValidityOverride)
    matcher_adapter: NonEmpty = "rules"
    matcher_version: NonEmpty = "1"
    manual_evaluation_id: UUID | None = None
    # Planner-owned input fingerprint, stored inside existing pinned job configuration.
    evaluation_context: str | None = None
    timeout_seconds: PositiveSeconds = 30
    lease_seconds: PositiveSeconds = 3600
    model: ModelSettings = Field(
        default_factory=lambda: ModelSettings(provider="groq", model="qwen/qwen3.8-27b")
    )
    routing: MatcherRouting = Field(default_factory=MatcherRouting)
    thresholds: ThresholdPolicy = Field(default_factory=ThresholdPolicy)
    uncertainty: UncertaintySettings = Field(default_factory=UncertaintySettings)
    matching_mode: Literal["broad", "balanced", "strict"] = "balanced"
    match_threshold: Score = 0.7
    no_match_threshold: Score = 0.3
    batch_size: PositiveInt = 100
    retry: RetrySettings = Field(default_factory=RetrySettings)
    budget_limit: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 10

    @field_serializer("validity")
    def serialize_validity(self, value: ValidityOverride) -> dict[str, object]:
        # Preserve omitted vs explicit-null overrides through job/config round trips.
        return value.model_dump(exclude_unset=True)

    @model_validator(mode="after")
    def ordered_thresholds(self) -> Self:
        if self.no_match_threshold >= self.match_threshold:
            raise ValueError("no-match threshold must be below match threshold")
        if self.lease_seconds <= self.batch_size * self.timeout_seconds:
            raise ValueError("lease must exceed the maximum batch matching time")
        return self


class RetryOverride(Contract):
    max_attempts: PositiveInt | None = None
    backoff_seconds: PositiveSeconds | None = None


class ModelOverride(Contract):
    max_input_bytes: PositiveInt | None = None
    context_window_tokens: PositiveInt | None = None
    provider: NonEmpty | None = None
    model: NonEmpty | None = None
    timeout_seconds: PositiveSeconds | None = None
    api_key_env: NonEmpty | None = None
    structured_output: Literal["json", "schema"] | None = None
    max_output_tokens: PositiveInt | None = None
    max_attempts: PositiveInt | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    request_interval_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None


class InvestigationOverride(Contract):
    timeout_seconds: PositiveSeconds | None = None
    max_actions: PositiveInt | None = None
    max_model_requests: Annotated[int, Field(ge=1, le=5)] | None = None
    max_input_bytes: Annotated[int, Field(ge=1000, le=16000)] | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=500)] | None = None
    allow_session_inspection: bool | None = None
    allow_network_request_details: bool | None = None
    allow_session_changes: bool | None = None
    max_response_bytes: PositiveInt | None = None


class RepairChecksOverride(Contract):
    timeout_seconds: PositiveSeconds | None = None
    sample_size: PositiveInt | None = None
    require_samples: bool | None = None
    maximum_field_change_ratio: Score | None = None
    maximum_identity_loss_ratio: Score | None = None
    require_complete: bool | None = None


class RepairOverride(Contract):
    mode: RepairActivationMode | None = None
    generated_code_requires_approval: bool | None = None
    limited_allowed_changes: (
        tuple[
            Literal[
                "selectors",
                "mapping",
                "transforms",
                "acquisition",
                "custom_code",
                "pagination",
                "navigation",
                "authentication",
                "inventory",
            ],
            ...,
        ]
        | None
    ) = None
    minimum_confidence: Score | None = None


class IdentityOverride(Contract):
    url_is_reliable: bool | None = None
    url_canonicalizer: NonEmpty | None = None
    drop_query_parameters: tuple[str, ...] | None = None
    remove_fragment: bool | None = None
    fingerprint_fields: tuple[NonEmpty, ...] | None = None


class NormalizationOverride(Contract):
    field_mapping: dict[str, NonEmpty] = Field(default_factory=dict)
    null_values: tuple[str, ...] | None = None
    date_formats: dict[str, tuple[NonEmpty, ...]] = Field(default_factory=dict)
    date_only_deadline: Literal["end_of_day", "start_of_day", "reject"] | None = None
    ambiguous_time_fold: Literal[0, 1] | None = None
    decimal_separator: Literal[".", ","] | None = None
    thousands_separator: Literal[".", ",", " "] | None = None
    money_prefixes: tuple[NonEmpty, ...] | None = None
    money_suffixes: tuple[NonEmpty, ...] | None = None
    status_mapping: dict[str, str] = Field(default_factory=dict)
    collapse_whitespace: bool | None = None
    case_sensitive_fields: tuple[str, ...] | None = None
    parse_failure_policy: Literal["reject", "null_with_issue"] | None = None


class SourceOverride(Contract):
    public_readonly: bool | None = None
    timezone: NonEmpty | None = None
    identity: IdentityOverride = Field(default_factory=IdentityOverride)
    normalization: NormalizationOverride = Field(default_factory=NormalizationOverride)
    health: HealthOverride = Field(default_factory=HealthOverride)
    calibration: CalibrationOverride = Field(default_factory=CalibrationOverride)
    check_interval_seconds: PositiveSeconds | None = None
    timeout_seconds: PositiveSeconds | None = None
    retry: RetryOverride = Field(default_factory=RetryOverride)
    acquisition_adapter: NonEmpty | None = None
    browser_adapter: NonEmpty | None = None
    authorized_session_reference: NonEmpty | None = None
    browser_fallback_enabled: bool | None = None
    fallback_recipe_reference: str | None = None
    batch_size: PositiveInt | None = None
    budget_limit: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    fixer_model: ModelOverride = Field(default_factory=ModelOverride)
    stronger_fixer_model: ModelOverride = Field(default_factory=ModelOverride)
    fixer_escalation_enabled: bool | None = None
    repair: RepairOverride = Field(default_factory=RepairOverride)
    investigation: InvestigationOverride = Field(default_factory=InvestigationOverride)
    repair_checks: RepairChecksOverride = Field(default_factory=RepairChecksOverride)
    fallback_max_runs: PositiveInt | None = None
    fallback_max_age_seconds: PositiveSeconds | None = None
    validity: ValidityOverride = Field(default_factory=ValidityOverride)
    rematch_fields: tuple[str, ...] | None = None
    field_change_policy: dict[NonEmpty, FieldChangePolicy] = Field(default_factory=dict)


class CampaignOverride(Contract):
    validity: ValidityOverride = Field(default_factory=ValidityOverride)
    matcher_adapter: NonEmpty | None = None
    matcher_version: NonEmpty | None = None
    manual_evaluation_id: UUID | None = None
    evaluation_context: str | None = None
    timeout_seconds: PositiveSeconds | None = None
    lease_seconds: PositiveSeconds | None = None
    model: ModelOverride = Field(default_factory=ModelOverride)
    routing: MatcherRouting | None = None
    thresholds: ThresholdPolicy | None = None
    uncertainty: UncertaintySettings | None = None
    matching_mode: Literal["broad", "balanced", "strict"] | None = None
    match_threshold: Score | None = None
    no_match_threshold: Score | None = None
    batch_size: PositiveInt | None = None
    retry: RetryOverride = Field(default_factory=RetryOverride)
    budget_limit: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None


class GlobalSettings(Contract):
    source: SourceSettings = Field(default_factory=SourceSettings)
    campaign: CampaignSettings = Field(default_factory=CampaignSettings)
    database_url_env: NonEmpty = "DATABASE_URL"


def merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    result = base.copy()
    for key, value in override.items():
        previous = result.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            result[key] = merge(previous, value)
        else:
            result[key] = value
    return result


class Configuration(Contract):
    schema_version: Literal[1] = 1
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    global_settings: GlobalSettings = Field(default_factory=GlobalSettings, alias="global")
    sources: dict[str, SourceOverride] = Field(default_factory=dict)
    campaigns: dict[str, CampaignOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_effective_settings(self) -> Self:
        if any(not key.strip() for key in (*self.sources, *self.campaigns)):
            raise ValueError("scope IDs must not be blank")
        for source_id in self.sources:
            self.for_source(source_id)
        for campaign_id in self.campaigns:
            self.for_campaign(campaign_id)
        for matching_campaign in ("", *self.campaigns):
            for matching_source in ("", *self.sources):
                self.for_matching_source(matching_source, matching_campaign)
        return self

    def for_source(self, source_id: str) -> SourceSettings:
        override = self.sources.get(source_id, SourceOverride())
        values = merge(
            self.global_settings.source.model_dump(), override.model_dump(exclude_unset=True)
        )
        return SourceSettings.model_validate(values)

    def for_campaign(self, campaign_id: str) -> CampaignSettings:
        override = self.campaigns.get(campaign_id, CampaignOverride())
        defaults = self.global_settings.campaign.model_dump()
        defaults["validity"] = self.global_settings.campaign.validity.model_dump(exclude_unset=True)
        values = merge(defaults, override.model_dump(exclude_unset=True))
        if not self.runtime.ai_semantic_enabled:
            values["matcher_adapter"] = "rules"
            values["routing"] = {
                **cast(dict[str, object], values["routing"]),
                "semantic_enabled": False,
            }
        return CampaignSettings.model_validate(values)

    def for_matching_source(
        self, source_id: str, campaign_id: str, *, source_settings: SourceSettings | None = None
    ) -> SourceSettings:
        """Matching only: explicit campaign validity overrides effective source validity."""
        source = source_settings or self.for_source(source_id)
        policy = ValiditySettings.model_validate(
            merge(
                source.validity.model_dump(),
                self.for_campaign(campaign_id).validity.model_dump(exclude_unset=True),
            )
        )
        return source.model_copy(update={"validity": policy})


def load_configuration(
    path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    validate_models: bool = True,
) -> Configuration:
    """Legacy TOML < dotenv/shell startup defaults < persisted scoped overrides.

    QUARRY_SOURCE__ and QUARRY_CAMPAIGN__ accept any existing typed field path.
    JSON arrays/objects/null; plain strings, numbers and booleans otherwise.
    """
    environment = load_environment() if environ is None else environ
    config_path = path or (
        Path(environment["QUARRY_CONFIG_FILE"]) if environment.get("QUARRY_CONFIG_FILE") else None
    )
    data: dict[str, Any] = {}
    if config_path:
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
    runtime = dict(SAFE_DEFAULTS)
    runtime.update(data.get("runtime", {}))
    for name in RuntimeSettings.model_fields:
        if (key := "QUARRY_" + name.upper()) in environment:
            runtime[name] = environment[key]
    data["runtime"] = runtime
    global_data = data.setdefault("global", {})
    slots = {
        "MATCHER": ("campaign", "model"),
        "STRONGER_MATCHER": ("campaign", "routing", "stronger_model"),
    }
    for key, raw in environment.items():
        parts: tuple[str, ...] | None = None
        for prefix, scope in (("QUARRY_SOURCE__", "source"), ("QUARRY_CAMPAIGN__", "campaign")):
            if key.startswith(prefix):
                parts = (scope, *key[len(prefix) :].lower().split("__"))
        for slot, target in slots.items():
            prefix = f"QUARRY_{slot}__"
            if key.startswith(prefix):
                parts = (*target, key[len(prefix) :].lower())
        if parts is None or raw == "":
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        target_data = global_data
        for part in parts[:-1]:
            if target_data.get(part) is None:
                target_data[part] = {}
            target_data = target_data.setdefault(part, {})
        target_data[parts[-1]] = value
    try:
        config = Configuration.model_validate(data)
    except ValidationError as error:
        raise configuration_error(error) from None

    if validate_models:
        validate_model_configuration(config, environment)
    return config


def validate_model_configuration(
    config: Configuration, environment: Mapping[str, str] | None = None
) -> None:
    environment = load_environment() if environment is None else environment
    required_models: list[tuple[str, ModelSettings]] = []
    campaign = config.global_settings.campaign
    if config.runtime.ai_semantic_enabled and campaign.matcher_adapter == "intelligent":
        required_models.append(("MATCHER", campaign.model))
        if campaign.routing.escalation_enabled and campaign.routing.stronger_model:
            required_models.append(("STRONGER_MATCHER", campaign.routing.stronger_model))
    errors: list[str] = []
    for slot, model in required_models:
        if model.provider == "unconfigured" or model.model == "unconfigured":
            errors.append(
                f"{slot}: configure provider and model or disable corresponding AI feature"
            )
        elif not model.credential_env or not environment.get(model.credential_env):
            errors.append(
                f"{slot}: missing credential variable "
                f"{model.credential_env or '(api_key_env unset)'}"
            )
    if errors:
        raise ValueError("Invalid Quarry configuration: " + "; ".join(errors))
