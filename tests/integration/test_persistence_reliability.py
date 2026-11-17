"""Real PostgreSQL failure boundaries and measured collection hydration budgets."""

import json
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import Connection, Engine, event

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.recipes import RecipeExecutor
from tender_system.collection.source_runs import SourceRunService
from tender_system.core.domain import SourceRecipe, SourceRecord
from tender_system.persistence.storage import (
    SqlSourceRepository,
    SqlTenderRepository,
    TransactionalRunStore,
    session_factory,
)
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.fakes import ScriptedHttpAdapter

pytestmark = pytest.mark.database


@pytest.mark.parametrize("count", [100, 3000])
def test_source_inventory_hydration_has_constant_query_budget(
    migrated_engine: Engine, count: int
) -> None:
    source = SourceRecord(id="hydration", name="Owned PostgreSQL performance fixture")
    rows: list[dict[str, JsonValue]] = [
        {
            "external_tender_id": str(index),
            "title": f"Equipment procurement {index}",
            "product_items": [{"name": "Router", "quantity": 2}],
        }
        for index in range(count)
    ]
    recipe = SourceRecipe.model_validate(
        {
            "source_id": source.id,
            "version": "1",
            "expected_count": count,
            "completion_signal": {"kind": "total_reconciled", "value": count},
            "steps": [
                {
                    "type": "transform",
                    "parameters": {"name": "identity", "input": rows, "emit": True},
                }
            ],
        }
    )
    settings = fixture_source_settings()
    sessions = session_factory(migrated_engine)
    with sessions.begin() as session:
        SqlSourceRepository(session).add(source)
    started = monotonic()
    receipt = SourceRunService(
        RecipeExecutor(ScriptedHttpAdapter(()), MemoryEvidenceStore(), settings),
        ConfigurableTenderNormalizer(),
        TransactionalRunStore(sessions),
    ).run(uuid4(), recipe, source, settings)
    publication_seconds = monotonic() - started
    assert receipt.status == "accepted" and len(receipt.tender_ids) == count
    queries: list[str] = []

    def query(
        _: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: bool
    ) -> None:
        queries.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", query)
    started = monotonic()
    try:
        with sessions() as session:
            tenders = SqlTenderRepository(session).for_source(source.id)
            assert len(tenders) == count
            assert all(tender.raw_data["product_items"] for tender in tenders)
    finally:
        duration = monotonic() - started
        event.remove(migrated_engine, "before_cursor_execute", query)
    phase = "before" if len(queries) > 2 else "after"
    report = Path("artifacts/tests/reliability-audit") / f"hydration-{count}-{phase}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "count": count,
                "query_count": len(queries),
                "hydration_seconds": duration,
                "initial_publication_seconds": publication_seconds,
                "phase": phase,
                "measurement": "local isolated PostgreSQL, source health inventory hydration",
            },
            indent=2,
        )
        + "\n"
    )
    assert len(queries) <= 2, f"Hydrating {count} tenders executed {len(queries)} SQL queries"
    queries.clear()
    event.listen(migrated_engine, "before_cursor_execute", query)
    try:
        with sessions() as session:
            page = SqlTenderRepository(session).scan(None, 100, source.id)
            assert len(page) == 100
            assert all(tender.raw_data["product_items"] for tender in page)
            assert [tender.internal_id for tender in page] == sorted(
                tender.internal_id for tender in tenders
            )[:100]
    finally:
        event.remove(migrated_engine, "before_cursor_execute", query)
    assert len(queries) <= 2, f"Scanning 100 tenders executed {len(queries)} SQL queries"
