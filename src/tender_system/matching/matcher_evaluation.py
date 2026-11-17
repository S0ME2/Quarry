"""Explicit offline replay or opt-in live benchmark; never invoked by normal matching."""

import argparse
import json
import math
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, Field, JsonValue

from tender_system.core.configuration import (
    CampaignSettings,
    MatcherRouting,
    ModelSettings,
    load_configuration,
)
from tender_system.core.domain import CampaignCriteria, Contract, MatchResult, NonEmpty, Tender
from tender_system.core.ports import ModelClient, ModelRequest, ModelResponse
from tender_system.matching.intelligent_matching import IntelligentMatcher
from tender_system.matching.matching import settings_digest
from tender_system.matching.model_providers import model_client
from tender_system.testing.fakes import FakeClock

Mode = Literal["broad", "balanced", "strict"]
MODES: tuple[Mode, ...] = ("broad", "balanced", "strict")


class BenchmarkCase(Contract):
    id: NonEmpty
    note: NonEmpty
    tender: dict[str, object]
    campaign: CampaignCriteria
    expected: dict[Mode, Literal["match", "no_match", "unsure"]]
    provenance: dict[str, JsonValue] = Field(default_factory=dict)


class Benchmark(Contract):
    evaluated_at: AwareDatetime
    cases: tuple[BenchmarkCase, ...] = Field(min_length=1)
    labeling: dict[str, JsonValue] = Field(default_factory=dict)


class ReplayClient:
    """Scripted response replay tests policy and routing, not model comprehension."""

    def __init__(self, output: dict[str, object]) -> None:
        self.output = output

    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            output=json.loads(json.dumps(self.output)), model_version="deterministic-replay-v1"
        )


def case_tender(case: BenchmarkCase, now: datetime) -> Tender:
    return Tender.model_validate(
        {
            "source_id": "benchmark",
            "external_tender_id": case.id,
            "internal_id": uuid5(NAMESPACE_URL, case.id),
            "current_version_id": uuid5(NAMESPACE_URL, case.id + "/v1"),
            "first_seen_at": now,
            "last_seen_at": now,
            **case.tender,
        }
    )


def unsure_category(result: MatchResult) -> str:
    """Classify observable cause; never infer an unrecorded HTTP/schema failure."""
    errors = {result.model_calls[-1].error_code} if result.model_calls else set()
    errors.discard(None)
    if errors:
        if "rate_limited" in errors:
            return "http_429"
        if errors & {"invalid_evidence", "insufficient_subject_evidence"}:
            return "citation_evidence_rejection"
        if errors & {
            "provider_output_invalid",
            "malformed_json",
            "malformed_response",
            "invalid_judgment",
            "incomplete_assessment",
            "truncated_output",
            "incomplete_output",
        }:
            return "malformed_schema_output"
        return "other_provider_failure"
    codes = {reason.code for reason in result.uncertainties}
    if codes & {"evidence_verification_failed", "evidence_required"}:
        return "citation_evidence_rejection"
    if codes & {"document_context_incomplete", "unexamined_products"}:
        return "incomplete_context"
    if codes & {"missing_evidence", "critical_missing_information", "unreliable_evidence"}:
        return "insufficient_tender_data"
    if codes & {
        "borderline_relevance",
        "below_decision_threshold",
        "adjacent_scope",
        "partial_scope",
        "inferred_scope",
    }:
        return "uncertainty_guard"
    return "genuine_ambiguity"


def technical_failure(result: MatchResult) -> bool:
    return (
        bool(result.model_calls)
        and result.model_calls[-1].error_code is not None
        or result.evidence_verification is not None
        and result.evidence_verification.status == "invalid"
        or any(reason.code == "evidence_required" for reason in result.uncertainties)
    )


