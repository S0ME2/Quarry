"""Repeatable offline restrictions workload. No database or provider credentials needed."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from tender_system.core.configuration import CampaignSettings
from tender_system.core.domain import CampaignCriteria, Tender
from tender_system.core.ports import ModelRequest, ModelResponse
from tender_system.matching.intelligent_matching import IntelligentMatcher
from tender_system.matching.matching import RulesMatcher
from tender_system.testing.fakes import FakeClock, FunctionModelClient


def main() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    criteria = CampaignCriteria(
        name="Networking",
        include_keywords=("router",),
        exclude_buyers=("Blocked",),
        currencies=("USD",),
        min_value=Decimal(100),
        max_value=Decimal(1000),
    )
    cases: tuple[tuple[dict[str, object], str], ...] = (
        ({}, "match"),
        ({"buyer": "Blocked"}, "no_match"),
        ({"currency": "UZS"}, "no_match"),
        ({"currency": None, "value": 10}, "unsure"),
        ({"value": None}, "unsure"),
        ({"deadline": now - timedelta(seconds=1)}, "no_match"),
        ({"location": None, "deadline": None}, "match"),
    )
    for mode in ("rules", "intelligent"):
        calls: list[ModelRequest] = []

        def respond(request: ModelRequest, calls: list[ModelRequest] = calls) -> ModelResponse:
            calls.append(request)
            return ModelResponse(
                {
                    "decision": "match",
                    "confidence": 0.99,
                    "explanation": "Router explicitly present",
                    "evidence": [{"field": "title", "quote": "Network router"}],
                },
                "fixture-v1",
            )

        settings = CampaignSettings(
            matcher_adapter=mode, matcher_version="3" if mode == "intelligent" else "1"
        )
        matcher = (
            IntelligentMatcher(settings, FakeClock(now), lambda _: FunctionModelClient(respond))
            if mode == "intelligent"
            else RulesMatcher(settings, FakeClock(now))
        )
        started = perf_counter()
        decisions = []
        correct = 0
        for index in range(140):
            fields, expected = cases[index % len(cases)]
            item = Tender.model_validate(
                {
                    "internal_id": uuid5(NAMESPACE_URL, f"workload/{index}"),
                    "current_version_id": uuid5(NAMESPACE_URL, f"version/{index}"),
                    "source_id": "fixture",
                    "external_tender_id": str(index),
                    "title": "Network router",
                    "buyer": "Allowed",
                    "currency": "USD",
                    "value": 500,
                    "deadline": now + timedelta(days=5),
                    "first_seen_at": now,
                    "last_seen_at": now,
                    **fields,
                }
            )
            result = matcher.match(item, criteria)
            decisions.append(result.result)
            correct += result.result == expected
        print(
            json.dumps(
                {
                    "adapter": mode,
                    "candidate_pairs": len(decisions),
                    "matcher_evaluations": len(decisions),
                    "model_calls": len(calls),
                    "hard_rejections": decisions.count("no_match"),
                    "unsure": decisions.count("unsure"),
                    "correct": correct,
                    "seconds": round(perf_counter() - started, 6),
                }
            )
        )


if __name__ == "__main__":
    main()
