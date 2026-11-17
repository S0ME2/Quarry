"""Recipe lookup, source locks and independent validation baselines."""

from collections.abc import Callable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    RecipeRevision,
    SourceSnapshot,
    Tender,
)
from tender_system.core.execution import remaining_seconds
from tender_system.core.ports import SourceExecutor
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    RecipeRevisionRow,
    SqlRunStore,
    SqlTenderRepository,
)


def source_lock(session: Session, source_id: str) -> None:
    remaining = remaining_seconds("source_lock")
    if remaining is not None:
        session.execute(
            text("select set_config('statement_timeout', :timeout, true)"),
            {"timeout": str(max(1, int(remaining * 1000)))},
        )
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended("source:" + source_id, 0)))
    )


class SqlRecipeCatalog:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def active(self, source_id: str) -> RecipeRevision | None:
        with self.sessions() as session:
            active = session.get(ActiveRecipeRow, source_id)
            if active is None:
                return None
            row = session.get(RecipeRevisionRow, (source_id, active.version))
            assert row is not None
            return RecipeRevision.model_validate(row.revision)

    def revision(self, source_id: str, version: str) -> RecipeRevision | None:
        with self.sessions() as session:
            row = session.get(RecipeRevisionRow, (source_id, version))
            return RecipeRevision.model_validate(row.revision) if row is not None else None


class SqlValidationEnvironment:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        executors: Callable[[RecipeRevision, SourceSettings], SourceExecutor],
    ) -> None:
        self.sessions, self.executors = sessions, executors

    def executor(self, revision: RecipeRevision, settings: SourceSettings) -> SourceExecutor:
        return self.executors(revision, settings)

    def baseline(self, source_id: str) -> tuple[SourceSnapshot, tuple[Tender, ...]]:
        with self.sessions() as session:
            return SqlRunStore(session).snapshot(source_id), SqlTenderRepository(
                session
            ).for_source(source_id)
