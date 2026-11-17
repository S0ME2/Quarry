"""Independent rendered witnesses behind the SampleVerifier port; no SQL/runtime dependency."""

import json
import re
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import CollectionRun, Reason, SampleVerification, SourceRecord
from tender_system.core.execution import (
    RequestBudgetExceeded,
    ResponseBudgetExceeded,
    remaining_seconds,
)
from tender_system.core.ports import BrowserAdapter, EvidenceStore


class BrowserWitnesses:
    """Independent rendered detail checks; absent witnesses prevent repair activation."""

    def __init__(self, browsers: Callable[[], BrowserAdapter], evidence: EvidenceStore):
        self.browsers, self.evidence = browsers, evidence

    def verify(
        self, run: CollectionRun, source: SourceRecord, settings: SourceSettings
    ) -> SampleVerification:
        try:
            browser = self.browsers()
        except Exception as error:
            reference = self.evidence.put(
                run.id,
                {
                    "kind": "independent_detail_witness_failure",
                    "phase": "browser_startup",
                    "error_type": type(error).__name__,
                },
            )
            return SampleVerification(
                checked=0,
                mismatches=(
                    Reason(
                        code="browser_unavailable",
                        detail=f"Independent browser unavailable; evidence: {reference}",
                    ),
                ),
                evidence_references=(reference,),
            )
        checked = 0
        mismatches: list[Reason] = []
        refs: list[str] = []
        try:
            if settings.authorized_session_reference:
                browser.restore_session(settings.authorized_session_reference)
            for candidate in run.candidates[: settings.repair_checks.sample_size]:
                if (
                    not candidate.source_url
                    or not candidate.external_tender_id
                    or not (candidate.title or candidate.description)
                ):
                    continue
                phase = "navigation"
                content_selector = None
                try:
                    remaining_seconds("independent_verification")
                    browser.navigate(candidate.source_url)
                    phase = "rendered tender content"
                    witness_fields = [
                        field
                        for field in (candidate.title, candidate.description)
                        if field is not None
                    ]
                    if not witness_fields:
                        continue
                    content_selector = ", ".join(
                        f"body:has-text({json.dumps(field, ensure_ascii=False)})"
                        for field in witness_fields
                    )
                    remaining_seconds("independent_verification")
                    browser.wait_for_selector(content_selector)
                    phase = "content capture"
                    content = browser.content()
                    resolved_url = browser.current_url()
                    remaining_seconds("independent_verification")
                except Exception as error:
                    sample = checked + len(mismatches) + 1
                    reference = self.evidence.put(
                        run.id,
                        {
                            "kind": "independent_detail_witness_failure",
                            "sample": sample,
                            "phase": phase,
                            "url": candidate.source_url,
                            "selector": content_selector,
                            "error_type": type(error).__name__,
                            "error": "Independent browser action failed; exception text withheld.",
                        },
                    )
                    refs.append(reference)
                    timed_out = type(error).__name__ == "TimeoutError"
                    outcome = "timed out" if timed_out else "failed"
                    code = "sample_timeout" if timed_out else "sample_check_failed"
                    action = ""
                    if isinstance(error, RequestBudgetExceeded):
                        code = error.code
                        action = (
                            f" Request limit reached ({error.requests_made}/{error.limit}); "
                            "browser pages include asset requests. Increase max_requests "
                            "for this validation run if appropriate."
                        )
                    elif isinstance(error, ResponseBudgetExceeded):
                        code = error.code
                        action = (
                            " Download limit reached; check max_response_bytes and "
                            "max_total_response_bytes for this run."
                        )
                    mismatches.append(
                        Reason(
                            code=code,
                            detail=(
                                f"Independent detail witness {outcome} during {phase} "
                                f"for sample {sample}.{action} Diagnostic evidence: {reference}"
                            ),
                        )
                    )
                    break
                refs.append(
                    self.evidence.put(
                        run.id,
                        {
                            "kind": "independent_detail_witness",
                            "url": candidate.source_url,
                            "content": content,
                        },
                    )
                )
                rendered = " ".join(BeautifulSoup(content, "html.parser").stripped_strings)
                normalized_text = " ".join(rendered.split()).casefold()
                content_match = any(
                    " ".join(field.split()).casefold() in normalized_text
                    for field in (candidate.title, candidate.description)
                    if field
                )
                identity_pattern = rf"(?<![\w]){re.escape(candidate.external_tender_id)}(?![\w])"
                same_host = (
                    urlsplit(resolved_url).hostname == urlsplit(candidate.source_url).hostname
                )
                identity_match = same_host and bool(
                    re.search(identity_pattern, rendered)
                    or re.search(identity_pattern, unquote(urlsplit(resolved_url).path))
                )
                if content_match and identity_match:
                    checked += 1
                else:
                    mismatches.append(
                        Reason(
                            code="sample_mismatch",
                            detail=(
                                f"Sample {checked + len(mismatches) + 1}: rendered content "
                                f"{'matches' if content_match else 'does not match'} "
                                "title/description; "
                                "tender identity "
                                f"{'confirmed' if identity_match else 'not confirmed'}. "
                                f"Inspect rendered evidence: {refs[-1]}"
                            ),
                        )
                    )
        finally:
            browser.close()
        return SampleVerification(
            checked=checked, mismatches=tuple(mismatches), evidence_references=tuple(refs)
        )
