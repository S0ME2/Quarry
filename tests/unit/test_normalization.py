from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import JsonValue, ValidationError

from tender_system.collection.normalization import (
    ConfigurableTenderNormalizer,
    ConservativeUrlCanonicalizer,
    UrlCanonicalizer,
)
from tender_system.core.configuration import Configuration, IdentitySettings, SourceSettings
from tender_system.core.domain import SourceRecord
from tender_system.core.ports import TenderNormalizer
from tender_system.core.registry import Registry


def settings(**values: object) -> SourceSettings:
    return SourceSettings.model_validate(values)


def test_nullable_fields_and_provenance() -> None:
    normalizer: TenderNormalizer = ConfigurableTenderNormalizer()
    raw: dict[str, JsonValue] = {
        "external_tender_id": "001",
        "title": None,
        "description": "  Equipment\n  supply ",
        "unexpected": {"original": "unchanged"},
    }
    result = normalizer.normalize(raw, SourceRecord(id="source", name="Source"), SourceSettings())
    assert result.issues == ()
    assert result.candidate is not None
    assert result.candidate.external_tender_id == "001"
    assert result.candidate.title is None
    assert result.candidate.source_url is None
    assert result.candidate.description == "Equipment supply"
    assert result.raw_data == raw
    assert result.candidate.raw_data == raw
    raw["external_tender_id"] = "mutated"
    assert result.raw_data["external_tender_id"] == "001"


@pytest.mark.parametrize("products", ["bad", [None], [{"name": "Router"}, 42]])
def test_malformed_products_are_diagnosed_with_original_payload(products: JsonValue) -> None:
    raw = {"external_tender_id": "001", "product_items": products}
    result = ConfigurableTenderNormalizer().normalize(
        raw, SourceRecord(id="s", name="Source"), SourceSettings()
    )
    assert result.candidate is None
    assert result.issues[0].field == "product_items"
    assert result.raw_data == raw


def test_partial_product_attributes_are_preserved_and_mappable() -> None:
    products: JsonValue = [{"id": "1", "quantity": None}, {"name": "Router"}]
    result = ConfigurableTenderNormalizer().normalize(
        {"external_tender_id": "001", "goods": products},
        SourceRecord(id="s", name="Source"),
        settings(normalization={"field_mapping": {"product_items": "goods"}}),
    )
    assert result.candidate and list(result.candidate.product_items or ()) == products


@pytest.mark.parametrize("id_value", [None, "", " ", True, 12.5])
def test_expected_external_id_never_falls_back(id_value: JsonValue) -> None:
    result = ConfigurableTenderNormalizer().normalize(
        {"external_tender_id": id_value, "source_url": "https://source.test/tender?id=1"},
        SourceRecord(id="source", name="Source"),
        settings(identity={"url_is_reliable": True, "fingerprint_fields": ["source_url"]}),
    )
    assert result.candidate is None
    assert result.issues[-1].code == "identity_failure"


def test_external_ids_preserve_case_leading_zeros_and_source_scope() -> None:
    raw: dict[str, JsonValue] = {"external_tender_id": "AbC-001"}
    normalizer = ConfigurableTenderNormalizer()
    first = normalizer.normalize(raw, SourceRecord(id="one", name="One"), SourceSettings())
    second = normalizer.normalize(raw, SourceRecord(id="two", name="Two"), SourceSettings())
    assert first.candidate is not None and second.candidate is not None
    assert first.candidate.external_tender_id == second.candidate.external_tender_id == "AbC-001"
    assert first.candidate.source_id != second.candidate.source_id


def test_reliable_url_precedes_fingerprint_without_destructive_canonicalization() -> None:
    url = "https://Source.test/a%2Fb;part?b=2&a=%2F&a=3#tender-1"
    result = ConfigurableTenderNormalizer().normalize(
        {"source_url": url, "stable": "other"},
        SourceRecord(id="s", name="Source", has_stable_external_id=False),
        settings(identity={"url_is_reliable": True, "fingerprint_fields": ["stable"]}),
    )
    assert result.candidate is not None
    assert result.candidate.fallback_kind == "url"
    assert result.candidate.fallback_key == url