def metrics(rows: list[tuple[str, str, MatchResult]]) -> dict[str, object]:
    count = len(rows)
    calls = sum(call.attempts for _, _, result in rows for call in result.model_calls)
    without = sum(not result.model_calls for _, _, result in rows)
    unsure = sum(result.result == "unsure" for _, _, result in rows)
    definitive_labels = [(expected, result) for _, expected, result in rows if expected != "unsure"]
    known_costs = [
        call.cost for _, _, result in rows for call in result.model_calls if call.cost is not None
    ]
    wrong_confident = sum(r.result != "unsure" and r.result != e for _, e, r in rows)
    technical_unsure = sum(r.result == "unsure" and technical_failure(r) for _, _, r in rows)
    return {
        "cases": count,
        "decision_accuracy": sum(result.result == expected for _, expected, result in rows) / count,
        "match_accuracy": (
            sum(r.result == e for e, r in definitive_labels) / len(definitive_labels)
            if definitive_labels
            else None
        ),
        "false_positives": sum(r.result == "match" and e == "no_match" for _, e, r in rows),
        "false_negatives": sum(r.result == "no_match" and e == "match" for _, e, r in rows),
        "useful_tenders_not_matched": sum(r.result != "match" and e == "match" for _, e, r in rows),
        "definitive_on_uncertain_labels": sum(
            r.result != "unsure" and e == "unsure" for _, e, r in rows
        ),
        "wrong_confident_decisions": wrong_confident,
        "wrong_confident_decision_rate": wrong_confident / count,
        "wrong_confident_rate_among_definitive": wrong_confident / (count - unsure)
        if count != unsure
        else None,
        "unsure_rate": unsure / count,
        "genuine_unsure_rate": sum(
            e == "unsure" and r.result == "unsure" and not technical_failure(r) for _, e, r in rows
        )
        / count,
        "technical_failure_unsure_rate": technical_unsure / count,
        "nontechnical_unsure_rate": (unsure - technical_unsure) / count,
        "useful_matches_lost_to_abstention": sum(
            e == "match" and r.result == "unsure" for _, e, r in rows
        ),
        "unsure_causes": dict(
            Counter(unsure_category(r) for _, _, r in rows if r.result == "unsure")
        ),
        "fraction_resolved_without_ai": sum(
            not r.model_calls and r.result != "unsure" for _, _, r in rows
        )
        / count,
        "fraction_without_ai": without / count,
        "fraction_requiring_ai": (count - without) / count,
        "model_calls_per_100_cases": calls / count * 100,
        "model_errors": sum(
            call.error_code is not None for _, _, r in rows for call in r.model_calls
        ),
        "provider_failures": dict(
            Counter(
                code
                for _, _, r in rows
                for call in r.model_calls
                for code in (call.attempt_errors or ((call.error_code,) if call.error_code else ()))
            )
        ),
        "percentage_resolved_by_rules": sum(
            r.decision_stage == "rules" and r.result != "unsure" for _, _, r in rows
        )
        / count
        * 100,
        "escalation_rate": sum(
            any(call.stage == "escalation" for call in r.model_calls) for _, _, r in rows
        )
        / count,
        "reported_cost": sum(known_costs) if known_costs else None,
        "calls_with_cost_metadata": len(known_costs),
        "decisions": dict(Counter(r.result for _, _, r in rows)),
    }


def evaluate(
    benchmark: Benchmark,
    settings: CampaignSettings,
    clients: dict[str, ModelClient] | None = None,
    *,
    workers: int = 1,
    request_interval: float = 0,
    modes: tuple[Mode, ...] = MODES,
) -> dict[str, object]:
    if not math.isfinite(request_interval) or request_interval < 0:
        raise ValueError("request interval must be finite and nonnegative")
    if request_interval and workers != 1:
        raise ValueError("paced evaluation requires one worker")
    if not modes or any(mode not in MODES for mode in modes):
        raise ValueError("at least one supported matching mode is required")
    timings: dict[tuple[str, Mode], float] = {}
    next_request = 0.0

    def live_provider(model: ModelSettings) -> ModelClient:
        nonlocal next_request
        wait = next_request - monotonic()
        if wait > 0:
            sleep(wait)
        next_request = monotonic() + request_interval
        return model_client(model)

    def run(pair: tuple[BenchmarkCase, Mode]) -> tuple[Mode, str, str, MatchResult]:
        case, mode = pair
        providers = live_provider if clients is None else lambda _: clients[case.id]
        matcher = IntelligentMatcher(settings, FakeClock(benchmark.evaluated_at), providers)
        criteria = case.campaign.model_copy(update={"matching_mode": mode})
        started = monotonic()
        result = matcher.match(case_tender(case, benchmark.evaluated_at), criteria)
        timings[case.id, mode] = monotonic() - started
        if clients is None:
            # Provider recovery/backoff belongs to the adapter. Do not repeat a potentially
            # day-long Retry-After here after its bounded evaluation has already abstained.
            print(f"{case.id}/{mode}: {result.result}", file=sys.stderr, flush=True)
        return mode, case.id, case.expected[mode], result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        outcomes = list(
            executor.map(run, [(case, mode) for case in benchmark.cases for mode in modes])
        )
    rows = [(id, expected, result) for _, id, expected, result in outcomes]
    return {
        "kind": "live" if clients is None else "deterministic_replay",
        "score_calibration": "subjective; no empirical probability calibration",
        "matcher_version": settings.matcher_version,
        "settings_hash": settings_digest(settings),
        "labeling": benchmark.labeling,
        "aggregate": metrics(rows),
        "modes": {
            mode: metrics([(id, e, r) for m, id, e, r in outcomes if m == mode]) for mode in modes
        },
        "cases": [
            {
                "id": id,
                "mode": mode,
                "expected": expected,
                "elapsed_seconds": round(timings[id, mode], 3) if clients is None else None,
                "unsure_cause": unsure_category(result) if result.result == "unsure" else None,
                "outcome": result.model_dump(mode="json"),
            }
            for mode, id, expected, result in outcomes
        ],
    }


