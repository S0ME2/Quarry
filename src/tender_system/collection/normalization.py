"""Explicit per-source parsing and identity. Never infer a fingerprint or a locale."""

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import unquote_plus, urlsplit
from zoneinfo import ZoneInfo

from pydantic import JsonValue, ValidationError

from tender_system.core.configuration import IdentitySettings, SourceSettings
from tender_system.core.domain import (
    DataQualityIssue,
    NormalizationResult,
    SourceRecord,
    TenderCandidate,
    TenderIdentity,
)
from tender_system.core.registry import Registry


class UrlCanonicalizer(Protocol):
    def canonicalize(self, url: str, settings: IdentitySettings) -> str: ...


class ConservativeUrlCanonicalizer:
    def canonicalize(self, url: str, settings: IdentitySettings) -> str:
        url = url.strip()
        if any(ord(char) < 32 for char in url):
            raise ValueError("URL cannot contain control characters")
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("URL requires absolute HTTP(S) address")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL cannot contain credentials")
        # Access port to reject malformed ports without rewriting an otherwise valid URL.
        _ = parsed.port
        if not settings.drop_query_parameters and not settings.remove_fragment:
            return url
        before_fragment, fragment_separator, fragment = url.partition("#")
        path, _, query = before_fragment.partition("?")
        parts = query.split("&")
        kept = [
            item
            for item in parts
            if unquote_plus(item.split("=", 1)[0]) not in settings.drop_query_parameters
        ]
        if kept != parts:
            before_fragment = path + ("?" + "&".join(kept) if kept else "")
        if settings.remove_fragment:
            return before_fragment
        return before_fragment + fragment_separator + fragment


def localize(value: datetime, settings: SourceSettings) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    zone = ZoneInfo(settings.timezone)
    choices = [value.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
    valid = [
        choice
        for choice in choices
        if choice.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == value
    ]
    if not valid:
        raise ValueError("nonexistent local time")
    if len(valid) == 2 and valid[0].utcoffset() != valid[1].utcoffset():
        fold = settings.normalization.ambiguous_time_fold
        if fold is None:
            raise ValueError("ambiguous local time requires configured fold")
        return choices[fold].astimezone(UTC)
    return valid[0].astimezone(UTC)


def parse_date(raw: JsonValue, field: str, settings: SourceSettings) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("date must be a string in an explicitly supported format")
    text = raw.strip()
    formats = settings.normalization.date_formats.get(field, ())
    value: datetime | None = None
    date_only = False
    if formats:
        for format_string in formats:
            try:
                value = datetime.strptime(text, format_string)
                date_only = not any(
                    directive in format_string for directive in ("%H", "%I", "%M", "%S")
                )
                break
            except ValueError:
                continue
        if value is None:
            raise ValueError("date does not match configured formats")
    else:
        date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))
        if not date_only and not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
            # Public detail pages often display a date while listings expose ISO.
            # Full English month names are unambiguous and independent of host locale.
            months = (
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
            )
            match = re.fullmatch(r"(\d{1,2}) ([A-Z][a-z]+) (\d{4})", text)
            if match is None or match[2] not in months:
                raise ValueError("date requires ISO, English day-month-year or configured formats")
            value = datetime(int(match[3]), months.index(match[2]) + 1, int(match[1]))
            date_only = True
        else:
            value = datetime.fromisoformat(text)
    if date_only:
        policy = settings.normalization.date_only_deadline
        if field == "deadline" and policy == "reject":
            raise ValueError("date-only deadline is disabled")
        clock = time.max if field == "deadline" and policy == "end_of_day" else time.min
        value = datetime.combine(value.date(), clock, value.tzinfo)
    return localize(value, settings)


def parse_money(raw: JsonValue, settings: SourceSettings) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError("money requires a numeric value or configured decimal string")
    if isinstance(raw, (int, float)):
        value = Decimal(str(raw))
        if not value.is_finite() or value < 0:
            raise ValueError("money must be finite and nonnegative")
        return value
    text = str(raw).strip()
    rules = settings.normalization
    for prefix in rules.money_prefixes:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    for suffix in rules.money_suffixes:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    decimal = re.escape(rules.decimal_separator)
    separator = rules.thousands_separator
    integer = r"\d+"
    if separator is not None and separator in text:
        integer = rf"\d{{1,3}}(?:{re.escape(separator)}\d{{3}})+"
    if not re.fullmatch(rf"[+\-]?{integer}(?:{decimal}\d+)?", text):
        raise ValueError("money does not match configured separators/grouping")
    if separator is not None:
        text = text.replace(separator, "")
    try:
        value = Decimal(text.replace(rules.decimal_separator, "."))
    except InvalidOperation:
        raise ValueError("malformed money") from None
    if not value.is_finite() or value < 0:
        raise ValueError("money must be finite and nonnegative")
    return value


