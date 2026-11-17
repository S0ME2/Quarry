"""Minimal operator reporting for durable source health evidence; read-only."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import JsonValue

from tender_system.collection.health import evidence_freshness, source_freshness
from tender_system.collection.source_runs import RunReceipt
from tender_system.core.configuration import SourceSettings, load_configuration
from tender_system.core.domain import SourceSnapshot, Tender
from tender_system.persistence.storage import (
    SqlRunStore,
    SqlTenderRepository,
    build_engine,
    database_url_from_environment,
    session_factory,
)


def receipt_report(receipt: RunReceipt) -> dict[str, JsonValue]:
    return {
        "source_id": receipt.source_id,
        "run_id": str(receipt.id),
        "recipe_version": receipt.recipe_version,
        "publication_result": receipt.status,
        "health": receipt.health.model_dump(mode="json")
        if receipt.health
        else {
            "overall_status": "unknown",
            "absence_evidence_allowed": False,
            "reason": "legacy_receipt_unassessed",
        },
        "started_at": receipt.started_at.isoformat() if receipt.started_at else None,
        "finished_at": receipt.finished_at.isoformat() if receipt.finished_at else None,
        "missing_incremented": receipt.missing_incremented,
        "checkpoint_advanced": receipt.checkpoint_advanced,
        "evidence_reference": receipt.evidence_reference,
    }


def source_report(
    source_id: str, snapshot: SourceSnapshot, settings: SourceSettings, now: datetime
) -> dict[str, JsonValue]:
    return {
        "source_id": source_id,
        "policy": settings.calibration.model_dump(mode="json"),
        "accepted_sequence": snapshot.sequence,
        "verified_full_runs": snapshot.accepted_full_runs,
        "freshness": {
            name: decision.model_dump(mode="json")
            for name, decision in source_freshness(snapshot, settings, now).items()
        },
        "last_full_verified_at": snapshot.last_full_verified_at.isoformat()
        if snapshot.last_full_verified_at
        else None,
        "last_incremental_at": snapshot.last_incremental_at.isoformat()
        if snapshot.last_incremental_at
        else None,
    }


def record_report(
    tender: Tender, receipt: RunReceipt | None, now: datetime
) -> dict[str, JsonValue]:
    return {
        "tender_id": str(tender.internal_id),
        "source_id": tender.source_id,
        "last_observed_at": tender.last_seen_at.isoformat(),
        "run_id": str(receipt.id) if receipt else None,
        "policy": receipt.health.policy if receipt and receipt.health else None,
        "freshness": evidence_freshness(receipt.health.freshness, now).model_dump(mode="json")
        if receipt and receipt.health
        else {"status": "unknown", "reason": "record_evidence_unassessed"},
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read source health receipts and freshness evidence."
    )
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run").add_argument("run_id", type=UUID)
    commands.add_parser("source").add_argument("source_id")
    commands.add_parser("tender").add_argument("tender_id", type=UUID)
    args = parser.parse_args()
    config = load_configuration(args.config)
    engine = build_engine(database_url_from_environment(config.global_settings.database_url_env))
    try:
        with session_factory(engine)() as session:
            store = SqlRunStore(session)
            if args.command == "run":
                receipt = store.receipt(args.run_id)
                if receipt is None:
                    parser.error("run receipt not found")
                report = receipt_report(receipt)
            elif args.command == "tender":
                repository = SqlTenderRepository(session)
                tender = repository.get(args.tender_id)
                if tender is None:
                    parser.error("tender not found")
                observation = next(
                    item
                    for item in repository.observations(tender.internal_id)
                    if item.id == tender.latest_observation_id
                )
                receipt = (
                    store.receipt(observation.source_run_id) if observation.source_run_id else None
                )
                report = record_report(tender, receipt, datetime.now(UTC))
            else:
                from tender_system.persistence.storage import SqlSourceRepository

                if SqlSourceRepository(session).get(args.source_id) is None:
                    parser.error("source not found")
                report = source_report(
                    args.source_id,
                    store.snapshot(args.source_id),
                    config.for_source(args.source_id),
                    datetime.now(UTC),
                )
            print(json.dumps(report, indent=2, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
