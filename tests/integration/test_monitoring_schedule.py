"""Scheduled collections use the durable operation queue."""

from datetime import UTC, datetime, timedelta
from threading import Thread
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select

from tender_system.core.configuration import SourceOverride, load_configuration
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    OperationRow,
    RecipeRevisionRow,
    SourceRow,
    session_factory,
)
from tender_system.testing.fakes import FakeClock

pytestmark = pytest.mark.database


def test_schedule_is_due_per_source_and_serialized(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        for name in ("one", "two"):
            session.add(SourceRow(id=name, name=name, has_stable_external_id=True))
        session.flush()
        for name in ("one", "two"):
            session.add(RecipeRevisionRow(source_id=name, version="1", revision={}, created_at=now))
        session.flush()
        for name in ("one", "two"):
            session.add(ActiveRecipeRow(source_id=name, version="1"))
    config = load_configuration(environ={"QUARRY_MANUAL_ONLY": "false"}, validate_models=False)
    config = config.model_copy(
        update={"sources": {"one": SourceOverride(check_interval_seconds=10800)}}
    )
    clock = FakeClock(now)
    workers = [OperatorRuntime(sessions, config, clock=clock) for _ in range(2)]
    threads = [Thread(target=worker.schedule_due) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with sessions.begin() as session:
        rows = list(session.scalars(select(OperationRow)))
        assert len(rows) == 2
        assert {row.source_id for row in rows} == {"one", "two"}
        for row in rows:
            row.status = "failed"
            row.finished_at = now
    # A fresh runtime must recover the stored schedule rather than enqueue again.
    workers[0].close()
    restarted = OperatorRuntime(sessions, config, clock=clock)
    assert restarted.schedule_due() == 0
    clock.instant += timedelta(hours=3)
    assert restarted.schedule_due() == 1
    assert workers[1].schedule_due() == 0
    clock.instant += timedelta(hours=1)
    assert workers[1].schedule_due() == 1
    for worker in workers:
        worker.close()
    restarted.close()


def test_busy_and_failed_sources_do_not_block_other_operations(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text

    sessions = session_factory(migrated_engine)
    now = datetime.now(UTC)
    config = load_configuration(environ={"QUARRY_MANUAL_ONLY": "false"}, validate_models=False)
    runtime = OperatorRuntime(sessions, config, clock=FakeClock(now))
    with sessions.begin() as session:
        for source in ("busy", "broken", "healthy"):
            session.add(
                OperationRow(
                    id=uuid4(),
                    kind="collection",
                    source_id=source,
                    status="queued",
                    payload={"scheduled": True},
                    result={},
                    created_at=now,
                )
            )

    def execute(*args: object) -> tuple[dict[str, object], str]:
        if args[2] == "broken":
            raise ValueError("failure")
        return {}, "completed"

    monkeypatch.setattr(runtime, "execute", execute)
    with sessions.begin() as lock:
        lock.execute(
            text("select pg_advisory_xact_lock(:namespace, hashtext(:source))"),
            {"namespace": 1364546124, "source": "busy"},
        )
        assert runtime.tick()
        assert runtime.tick()
        with sessions() as session:
            states = dict(
                session.execute(select(OperationRow.source_id, OperationRow.status)).all()
            )
        assert states == {"busy": "queued", "broken": "failed", "healthy": "completed"}
    assert runtime.tick()
    runtime.close()
