"""Import retained legacy records into an empty, migrated Quarry database.

Stop workers first. Take and restore-test a full pg_dump backup before importing.
The source connection is repeatable-read/read-only; the target import and all
verification run in one transaction. Neither database is created or deleted here.
URLs are read from environment variables and never included in diagnostics.
"""

import argparse
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import Connection, Engine, MetaData, Table, func, inspect, literal, select, text
from sqlalchemy.orm import Session

from tender_system.core.domain import RecipeRevision, TenderCandidate
from tender_system.core.tenders import reconstruct_history, state_of
from tender_system.persistence.matching_storage import SqlCampaignRepository, SqlMatchJobRepository
from tender_system.persistence.storage import (
    CriteriaRow,
    MatchJobRow,
    RecipeRevisionRow,
    SqlTenderRepository,
    build_engine,
)

TABLES = (
    "users",
    "user_sessions",
    "sources",
    "source_runs",
    "recipe_revisions",
    "active_recipes",
    "source_states",
    "tenders",
    "tender_versions",
    "tender_observations",
    "tender_documents",
    "campaigns",
    "campaign_criteria",
    "account_campaigns",
    "account_sources",
    "match_jobs",
    "match_results",
    "match_selections",
    "telegram_notifications",
    "matching_spend",
    "settings",
    "operations",
)
RENAMES = {"settings": "operator_config", "operations": "operator_operations"}


def retained_setting(key: str) -> bool:
    """Only settings read by the current product survive; archives retain the rest."""
    return key.startswith(("source/", "web/source/")) or key in {
        "matching_budget",
        "model/matcher",
        "model/stronger_matcher",
    }