class ConfigurableTenderNormalizer:
    def __init__(self, canonicalizers: Registry[UrlCanonicalizer] | None = None) -> None:
        if canonicalizers is None:
            canonicalizers = Registry[UrlCanonicalizer]()
            canonicalizers.register("conservative", ConservativeUrlCanonicalizer())
        self._canonicalizers = canonicalizers

    def normalize(
        self, raw: Mapping[str, JsonValue], source: SourceRecord, settings: SourceSettings
    ) -> NormalizationResult:
        original = deepcopy(dict(raw))
        rules = settings.normalization
        values: dict[str, object] = {}
        issues: list[DataQualityIssue] = []
        for field in (
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
        ):
            value = raw.get(rules.field_mapping.get(field, field))
            if value is None or (isinstance(value, str) and value.strip() in rules.null_values):
                values[field] = () if field == "document_urls" else None
                continue
            try:
                if field in {"published_at", "deadline"}:
                    values[field] = parse_date(value, field, settings)
                elif field == "value":
                    values[field] = parse_money(value, settings)
                elif field == "document_urls":
                    if not isinstance(value, list):
                        raise ValueError("document URLs require a list")
                    urls = []
                    for item in value:
                        url = item.get("url") if isinstance(item, dict) else item
                        try:
                            if not isinstance(url, str):
                                raise ValueError("invalid attachment reference")
                            urls.append(
                                ConservativeUrlCanonicalizer().canonicalize(url, IdentitySettings())
                            )
                        except ValueError:
                            issues.append(
                                DataQualityIssue(
                                    field=field,
                                    code="invalid_document_reference",
                                    detail=(
                                        "Attachment reference requires an absolute HTTP(S) URL; "
                                        "see original payload"
                                    ),
                                    severity="error"
                                    if rules.parse_failure_policy == "reject"
                                    else "warning",
                                )
                            )
                    values[field] = tuple(dict.fromkeys(urls))
                elif field == "product_items":
                    if not isinstance(value, list) or not all(
                        isinstance(item, dict) for item in value
                    ):
                        raise ValueError("product items require a list of objects")
                    values[field] = tuple(deepcopy(value))
                else:
                    if not isinstance(value, str):
                        raise ValueError("text field must be a string")
                    text = (
                        value.strip()
                        if field == "source_url"
                        else unicodedata.normalize("NFC", value).strip()
                    )
                    if rules.collapse_whitespace and field != "source_url":
                        text = " ".join(text.split())
                    if field == "currency":
                        text = text.upper()
                    elif field == "status":
                        text = rules.status_mapping.get(text.casefold(), text.casefold())
                    elif field == "source_url":
                        text = ConservativeUrlCanonicalizer().canonicalize(text, IdentitySettings())
                    values[field] = text
            except (ValueError, OverflowError):
                issues.append(
                    DataQualityIssue(
                        field=field,
                        code="parse_failure",
                        detail="Value violates parsing rules; see original payload",
                        severity="error" if rules.parse_failure_policy == "reject" else "warning",
                    )
                )
                values[field] = () if field == "document_urls" else None
        try:
            identity = self._identity(raw, source, settings, values.get("source_url"))
        except (ValueError, KeyError) as error:
            issues.append(
                DataQualityIssue(field="identity", code="identity_failure", detail=str(error))
            )
            return NormalizationResult(candidate=None, issues=tuple(issues), raw_data=original)
        if any(issue.severity == "error" for issue in issues):
            return NormalizationResult(candidate=None, issues=tuple(issues), raw_data=original)
        try:
            candidate = TenderCandidate.model_validate(
                {
                    **identity.model_dump(),
                    **values,
                    "raw_data": original,
                    "quality_issues": tuple(issues),
                }
            )
        except ValidationError:
            issues.append(
                DataQualityIssue(
                    field="candidate",
                    code="schema_failure",
                    detail="Normalized candidate violates domain contract",
                )
            )
            candidate = None
        return NormalizationResult(candidate=candidate, issues=tuple(issues), raw_data=original)

    def _identity(
        self,
        raw: Mapping[str, JsonValue],
        source: SourceRecord,
        settings: SourceSettings,
        url: object,
    ) -> TenderIdentity:
        if source.has_stable_external_id:
            value = raw.get(
                settings.normalization.field_mapping.get("external_tender_id", "external_tender_id")
            )
            if (
                isinstance(value, bool)
                or not isinstance(value, (str, int))
                or not str(value).strip()
                or (isinstance(value, str) and value.strip() in settings.normalization.null_values)
            ):
                raise ValueError("source requires its stable external tender ID")
            return TenderIdentity(source_id=source.id, external_tender_id=str(value))
        rules = settings.identity
        if rules.url_is_reliable and isinstance(url, str):
            canonicalizer = self._canonicalizers.get(rules.url_canonicalizer)
            key = canonicalizer.canonicalize(url, rules)
            if not key.strip():
                raise ValueError("canonicalizer returned an empty identity")
            return TenderIdentity(
                source_id=source.id,
                source_has_stable_external_id=False,
                fallback_kind="url",
                fallback_key=key,
            )
        if not rules.fingerprint_fields:
            raise ValueError("no reliable URL or explicitly configured stable fingerprint inputs")
        inputs: dict[str, JsonValue] = {}
        for field in rules.fingerprint_fields:
            value = raw.get(field)
            if (
                value is None
                or isinstance(value, (list, dict))
                or (isinstance(value, str) and not value.strip())
            ):
                raise ValueError("fingerprint requires every configured stable scalar input")
            inputs[field] = value
        payload = json.dumps(
            inputs, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        return TenderIdentity(
            source_id=source.id,
            source_has_stable_external_id=False,
            fallback_kind="fingerprint",
            fallback_key="sha256:" + hashlib.sha256(payload.encode()).hexdigest(),
        )