def test_url_rules_remove_only_explicit_components_preserving_encoding_and_order() -> None:
    canonical = ConservativeUrlCanonicalizer().canonicalize(
        "https://source.test/path?keep=%2f&track=x&keep=2#fragment",
        IdentitySettings(drop_query_parameters=("track",), remove_fragment=True),
    )
    assert canonical == "https://source.test/path?keep=%2f&keep=2"


def test_source_url_plugin_is_replaceable() -> None:
    class KeepPath:
        def canonicalize(self, url: str, settings: IdentitySettings) -> str:
            return url.split("?", 1)[0]

    registry = Registry[UrlCanonicalizer]()
    registry.register("custom", KeepPath())
    result = ConfigurableTenderNormalizer(registry).normalize(
        {"source_url": "https://source.test/1?tracking=x"},
        SourceRecord(id="s", name="Source", has_stable_external_id=False),
        settings(identity={"url_is_reliable": True, "url_canonicalizer": "custom"}),
    )
    assert result.candidate is not None
    assert result.candidate.fallback_key == "https://source.test/1"
    assert result.candidate.source_url == "https://source.test/1?tracking=x"


def test_fingerprint_uses_only_explicit_stable_inputs_and_is_order_independent() -> None:
    source = SourceRecord(id="s", name="Source", has_stable_external_id=False)
    normalizer = ConfigurableTenderNormalizer()
    raw: dict[str, JsonValue] = {"buyer_code": "001", "reference": "R1", "title": "Old"}
    first = normalizer.normalize(
        raw, source, settings(identity={"fingerprint_fields": ["buyer_code", "reference"]})
    )
    second = normalizer.normalize(
        {**raw, "title": "New", "external_tender_id": "unstable"},
        source,
        settings(identity={"fingerprint_fields": ["reference", "buyer_code"]}),
    )
    assert first.candidate is not None and second.candidate is not None
    assert first.candidate.fallback_key == second.candidate.fallback_key
    assert first.candidate.fallback_kind == "fingerprint"
    assert second.candidate.external_tender_id is None


@pytest.mark.parametrize(
    "raw,fields",
    [
        ({"title": "Plausible", "source_url": "https://source.test"}, []),
        ({"title": "Plausible"}, ["missing"]),
        ({"reference": None}, ["reference"]),
        ({"reference": " "}, ["reference"]),
        ({"reference": {"nested": "value"}}, ["reference"]),
    ],
)
def test_unconfigured_or_unsafe_fingerprints_fail_clearly(
    raw: dict[str, JsonValue], fields: list[str]
) -> None:
    result = ConfigurableTenderNormalizer().normalize(
        raw,
        SourceRecord(id="s", name="Source", has_stable_external_id=False),
        settings(identity={"fingerprint_fields": fields}),
    )
    assert result.candidate is None
    assert result.issues[-1].field == "identity"
    assert "fingerprint" in result.issues[-1].detail


@pytest.mark.parametrize(
    "field,value",
    [
        ("deadline", "32/14/2026"),
        ("published_at", "2026-02-30"),
        ("value", "1,234.50"),
        ("value", "NaN"),
        ("value", -10),
        ("value", True),
        ("value", "95%"),
        ("title", 123),
    ],
)
def test_malformed_values_are_observable_and_rejected_by_default(
    field: str, value: JsonValue
) -> None:
    raw = {"external_tender_id": "1", field: value}
    result = ConfigurableTenderNormalizer().normalize(
        raw, SourceRecord(id="s", name="Source"), SourceSettings()
    )
    assert result.candidate is None
    assert result.issues[0].field == field
    assert result.issues[0].code == "parse_failure"
    assert result.raw_data[field] == value


