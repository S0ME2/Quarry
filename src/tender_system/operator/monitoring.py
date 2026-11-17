"""Persist FR-20 totals from run-owned operations and immutable matching results."""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tender_system.persistence.storage import (
    MatchJobRow,
    MatchResultRow,
    OperationRow,
    SourceRunRow,
)


def refresh_monitoring(sessions: sessionmaker[Session], now: datetime) -> None:
    with sessions.begin() as session:
        for parent in session.scalars(
            select(OperationRow).where(OperationRow.kind == "collection").with_for_update()
        ):
            receipt = session.get(SourceRunRow, parent.id)
            children = list(
                session.scalars(
                    select(OperationRow).where(
                        OperationRow.kind == "automatic_matching",
                        OperationRow.payload["source_run_id"].as_string() == str(parent.id),
                    )
                )
            )
            found = (
                set(cast(list[str], receipt.receipt.get("tender_ids", []))) if receipt else set()
            )
            ids = {
                UUID(str(key))
                for child in children
                for key in cast(list[str], child.payload.get("job_ids", []))
            }
            jobs = (
                list(session.scalars(select(MatchJobRow).where(MatchJobRow.id.in_(ids))))
                if ids
                else []
            )
            results = (
                list(session.scalars(select(MatchResultRow).where(MatchResultRow.job_id.in_(ids))))
                if ids
                else []
            )

            def technical_failure(outcome: dict[str, Any]) -> bool:
                verification = outcome.get("evidence_verification") or {}
                return verification.get("status") == "invalid" or any(
                    call.get("error_code") for call in outcome.get("model_calls", [])
                )

            relevant = {
                str(row.tender_id)
                for row in results
                if row.outcome.get("result") == "match" and not technical_failure(row.outcome)
            } & found
            documents = cast(dict[str, Any], parent.result.get("documents", {}))
            document_errors = sum(row["status"] == "failed" for row in documents.values())
            failed_jobs = {row.id for row in jobs if row.status == "failed"}
            failed_jobs.update(row.job_id for row in results if technical_failure(row.outcome))
            matching_errors = len(failed_jobs)
            pending = (
                parent.status in ("running", "queued")
                or any(child.status in ("running", "queued") for child in children)
                or any(job.status == "running" for job in jobs)
            )
            operation_errors = sum(
                child.status == "failed" and not child.payload.get("job_ids") for child in children
            )
            errors = (
                document_errors
                + matching_errors
                + operation_errors
                + int(parent.status == "failed")
            )
            waiting = sum(job.status == "pending" for job in jobs)
            unresolved = sum(row.outcome.get("result") == "unsure" for row in results)
            status = (
                "running"
                if pending
                else "blocked"
                if waiting
                else "needs_review"
                if unresolved and not errors
                else "partial_failure"
                if errors and found
                else "failed"
                if errors
                else "completed"
            )
            reused = {
                UUID(value)
                for child in children
                for value in cast(list[str], child.payload.get("reused_job_ids", []))
            }
            previous = cast(dict[str, Any], parent.result.get("monitoring", {}))
            parent.result = {
                **parent.result,
                "monitoring": {
                    "source": parent.source_id,
                    "started_at": (parent.started_at or parent.created_at).isoformat(),
                    "completed_at": None
                    if pending or waiting
                    else previous.get("completed_at") or now.isoformat(),
                    "status": status,
                    "found_tenders": len(found),
                    "relevant_tenders": len(relevant),
                    "errors": errors,
                    "pending_evaluations": waiting,
                    "unresolved_evaluations": unresolved,
                    "documents_succeeded": sum(
                        row["status"] == "success" and not row["reused"]
                        for row in documents.values()
                    ),
                    "documents_failed": document_errors,
                    "documents_reused": sum(row["reused"] for row in documents.values()),
                    "matching_results_processed": sum(row.job_id not in reused for row in results),
                    "matching_results_reused": sum(row.job_id in reused for row in results),
                },
            }