def _json_value(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    raise TypeError(f"Unsupported record type: {type(value).__name__}")


def _digest(rows: Sequence[Mapping[str, Any]]) -> str:
    encoded = sorted(
        json.dumps(dict(row), sort_keys=True, default=_json_value, separators=(",", ":"))
        for row in rows
    )
    return hashlib.sha256("\n".join(encoded).encode()).hexdigest()


def _history_order(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Parents must precede children even when history crosses insert batches."""
    remaining = {row["id"]: row for row in rows}
    ordered: list[Mapping[str, Any]] = []
    inserted: set[Any] = set()
    while remaining:
        ready = [
            row
            for row in remaining.values()
            if row["previous_version_id"] is None or row["previous_version_id"] in inserted
        ]
        if not ready:
            raise ValueError("History contains a missing parent or cycle")
        for row in ready:
            ordered.append(row)
            inserted.add(row["id"])
            del remaining[row["id"]]
    return ordered


def verify_histories(connection: Connection) -> dict[str, int]:
    """Reconstruct every version and observation using production readers."""
    versions = observations = tenders = legacy_product_histories = 0
    # Closing this Session must not commit or roll back the caller's transaction.
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        repository = SqlTenderRepository(session)
        identifiers = cast(
            Sequence[UUID],
            session.execute(text("SELECT id FROM tenders ORDER BY id")).scalars().all(),
        )
        for identifier in identifiers:
            history = repository.history(identifier)
            for version in history:
                reconstruct_history(history, version.id)
            evidence = repository.observations(identifier)
            current = session.execute(
                text("SELECT current_version_id, latest_observation_id FROM tenders WHERE id=:id"),
                {"id": identifier},
            ).one()
            if not history or history[-1].id != current.current_version_id:
                raise ValueError("Tender current version disagrees with reconstructed history")
            stored = repository.get(identifier)
            history_state = reconstruct_history(history)
            reconstructed = TenderCandidate.model_validate(history_state)
            actual = state_of(stored) if stored is not None else {}
            expected = state_of(reconstructed)
            # Product extraction preceded product versioning in legacy deployments.
            # Never rewrite their immutable roots; exact table digests preserve products.
            if "product_items" not in history_state:
                actual.pop("product_items", None)
                expected.pop("product_items", None)
                legacy_product_histories += 1
            if actual != expected:
                raise ValueError("Tender current fields disagree with reconstructed history")
            if not any(item.id == current.latest_observation_id for item in evidence):
                raise ValueError("Tender latest observation is missing")
            versions += len(history)
            observations += len(evidence)
            tenders += 1
    return {
        "tenders": tenders,
        "versions": versions,
        "observations": observations,
        "legacy_product_histories": legacy_product_histories,
    }


def verify_domain_records(connection: Connection) -> dict[str, int]:
    """Read recipe, criteria and matching snapshots through current contracts."""
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        recipes = 0
        for recipe in session.scalars(select(RecipeRevisionRow)):
            parsed = RecipeRevision.model_validate(recipe.revision)
            if (
                parsed.recipe.source_id != recipe.source_id
                or parsed.recipe.version != recipe.version
            ):
                raise ValueError("Recipe payload disagrees with revision identity")
            recipes += 1
        campaigns = SqlCampaignRepository(session)
        criteria = 0
        for version in session.scalars(select(CriteriaRow)):
            campaigns.criteria(version.id)
            criteria += 1
        matches = SqlMatchJobRepository(session)
        jobs = 0
        for job in session.scalars(select(MatchJobRow)):
            matches.get(job.id)
            jobs += 1
        results = len(matches.results())
    return {"recipes": recipes, "criteria": criteria, "jobs": jobs, "results": results}


def import_database(source: Engine, target: Engine) -> dict[str, Any]:
    """Copy exact retained values, reject drift/nonempty targets, validate before commit."""
    report: dict[str, Any] = {"tables": {}}
    with source.connect().execution_options(isolation_level="REPEATABLE READ") as origin:
        with origin.begin():
            origin.execute(text("SET TRANSACTION READ ONLY"))
            source_names = set(inspect(origin).get_table_names())
            with target.begin() as destination:
                identity = text(
                    "SELECT current_database(), current_schema(), "
                    "inet_server_addr(), inet_server_port()"
                )
                if origin.execute(identity).one() == destination.execute(identity).one():
                    raise ValueError("Import requires a different database or schema")
                # Fail promptly if a worker or another import still owns the target.
                destination.execute(text("SET LOCAL lock_timeout = '5s'"))
                # Serialize competing import attempts, then lock every target table.
                destination.execute(text("SELECT pg_advisory_xact_lock(717827643226)"))
                target_names = set(inspect(destination).get_table_names())
                if target_names != set(TABLES) | {"alembic_version"}:
                    raise ValueError("Target must contain only the current migrated schema")
                metadata = MetaData()
                tables = {name: Table(name, metadata, autoload_with=destination) for name in TABLES}
                destination.execute(
                    text(
                        "LOCK TABLE "
                        + ", ".join('"' + name + '"' for name in TABLES)
                        + " IN ACCESS EXCLUSIVE MODE"
                    )
                )
                if any(
                    destination.scalar(select(func.count()).select_from(t)) for t in tables.values()
                ):
                    raise ValueError("Target database must be empty; no records were imported")
                destination.execute(text("SET CONSTRAINTS ALL DEFERRED"))
                for name, table in tables.items():
                    old_name = name if name in source_names else RENAMES.get(name, name)
                    if old_name not in source_names:
                        if name not in {
                            "matching_spend",
                            "tender_documents",
                            "match_selections",
                            "telegram_notifications",
                            "users",
                            "user_sessions",
                            "account_campaigns",
                            "account_sources",
                        }:
                            raise ValueError(f"Required source table missing: {old_name}")
                        rows: list[Mapping[str, Any]] = []
                    else:
                        old = Table(old_name, MetaData(), autoload_with=origin)
                        removed = set(old.c.keys()) - set(table.c.keys())
                        if removed - {"actor_id"}:
                            raise ValueError(f"Unexpected source columns in {old_name}")
                        missing = set(table.c.keys()) - set(old.c.keys())
                        nullable_notification_fields = (
                            {"channel", "destination"}
                            if name == "telegram_notifications"
                            else set()
                        )
                        if missing - nullable_notification_fields:
                            raise ValueError(f"Source schema requires prior migration: {old_name}")
                        rows = [
                            dict(row)
                            for row in origin.execute(
                                select(
                                    *(
                                        old.c[c.name]
                                        if c.name in old.c
                                        else literal(None).label(c.name)
                                        for c in table.c
                                    )
                                )
                            ).mappings()
                        ]
                        if name == "settings":
                            rows = [row for row in rows if retained_setting(row["key"])]
                        if name in {"tender_versions", "campaign_criteria"}:
                            rows = _history_order(rows)
                    # Bound statement size; FK cycles are deferred until final validation.
                    for offset in range(0, len(rows), 250):
                        destination.execute(
                            table.insert(), [dict(r) for r in rows[offset : offset + 250]]
                        )
                    copied = [dict(row) for row in destination.execute(select(table)).mappings()]
                    if len(rows) != len(copied) or _digest(rows) != _digest(copied):
                        raise ValueError(f"Imported records failed exact verification: {name}")
                    report["tables"][name] = {"count": len(rows), "sha256": _digest(rows)}
                report["history"] = verify_histories(destination)
                report["domain_records"] = verify_domain_records(destination)
                destination.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-env", default="LEGACY_DATABASE_URL")
    parser.add_argument("--target-env", default="REBUILD_DATABASE_URL")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    source = target = None
    try:
        control_root = Path(__file__).resolve().parents[1] / ".codex"
        if not args.report.resolve().is_relative_to(control_root):
            raise ValueError("Import report must be inside the project's .codex directory")
        source = build_engine(SecretStr(os.environ[args.source_env]))
        target = build_engine(SecretStr(os.environ[args.target_env]))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation avoids overwriting earlier evidence; output never contains URLs.
        with args.report.open("x", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
            try:
                report = import_database(source, target)
            except Exception:
                json.dump({"status": "failed", "target_transaction": "rolled_back"}, handle)
                raise
            json.dump(report, handle, indent=2)
        print("Import committed; exact row digests and reconstructed history verified.")
    except Exception:
        raise SystemExit(
            "Database import failed. Credentials and record contents omitted."
        ) from None
    finally:
        if source is not None:
            source.dispose()
        if target is not None:
            target.dispose()


if __name__ == "__main__":
    main()
