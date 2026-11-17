"""Explicit terminal operations using local application services; no daemon."""

import argparse
import json
import os
import shlex
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from tender_system.collection.adapters import FileEvidenceStore
from tender_system.collection.recipes import FileRecipeLoader
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import (
    CampaignOverride,
    Configuration,
    ModelSettings,
    load_configuration,
)
from tender_system.core.domain import ExecutionLimits
from tender_system.core.environment import PROJECT_ROOT, read_dotenv
from tender_system.core.ports import ModelClient
from tender_system.matching.matching import (
    manual_matching_settings,
)
from tender_system.matching.model_providers import model_client
from tender_system.operator.operations import (
    OperatorService,
    effective_configuration,
    recover_operations,
    sanitized,
)
from tender_system.operator.operator_contracts import SourceWrite
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.operator.product_contracts import CampaignWrite, PageQuery
from tender_system.operator.recipe_input import ManualCommandError as ManualCommandError
from tender_system.operator.recipe_input import recipe_url as recipe_url
from tender_system.operator.recipe_input import validate_recipe_content as validate_recipe_content
from tender_system.persistence.matching_storage import (
    SqlCampaignRepository,
    SqlMatchJobRepository,
    matching_planner,
)
from tender_system.persistence.storage import (
    OperationRow,
    build_engine,
    database_url_from_environment,
    session_factory,
    validate_test_database,
)

_DETAILS = False
_COMMAND_PREFIX = "uv run --locked quarry-manual"