def load_env(path: Path) -> None:
    from tender_system.core.environment import load_environment

    load_environment(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark", type=Path, default=Path("benchmarks/matcher/matcher_synthetic_v2.json")
    )
    parser.add_argument(
        "--replay", type=Path, default=Path("benchmarks/matcher/matcher_replay_v2.json")
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--matcher-version", choices=("1", "2", "3"), default="2")
    runtime_config = load_configuration(validate_models=False)
    defaults = runtime_config.global_settings.campaign.model
    parser.add_argument(
        "--provider", choices=("groq", "openrouter", "gemma"), default=defaults.provider
    )
    parser.add_argument("--model", default=defaults.model)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--api-key-env")
    routing = runtime_config.global_settings.campaign.routing
    stronger = routing.stronger_model or ModelSettings()
    parser.add_argument(
        "--stronger-provider",
        choices=("groq", "openrouter", "gemma"),
        default=stronger.provider if routing.escalation_enabled else None,
    )
    parser.add_argument(
        "--stronger-model", default=stronger.model if routing.escalation_enabled else None
    )
    parser.add_argument("--stronger-api-key-env", default=stronger.api_key_env)
    parser.add_argument("--mode", choices=MODES, help="Run one mode to bound live usage")
    parser.add_argument("--max-attempts", type=int, default=defaults.max_attempts)
    parser.add_argument("--max-input-bytes", type=int, default=defaults.max_input_bytes)
    parser.add_argument(
        "--structured-output", choices=("json", "schema"), default=defaults.structured_output
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--request-interval", type=float, default=defaults.request_interval_seconds)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if bool(args.stronger_provider) != bool(args.stronger_model):
        parser.error("escalation needs both stronger provider and stronger model")
    api_key_env = (
        args.api_key_env
        or (defaults.api_key_env if args.provider == defaults.provider else None)
        or {
            "groq": "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "gemma": "GEMMA_API_KEY",
        }.get(args.provider)
    )
    if not 1 <= args.workers <= 4:
        parser.error("workers must be between 1 and 4")
    if (
        not math.isfinite(args.request_interval)
        or args.request_interval < 0
        or (args.request_interval and args.workers != 1)
    ):
        parser.error("request interval must be finite/nonnegative and pacing needs one worker")
    benchmark = Benchmark.model_validate_json(args.benchmark.read_text())
    clients: dict[str, ModelClient] | None = None
    if args.live:
        if not runtime_config.runtime.ai_semantic_enabled:
            parser.error("live AI disabled by QUARRY_AI_SEMANTIC_ENABLED")
        load_env(args.env_file)
        if not api_key_env or not os.environ.get(api_key_env):
            parser.error("live benchmark requires the configured credential environment variable")
    else:
        replay = json.loads(args.replay.read_text())
        clients = {
            case.id: ReplayClient(replay[case.id]) for case in benchmark.cases if case.id in replay
        }
    settings = CampaignSettings(
        matcher_adapter="intelligent",
        matcher_version=args.matcher_version,
        timeout_seconds=60,
        lease_seconds=7000,
        model=ModelSettings(
            provider=args.provider,
            model=args.model,
            timeout_seconds=defaults.timeout_seconds,
            max_output_tokens=defaults.max_output_tokens,
            max_attempts=args.max_attempts,
            max_input_bytes=args.max_input_bytes,
            reasoning_effort=defaults.reasoning_effort,
            request_interval_seconds=defaults.request_interval_seconds,
            api_key_env=api_key_env,
            structured_output=args.structured_output,
        ),
        routing=MatcherRouting(
            escalation_enabled=bool(args.stronger_model),
            stronger_model=ModelSettings(
                provider=args.stronger_provider,
                model=args.stronger_model,
                api_key_env=args.stronger_api_key_env
                or {
                    "groq": "GROQ_API_KEY",
                    "openrouter": "OPENROUTER_API_KEY",
                    "gemma": "GEMMA_API_KEY",
                }[args.stronger_provider],
                timeout_seconds=stronger.timeout_seconds,
                max_output_tokens=stronger.max_output_tokens,
                max_attempts=stronger.max_attempts,
                reasoning_effort=stronger.reasoning_effort,
                request_interval_seconds=stronger.request_interval_seconds,
                structured_output=args.structured_output,
            )
            if args.stronger_model
            else None,
        ),
    )
    report = evaluate(
        benchmark,
        settings,
        clients,
        workers=args.workers,
        request_interval=args.request_interval,
        modes=(args.mode,) if args.mode else MODES,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, indent=2))


if __name__ == "__main__":
    main()