def test_null_with_issue_requires_explicit_configuration() -> None:
    result = ConfigurableTenderNormalizer().normalize(
        {"external_tender_id": "1", "deadline": "bad"},
        SourceRecord(id="s", name="Source"),
        settings(normalization={"parse_failure_policy": "null_with_issue"}),
    )
    assert result.candidate is not None
    assert result.candidate.deadline is None
    assert result.candidate.quality_issues == result.issues
    assert result.issues[0].severity == "warning"
    assert result.candidate.raw_data["deadline"] == "bad"


def test_per_source_mapping_formats_null_tokens_and_money_locale() -> None:
    config = Configuration.model_validate(
        {
            "sources": {
                "s": {
                    "timezone": "Asia/Tashkent",
                    "normalization": {
                        "field_mapping": {"deadline": "end", "external_tender_id": "id"},
                        "date_formats": {"deadline": ["%d.%m.%Y"]},
                        "null_values": ["", "N/A"],
                        "decimal_separator": ",",
                        "thousands_separator": ".",
                        "money_prefixes": ["EUR"],
                        "status_mapping": {"active": "open"},
                    },
                }
            }
        }
    )
    raw: dict[str, JsonValue] = {
        "id": 123,
        "end": "04.10.2026",
        "value": "EUR 1.234,50",
        "description": "N/A",
        "status": "ACTIVE",
    }
    result = ConfigurableTenderNormalizer().normalize(
        raw, SourceRecord(id="s", name="Source"), config.for_source("s")
    )
    assert result.candidate is not None
    assert result.candidate.external_tender_id == "123"
    assert result.candidate.description is None
    assert result.candidate.value == Decimal("1234.50")
    assert result.candidate.deadline == datetime(2026, 10, 4, 18, 59, 59, 999999, tzinfo=UTC)
    assert result.candidate.status == "open"
    assert config.for_source("other").normalization.date_formats == {}


@pytest.mark.parametrize("policy,hour,minute", [("start_of_day", 19, 0), ("end_of_day", 18, 59)])
def test_date_only_timezone_boundaries(policy: str, hour: int, minute: int) -> None:
    result = ConfigurableTenderNormalizer().normalize(
        {"external_tender_id": "1", "deadline": "2026-10-04"},
        SourceRecord(id="s", name="Source"),
        settings(timezone="Asia/Tashkent", normalization={"date_only_deadline": policy}),
    )
    assert result.candidate is not None and result.candidate.deadline is not None
    assert result.candidate.deadline.hour == hour and result.candidate.deadline.minute == minute
    assert result.candidate.deadline.day == (3 if policy == "start_of_day" else 4)


def test_date_only_rejection_and_aware_offset_preservation() -> None:
    normalizer = ConfigurableTenderNormalizer()
    source = SourceRecord(id="s", name="Source")
    rejected = normalizer.normalize(
        {"external_tender_id": "1", "deadline": "2026-10-04"},
        source,
        settings(normalization={"date_only_deadline": "reject"}),
    )
    assert rejected.candidate is None
    aware = normalizer.normalize(
        {"external_tender_id": "1", "deadline": "2026-10-04T12:00:00+03:00"},
        source,
        settings(timezone="Asia/Tashkent"),
    )
    assert aware.candidate is not None
    assert aware.candidate.deadline == datetime(2026, 10, 4, 9, tzinfo=UTC)


@pytest.mark.parametrize("value", ["2026-03-08T02:30:00", "2026-11-01T01:30:00"])
def test_nonexistent_or_ambiguous_local_dates_are_not_guessed(value: str) -> None:
    result = ConfigurableTenderNormalizer().normalize(
        {"external_tender_id": "1", "deadline": value},
        SourceRecord(id="s", name="Source"),
        settings(timezone="America/New_York"),
    )
    assert result.candidate is None
    assert result.issues[0].field == "deadline"