def emit(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")

    def compact(item: Any) -> Any:
        if isinstance(item, list):
            return [compact(child) for child in item]
        if not isinstance(item, dict) or not isinstance(item.get("result"), dict):
            return item
        return {
            **item,
            "result": {
                key: child
                for key, child in item["result"].items()
                if key
                not in {
                    "proposal",
                    "samples",
                    "approval_witnesses",
                    "provisional_calibration",
                    "fields",
                }
            },
        }

    if not _DETAILS:
        value = compact(value)
    safe = sanitized(value)

    def usage(original: Any, filtered: Any) -> None:
        if isinstance(original, dict) and isinstance(filtered, dict):
            for key, item in original.items():
                if key in {
                    "input_tokens",
                    "output_tokens",
                    "max_model_requests",
                    "max_input_bytes",
                    "max_output_tokens",
                    "provider_attempts_per_request",
                    "input_byte_limit_is_not_a_token_cap",
                } and (item is None or type(item) in {int, bool}):
                    filtered[key] = item
                else:
                    usage(item, filtered.get(key))
        elif isinstance(original, (tuple, list)) and isinstance(filtered, list):
            for item, output in zip(original, filtered, strict=True):
                usage(item, output)

    usage(value, safe)
    if isinstance(safe, dict):
        next_action = safe.get("next")
        if isinstance(next_action, str):
            safe["next"] = next_action.replace("quarry-manual", _COMMAND_PREFIX)
    print(json.dumps(safe, ensure_ascii=False, default=str), flush=True)


def export_recipe(value: Any) -> None:
    validate_recipe_content(value)
    print(json.dumps(value, ensure_ascii=False), flush=True)


def manual_environment(path: Path) -> None:
    """Root dotenv < explicit file < shell. Reject ambiguous database selection."""
    if not path.is_file():
        raise ManualCommandError(
            "environment file missing; use --env-file .env or provision the named file"
        )
    values = {**read_dotenv(PROJECT_ROOT / ".env"), **read_dotenv(path)}
    for key in ("DATABASE_URL", "TEST_DATABASE_URL"):
        if values.get(key) and os.environ.get(key) not in (None, values[key]):
            raise ManualCommandError(
                f"{key} conflicts with environment file; unset shell variable first"
            )
    for key, value in values.items():
        os.environ.setdefault(key, value)


def manual_config(config: Configuration) -> Configuration:
    runtime = config.runtime.model_copy(
        update={
            "manual_only": True,
            "operator_worker_enabled": False,
            "ai_semantic_enabled": False,
        }
    )
    return config.model_copy(update={"runtime": runtime})


def pair(value: str) -> tuple[UUID, UUID]:
    try:
        tender, campaign = value.split(":")
        return UUID(tender), UUID(campaign)
    except ValueError:
        raise argparse.ArgumentTypeError("pair must be TENDER_UUID:CAMPAIGN_UUID") from None


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
        if parsed > 0:
            return parsed
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    p.add_argument("--config", type=Path)
    p.add_argument("--json", action="store_true", help="Include detailed operation reports")
    commands = p.add_subparsers(dest="command")
    commands.add_parser(
        "config", help="Inspect database names and safe runtime flags; no DB connection"
    )
    for name in ("operation", "status", "recipe", "approve", "execute"):
        commands.add_parser(name).add_argument("id", type=UUID)
    doctor_parser = commands.add_parser(
        "doctor", help="Check environment, database identity and migrations without writes"
    )
    doctor_parser.add_argument("--expected-database")
    doctor_parser.add_argument("--browser", action="store_true", help="Require installed Chromium")
    commands.add_parser(
        "source-enable", help="Enable an approved source for manual collection"
    ).add_argument("source")
    commands.add_parser("validate").add_argument("path", type=Path)
    recipe_import = commands.add_parser(
        "recipe-import", help="Stage an external recipe for explicit validation and approval"
    )
    recipe_import.add_argument("path", type=Path)
    import_mode = recipe_import.add_mutually_exclusive_group()
    import_mode.add_argument(
        "--replace", type=UUID, help="Replace a pending import and revoke its validation"
    )
    import_mode.add_argument(
        "--revalidate",
        metavar="SOURCE_ID",
        help="Stage the unchanged active recipe for explicit collection scope approval",
    )
    commands.add_parser(
        "recipe-reject", help="Reject a pending manual recipe import so it can be superseded"
    ).add_argument("id", type=UUID)
    validation = commands.add_parser(
        "recipe-validate", help="Independently validate a full recipe or bounded pilot"
    )
    validation.add_argument("id", type=UUID)
    validation.add_argument("--max-tenders", type=positive_int, metavar="N")
    validation.add_argument("--max-requests", type=int, default=10000)
    validation.add_argument("--max-seconds", type=float, default=600)
    validation.add_argument("--max-response-bytes", type=positive_int, default=10000000)
    validation.add_argument("--max-total-response-bytes", type=positive_int, default=100000000)
    smoke = commands.add_parser(
        "recipe-smoke", help="Run a bounded, non-validating sample of a staged recipe"
    )
    smoke.add_argument("id", type=UUID)
    smoke.add_argument("--max-tenders", type=int, default=10, choices=range(1, 101))
    commands.add_parser("evidence").add_argument("reference")
    collection = commands.add_parser("collect")
    collection.add_argument("source")
    collection.add_argument("--max-tenders", type=positive_int, metavar="N")
    collection.add_argument("--max-requests", type=int, default=10000)
    collection.add_argument("--max-seconds", type=float, default=600)
    collection.add_argument("--max-response-bytes", type=positive_int, default=10000000)
    collection.add_argument("--max-total-response-bytes", type=positive_int, default=100000000)
    commands.add_parser("sources")
    commands.add_parser("runs").add_argument("--source")
    commands.add_parser("operations")
    commands.add_parser("history").add_argument("id", type=UUID)
    tenders = commands.add_parser("tenders")
    tenders.add_argument("--limit", type=int, default=20)
    tenders.add_argument("--after", type=UUID)
    tenders.add_argument("--source")
    commands.add_parser("campaign-create").add_argument("path", type=Path)
    commands.add_parser("campaigns")
    commands.add_parser("results").add_argument("campaign", type=UUID)
    commands.add_parser("jobs").add_argument("campaign", type=UUID)
    rules = commands.add_parser("rules", help="Process only explicitly selected pairs")
    rules.add_argument("--pair", type=pair, action="append", required=True)
    rules.add_argument("--max-evaluations", type=int, default=1, choices=range(1, 6))
    rules.add_argument("--new-evaluation", action="store_true")
    qwen = commands.add_parser("qwen", help="Evaluate one explicitly selected pair with Qwen")
    qwen.add_argument("--pair", type=pair, action="append", required=True)
    qwen.add_argument("--max-evaluations", type=int, default=1, choices=(1,))
    qwen.add_argument(
        "--new-evaluation",
        action="store_true",
        help="Create a new result for this pair even if its prior job completed",
    )
    return p


def selected_config(
    config: Configuration,
    pairs: tuple[tuple[UUID, UUID], ...],
    ai: bool,
    *,
    new_evaluation: bool = False,
) -> Configuration:
    """Apply safety after persisted scope resolution without replacing campaign policies."""
    overrides = dict(config.campaigns)
    for campaign in dict.fromkeys(campaign for _, campaign in pairs):
        settings = config.for_campaign(str(campaign))
        if ai and (settings.model.provider != "groq" or settings.model.model != "qwen/qwen3.8-27b"):
            raise ManualCommandError(
                "qwen requires Groq model qwen/qwen3.8-27b in the effective campaign settings"
            )
        if ai and (
            not settings.model.credential_env or not os.environ.get(settings.model.credential_env)
        ):
            raise ManualCommandError(
                "qwen requires the configured Groq API credential in the selected environment"
            )
        settings = manual_matching_settings(settings, ai, uuid4() if new_evaluation else None)
        overrides[str(campaign)] = CampaignOverride.model_validate(settings.model_dump())
    return config.model_copy(
        update={
            "campaigns": overrides,
            "runtime": config.runtime.model_copy(update={"ai_semantic_enabled": ai}),
        }
    )


def dispatch(
    args: argparse.Namespace,
    sessions: sessionmaker[Session],
    config: Configuration,
    runtime: OperatorRuntime | None = None,
    providers: Callable[[ModelSettings], ModelClient] = model_client,
    qwen_confirmed: bool = False,
) -> int:
    global _COMMAND_PREFIX
    _COMMAND_PREFIX = "uv run --locked quarry-manual --env-file " + shlex.quote(str(args.env_file))
    if args.config:
        _COMMAND_PREFIX += " --config " + shlex.quote(str(args.config))
    clock = SystemClock()
    pairs = tuple(args.pair) if args.command in {"rules", "qwen"} else ()
    operation = None
    with sessions.begin() as session:
        if args.command in {"status", "operation", "operations"}:
            recover_operations(session, clock.now())
        effective = manual_config(effective_configuration(session, config))
        if args.command in {"rules", "qwen", "results", "jobs"}:
            for campaign in (
                [args.campaign] if args.command in {"results", "jobs"} else [c for _, c in pairs]
            ):
                if SqlCampaignRepository(session).get(campaign) is None:
                    raise LookupError("campaign unavailable")
            if args.command in {"results", "jobs"}:
                match_repository = SqlMatchJobRepository(session)
                if args.command == "jobs":
                    emit(
                        [
                            job.model_dump(mode="json")
                            for job in match_repository.jobs(campaign_id=args.campaign)
                        ]
                    )
                    return 0
                emit(
                    [
                        r.model_dump(mode="json")
                        for r in match_repository.results(campaign_id=args.campaign)
                    ]
                )
                return 0
            effective = selected_config(
                effective,
                pairs,
                args.command == "qwen",
                new_evaluation=args.new_evaluation,
            )
            emit(
                {
                    "preflight_estimate": {
                        "model": effective.for_campaign(str(pairs[0][1])).model.model,
                        "selected_tenders": len(pairs),
                        "maximum_model_requests": 1,
                        "maximum_output_tokens": min(
                            effective.for_campaign(str(pairs[0][1])).model.max_output_tokens or 600,
                            600,
                        ),
                        "request_interval_seconds": effective.for_campaign(
                            str(pairs[0][1])
                        ).model.request_interval_seconds,
                        "automatic_retries": False,
                        "provider_cost": "not available before provider usage is returned",
                    }
                    if args.command == "qwen"
                    else None,
                    "scope": [
                        {
                            "tender": str(t),
                            "campaign": str(c),
                            "model": effective.for_campaign(str(c)).model.model
                            if args.command == "qwen"
                            else None,
                        }
                        for t, c in pairs
                    ],
                    "max_evaluations": args.max_evaluations,
                    "model_attempts": 1,
                    "escalation": False,
                }
            )
            if args.command == "qwen" and not qwen_confirmed:
                try:
                    answer = input("Proceed with this Qwen request budget? [y/N]: ").strip().lower()
                except EOFError:
                    answer = ""
                if answer != "y":
                    emit({"qwen": "cancelled", "provider_called": False})
                    return 0
            jobs = matching_planner(session, effective).for_pairs(pairs, clock.now())
        elif args.command == "evidence":
            store = (
                runtime.evidence
                if runtime
                else FileEvidenceStore(effective.runtime.evidence_directory)
            )
            emit(store.get(args.reference))
            return 0
        elif args.command in {"campaign-create", "campaigns"}:
            svc = OperatorService(session, effective, clock.now())
            if args.command == "campaign-create":
                emit(svc.write_campaign(CampaignWrite.model_validate_json(args.path.read_bytes())))
            else:
                emit(svc.campaigns(PageQuery()))
            return 0
        else:
            svc = OperatorService(session, effective, clock.now())
            if args.command == "recipe-import":
                recipe = FileRecipeLoader().load(str(args.path))
                validate_recipe_content(recipe.model_dump(mode="json"))
                url = recipe_url(recipe)
                operation = (
                    svc.replace_recipe(args.replace, recipe, url)
                    if args.replace
                    else svc.import_recipe(recipe, url, revalidate_source=args.revalidate)
                )
                emit(
                    {
                        **operation.model_dump(mode="json"),
                        "recipe_version": recipe.version,
                        "recipe_schema_validated": True,
                        "validation_passed": False,
                        "next": f"quarry-manual recipe-validate {operation.id}",
                    }
                )
                return 0
            elif args.command == "recipe-reject":
                imported = svc.operation(args.id)
                if imported.result.get(
                    "manual_recipe_import"
                ) is not True or imported.status not in ("awaiting_approval", "failed"):
                    raise ManualCommandError("only a pending manual recipe import can be rejected")
                operation = svc.act(args.id, "reject")
                emit(
                    {
                        "id": str(operation.id),
                        "status": operation.status,
                        "manual_recipe_import": True,
                    }
                )
                return 0
            elif args.command == "recipe-validate":
                imported = svc.operation(args.id)
                if imported.result.get(
                    "manual_recipe_import"
                ) is not True or imported.status not in ("awaiting_approval", "failed"):
                    raise ManualCommandError("recipe import is not available for validation")
                operation = svc.enqueue(
                    "recipe_validation",
                    imported.source_id,
                    {
                        "operation_id": str(imported.id),
                        "limits": ExecutionLimits(
                            max_tenders=args.max_tenders,
                            max_requests=args.max_requests,
                            max_response_bytes=args.max_response_bytes,
                            max_total_response_bytes=args.max_total_response_bytes,
                            max_duration_seconds=min(
                                args.max_seconds, effective.runtime.operation_timeout_seconds
                            ),
                        ).model_dump(mode="json"),
                    },
                )
            elif args.command == "recipe-smoke":
                imported = svc.operation(args.id)
                if imported.result.get(
                    "manual_recipe_import"
                ) is not True or imported.status not in ("awaiting_approval", "failed"):
                    raise ManualCommandError("recipe import is not available for smoke execution")
                operation = svc.enqueue(
                    "recipe_smoke",
                    imported.source_id,
                    {"operation_id": str(imported.id), "max_tenders": args.max_tenders},
                )
            elif args.command == "approve":
                operation = svc.act(args.id, "approve")
            elif args.command == "operations":
                emit([op.model_dump(mode="json") for op in svc.operations()])
                return 0
            elif args.command == "history":
                from tender_system.persistence.storage import SqlTenderRepository

                repository = SqlTenderRepository(session)
                emit(
                    {
                        "versions": [
                            v.model_dump(mode="json") for v in repository.history(args.id)
                        ],
                        "observations": [
                            o.model_dump(mode="json", exclude={"raw_data"})
                            for o in repository.observations(args.id)
                        ],
                    }
                )
                return 0
            elif args.command == "source-enable":
                emit(svc.configure_source(args.source, SourceWrite(enabled=True, manual_only=True)))
                return 0
            elif args.command == "collect":
                operation = svc.collect(
                    args.source,
                    ExecutionLimits(
                        max_tenders=args.max_tenders,
                        max_requests=args.max_requests,
                        max_response_bytes=args.max_response_bytes,
                        max_total_response_bytes=args.max_total_response_bytes,
                        max_duration_seconds=min(
                            args.max_seconds, effective.runtime.operation_timeout_seconds
                        ),
                    ),
                )
            elif args.command == "execute":
                operation = svc.operation(args.id)
                if operation.kind not in {"collection", "activation"}:
                    raise ManualCommandError("execute permits collection/activation only")
            elif args.command in {"operation", "status", "recipe"}:
                op = svc.operation(args.id)
                if args.command == "recipe":
                    export_recipe(svc.generated_recipe(args.id).model_dump(mode="json"))
                    print(
                        f"Next: {_COMMAND_PREFIX} status {args.id}; approve only when "
                        "validation_passed is true.",
                        file=sys.stderr,
                    )
                else:
                    emit(
                        {
                            **op.model_dump(mode="json"),
                            "next": (
                                f"quarry-manual approve {args.id}"
                                if args.command == "status"
                                and op.status == "awaiting_approval"
                                and (op.result.get("validation_passed") is True)
                                else f"quarry-manual recipe-validate {args.id}"
                                if args.command == "status"
                                and op.status in {"awaiting_approval", "failed"}
                                and op.result.get("manual_recipe_import") is True
                                else f"quarry-manual recipe {args.id}"
                                if op.status == "awaiting_approval"
                                else (
                                    "Inspect result.validation_passed and result.warnings; "
                                    "correct the issue before retrying."
                                )
                                if op.status == "failed"
                                else f"quarry-manual source-enable {shlex.quote(str(op.source_id))}"
                                if op.status == "activated" and op.result.get("manual_only") is True
                                else f"quarry-manual collect {shlex.quote(str(op.source_id))}"
                                if op.status == "activated" and op.source_id
                                else "quarry-manual status " + str(args.id)
                            ),
                        }
                    )
                return 0
            elif args.command == "tenders":
                emit(svc.stored_tenders(args.limit, args.after, args.source))
                return 0
            elif args.command == "sources":
                emit(svc.sources())
                return 0
            elif args.command == "runs":
                emit(svc.runs(args.source))
                return 0
    if pairs:
        from tender_system.operator.automatic_matching import run_jobs

        started = time.monotonic()
        matching_runtime = OperatorRuntime(sessions, effective, clock=clock, models=providers)
        try:
            report, state = run_jobs(
                matching_runtime, uuid4(), tuple(j.id for j in jobs), automatic=False
            )
        finally:
            matching_runtime.close()
        emit({**report, "duration_seconds": round(time.monotonic() - started, 3)})
        with sessions() as session:
            rows = SqlMatchJobRepository(session).results(job_ids=tuple(j.id for j in jobs))
            emit([r.model_dump(mode="json") for r in rows])
        return int(state == "failed")
    owned_runtime = runtime is None
    assert operation is not None
    # Persisted operation ID is visible before external work starts, including interrupted runs.
    execution_plan_limits = (
        {
            "max_tenders": args.max_tenders,
            "max_requests": args.max_requests,
            "max_response_bytes": args.max_response_bytes,
            "max_total_response_bytes": args.max_total_response_bytes,
            "max_duration_seconds": min(
                args.max_seconds, effective.runtime.operation_timeout_seconds
            ),
        }
        if args.command in {"collect", "recipe-validate"}
        else None
    )
    plan: dict[str, Any] = {}
    if operation.kind == "activation":
        with sessions() as session:
            queued_row = session.get(OperationRow, operation.id)
            origin = (
                session.get(OperationRow, UUID(str(queued_row.payload["operation_id"])))
                if queued_row
                else None
            )
            approved = origin.result.get("execution_limits") if origin else None
            scope = origin.result.get("validation_scope") if origin else None
        approval_limits = (
            ExecutionLimits.model_validate(approved) if approved else ExecutionLimits()
        )
        plan = {
            "execution_plan": {
                "scope": "fresh witnesses for " + str(scope or "validated scope"),
                "full_refresh_possible": scope == "full",
                "max_recipe_http_attempts": approval_limits.max_requests,
                "max_response_bytes": approval_limits.max_response_bytes,
                "max_total_response_bytes": approval_limits.max_total_response_bytes,
                "max_duration_seconds": min(
                    approval_limits.max_duration_seconds,
                    effective.runtime.operation_timeout_seconds,
                ),
                "source_configuration_writes": True,
                "tender_writes": False,
                "absence_changes": False,
                "browser_requests": "separate per browser session; same request/byte caps apply",
                "max_browser_session_requests": approval_limits.max_requests,
            }
        }
    if operation.kind in {"collection", "recipe_validation"} and isinstance(
        execution_plan_limits, dict
    ):
        bounded = execution_plan_limits.get("max_tenders") is not None
        plan = {
            "execution_plan": {
                "scope": "sampled" if bounded else "declared recipe scope",
                "max_tenders": execution_plan_limits.get("max_tenders"),
                "max_recipe_http_attempts": execution_plan_limits.get("max_requests"),
                "max_response_bytes": execution_plan_limits.get("max_response_bytes"),
                "max_total_response_bytes": execution_plan_limits.get("max_total_response_bytes"),
                "max_duration_seconds": execution_plan_limits.get("max_duration_seconds"),
                "browser_requests": "separate per browser session; same request/byte caps apply",
                "max_browser_session_requests": execution_plan_limits.get("max_requests"),
                "tender_writes": operation.kind == "collection",
                "absence_changes": False
                if bounded or operation.kind == "recipe_validation"
                else "only when full inventory, consistency and freshness evidence authorize",
            }
        }
    emit({"queued_operation": str(operation.id), "kind": operation.kind, **plan})
    runtime = runtime or OperatorRuntime(sessions, effective)
    try:
        runtime.tick(operation.id)
        with sessions() as session:
            op = OperatorService(session, effective, clock.now()).operation(operation.id)
            emit(
                {
                    **op.model_dump(mode="json"),
                    "next": (
                        f"quarry-manual recipe {op.id}"
                        if op.status == "awaiting_approval"
                        else f"quarry-manual source-enable {shlex.quote(str(op.source_id))}"
                        if op.status == "activated" and op.result.get("manual_only") is True
                        else f"quarry-manual collect {shlex.quote(str(op.source_id))}"
                        if op.status == "activated" and op.source_id
                        else f"quarry-manual status {op.id}"
                    ),
                    "action": (
                        "HTTP 429: wait for provider quota reset, then retry with lower "
                        "--max-model-requests or smaller budgets."
                        if op.error_code in {"rate_limited", "provider_rate_limited"}
                        else (
                            "Inspect result.validation_passed, result.warnings, and "
                            "evidence references."
                        )
                        if op.status == "failed"
                        else "Review the validated recipe, then approve manually."
                        if op.status == "awaiting_approval"
                        else "Inspect stored records and run receipts."
                    ),
                }
            )
            return int(op.status not in {"completed", "awaiting_approval", "activated"})
    finally:
        if owned_runtime:
            runtime.close()


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    global _DETAILS
    _DETAILS = args.json
    if args.command is None:
        p.print_help()
        return 0
    if args.command in {"rules", "qwen"}:
        if len(args.pair) > args.max_evaluations or len(set(args.pair)) != len(args.pair):
            p.error("select unique pairs within --max-evaluations (default 1, maximum 5)")
        if args.command == "qwen" and len(args.pair) != 1:
            p.error("qwen accepts exactly one selected tender/campaign pair")
    try:
        if args.command == "validate":
            recipe = FileRecipeLoader().load(str(args.path))
            emit({"valid": True, "source_id": recipe.source_id, "version": recipe.version})
            return 0
        if args.command == "recipe-import":
            FileRecipeLoader().load(str(args.path))
        manual_environment(args.env_file)
        configured = load_configuration(args.config, validate_models=False)
        if args.command == "doctor":
            return doctor(args, manual_config(configured))
        if args.command == "config":
            config = manual_config(configured)
            database = database_url_from_environment(configured.global_settings.database_url_env)
            test = os.environ.get("TEST_DATABASE_URL")
            if test:
                validate_test_database(test, database.get_secret_value())
            emit(
                {
                    "env_file": str(args.env_file),
                    "database": make_url(database.get_secret_value()).database,
                    "test_database": make_url(test).database if test else None,
                    "runtime": config.runtime.model_dump(mode="json"),
                    "models": {
                        slot: {
                            "provider": model.provider,
                            "model": model.model,
                            "credential_configured": bool(
                                model.api_key_env and os.environ.get(model.api_key_env)
                            ),
                        }
                        for slot, model in (("matcher", config.global_settings.campaign.model),)
                    },
                }
            )
            return 0
        config = configured
        engine = build_engine(
            database_url_from_environment(config.global_settings.database_url_env)
        )
        try:
            with engine.connect() as connection:
                database_readiness(connection, engine.url.database)
            return dispatch(args, session_factory(engine), config)
        finally:
            engine.dispose()
    except ManualCommandError as error:
        emit({"error": "manual_command", "action": str(error)})
        return 1
    except ValidationError as error:
        emit(
            {
                "error": "validation",
                "fields": [
                    {"path": list(item["loc"]), "type": item["type"]}
                    for item in error.errors(include_input=False)
                ],
                "action": "Correct these fields using the recipe/campaign schema.",
            }
        )
        return 1
    except Exception as error:
        # Never print exception text: database/provider errors can contain credentials.
        code = (
            "database_unavailable"
            if type(error).__name__ == "OperationalError"
            else getattr(error, "code", type(error).__name__)
        )
        actions = {
            "approve": "Require awaiting_approval and validation_passed; inspect operation.",
            "collect": "Require enabled source and approved active recipe; inspect sources/runs.",
            "rules": "Require active campaign and valid stored tender.",
            "qwen": "Check Groq Qwen configuration, active campaign and tender; inspect results.",
            "campaign-create": "Use valid CampaignWrite JSON with criteria.",
        }
        action = actions.get(args.command, "Check selected environment and inputs.")
        if isinstance(error, LookupError):
            action = "Check campaign and source existence."
        elif type(error).__name__ == "OperationalError":
            action = "Check DB service, selected database, credentials and migrations privately."
        emit({"error": str(code), "action": action})
        return 1


def database_readiness(connection: Connection, expected: str | None) -> dict[str, Any]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    actual = connection.scalar(text("select current_database()"))
    if actual != expected:
        raise ManualCommandError("Connected database differs from the selected database identity")
    scripts = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    versions = set(connection.scalars(text("select version_num from alembic_version")))
    if versions != set(scripts.get_heads()):
        raise ManualCommandError(
            "Database migrations are pending; review migration status before work"
        )
    return {"database": actual, "migrations_current": True}


def doctor(args: argparse.Namespace, config: Configuration) -> int:
    """Read-only readiness check. Never opens a public page or invokes a model."""
    from datetime import datetime
    from typing import cast

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    checks: list[dict[str, Any]] = []
    engine = None
    try:
        url = database_url_from_environment(config.global_settings.database_url_env)
        selected = make_url(url.get_secret_value()).database
        engine = build_engine(url)
        with engine.connect() as connection:
            actual = connection.scalar(text("select current_database()"))
            expected = args.expected_database or selected
            checks.append(
                {
                    "check": "database_identity",
                    "passed": actual == expected,
                    "database": actual,
                    "expected": expected,
                }
            )
            scripts = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
            versions = set(connection.scalars(text("select version_num from alembic_version")))
            required = set(scripts.get_heads())
            checks.append(
                {
                    "check": "migrations",
                    "passed": versions == required,
                    "current": sorted(versions),
                    "required": sorted(required),
                    "action": "No migration action required."
                    if versions == required
                    else "Review pending migrations and upgrade strategy before any work; "
                    "doctor does not apply migrations.",
                }
            )
            running = connection.execute(
                text("select id, started_at, payload from operations where status = 'running'")
            ).all()
            # Inspect the same bigint advisory-lock namespace as operation_lock without
            # taking locks or recovering operations during this read-only command.
            held = set(
                connection.execute(
                    text(
                        "select classid::bigint, objid::bigint from pg_locks "
                        "where locktype = 'advisory' and granted and objsubid = 1 "
                        "and database = (select oid from pg_database "
                        "where datname = current_database())"
                    )
                ).all()
            )
            abandoned, overdue = [], []
            now = SystemClock().now()
            for operation_id, started_at, payload in running:
                operation_id = cast(UUID, operation_id)
                started_at = cast(datetime | None, started_at)
                lock_key = operation_id.int % (2**63)
                if (lock_key >> 32, lock_key & 0xFFFFFFFF) not in held:
                    abandoned.append(str(operation_id))
                else:
                    duration = config.runtime.operation_timeout_seconds
                    limits = payload.get("limits") if isinstance(payload, dict) else None
                    if isinstance(limits, dict):
                        try:
                            duration = min(
                                duration,
                                ExecutionLimits.model_validate(limits).max_duration_seconds,
                            )
                        except ValidationError:
                            overdue.append(str(operation_id))
                            continue
                    if started_at is None or (now - started_at).total_seconds() > duration:
                        overdue.append(str(operation_id))
            checks.append(
                {
                    "check": "operations",
                    "passed": not abandoned and not overdue,
                    "running": len(running),
                    "healthy_running": len(running) - len(abandoned) - len(overdue),
                    "recoverable_abandoned": sorted(abandoned),
                    "overdue_running": sorted(overdue),
                    "action": "Inspect with operations to recover abandoned workers; "
                    "inspect overdue workers and their receipts before terminating or retrying."
                    if abandoned or overdue
                    else "Running workers hold ownership locks within their duration budget.",
                }
            )
    except Exception as error:
        checks.append(
            {
                "check": "database",
                "passed": False,
                "error": "database_unavailable"
                if type(error).__name__ == "OperationalError"
                else type(error).__name__,
                "action": "Check PostgreSQL, selected database identity and migrations.",
            }
        )
    finally:
        if engine is not None:
            engine.dispose()
    browser_ready = False
    browser_launch_verified = False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser_ready = (
                config.runtime.browser_enabled
                and Path(playwright.chromium.executable_path).is_file()
            )
            if browser_ready and args.browser:
                browser = playwright.chromium.launch(headless=True, timeout=10000)
                try:
                    browser_launch_verified = browser.is_connected()
                    browser_ready = browser_launch_verified
                finally:
                    browser.close()
    except Exception:
        browser_ready = False
    checks.append(
        {
            "check": "browser",
            "passed": browser_ready or not args.browser,
            "available": browser_ready,
            "required": args.browser,
            "launch_verified": browser_launch_verified,
            **(
                {
                    "error_code": "browser_unavailable",
                    "action": "Check Chromium installation and system dependencies; "
                    "doctor requires a successful local launch and makes no website requests.",
                }
                if args.browser and not browser_ready
                else {}
            ),
        }
    )
    passed = all(check["passed"] for check in checks)
    emit(
        {
            "doctor": "PASS" if passed else "FAIL",
            "checks": checks,
            "manual_mode": config.runtime.manual_only,
            "provider_requests": 0,
        }
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