def test_explicit_ambiguous_fold_selects_requested_instant() -> None:
    raw: dict[str, JsonValue] = {"external_tender_id": "1", "deadline": "2026-11-01T01:30:00"}
    normalizer = ConfigurableTenderNormalizer()
    source = SourceRecord(id="s", name="Source")
    first = normalizer.normalize(
        raw, source, settings(timezone="America/New_York", normalization={"ambiguous_time_fold": 0})
    )
    second = normalizer.normalize(
        raw, source, settings(timezone="America/New_York", normalization={"ambiguous_time_fold": 1})
    )
    assert first.candidate is not None and second.candidate is not None
    assert first.candidate.deadline == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert second.candidate.deadline == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "rules",
    [
        {"decimal_separator": ",", "thousands_separator": ","},
        {"field_mapping": {"invented": "x"}},
        {"date_formats": {"value": ["%Y"]}},
    ],
)
def test_invalid_normalization_rules_fail_at_load(rules: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Configuration.model_validate({"sources": {"s": {"normalization": rules}}})


@pytest.mark.parametrize(
    "month,name",
    list(
        enumerate(
            (
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            ),
            start=1,
        )
    ),
)
def test_exact_english_detail_dates_preserve_raw_values_and_timezone(month: int, name: str) -> None:
    raw: dict[str, JsonValue] = {"external_tender_id": "1", "published_at": f"15 {name} 2026"}
    result = ConfigurableTenderNormalizer().normalize(
        raw, SourceRecord(id="s", name="Source"), settings(timezone="Asia/Tashkent")
    )
    assert result.candidate is not None and result.issues == ()
    assert result.candidate.published_at == datetime(2026, month, 14, 19, tzinfo=UTC)
    assert result.raw_data == raw


@pytest.mark.parametrize(
    "value", ["10/11/2026", "October 15 2026", "15 Oct 2026", "31 February 2026", "15 Unknown 2026"]
)
def test_default_date_parser_does_not_guess_other_human_dates(value: str) -> None:
    result = ConfigurableTenderNormalizer().normalize(
        {"external_tender_id": "1", "published_at": value},
        SourceRecord(id="s", name="Source"),
        SourceSettings(),
    )
    assert result.candidate is None


@pytest.mark.parametrize(
    "normalization",
    [{"date_only_deadline": "reject"}, {"date_formats": {"deadline": ["%Y-%m-%d"]}}],
)
def test_english_date_respects_explicit_formats_and_date_only_policy(
    normalization: dict[str, JsonValue],
) -> None:
    result = ConfigurableTenderNormalizer().normalize(
        {"external_tender_id": "1", "deadline": "15 October 2026"},
        SourceRecord(id="s", name="Source"),
        settings(normalization=normalization),
    )
    assert result.candidate is None


def test_english_date_only_deadline_matches_iso_date_semantics() -> None:
    from tender_system.collection.normalization import parse_date

    policy = settings(timezone="Asia/Tashkent")
    assert parse_date("15 October 2026", "deadline", policy) == parse_date(
        "2026-10-15", "deadline", policy
    )


@pytest.mark.parametrize("reference", [{"url": "/spec.pdf"}, "/spec.pdf", {"missing": "url"}, 42])
def test_invalid_attachment_references_are_never_silently_complete(reference: JsonValue) -> None:
    raw: dict[str, JsonValue] = {
        "external_tender_id": "1",
        "document_urls": ["https://example.org/valid.pdf", reference],
    }
    source = SourceRecord(id="s", name="Source")
    rejected = ConfigurableTenderNormalizer().normalize(raw, source, SourceSettings())
    assert rejected.candidate is None
    assert any(issue.field == "document_urls" for issue in rejected.issues)
    partial = ConfigurableTenderNormalizer().normalize(
        raw, source, settings(normalization={"parse_failure_policy": "null_with_issue"})
    )
    assert partial.candidate is not None
    assert partial.candidate.document_urls == ("https://example.org/valid.pdf",)
    assert partial.raw_data == raw
    assert any(issue.field == "document_urls" for issue in partial.candidate.quality_issues)
