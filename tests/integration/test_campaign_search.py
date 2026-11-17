"""Scheduled keyword requests through real HTTP, normalization and PostgreSQL."""

import json
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from test_automatic_matching import campaign, collect, source

from tender_system.core.domain import RecipeRevision, SourceRecipe
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    OperationRow,
    OperatorConfigRow,
    RecipeRevisionRow,
    SourceStateRow,
    TenderRow,
)
from tender_system.testing.web import web_fixture

pytestmark = pytest.mark.database


@pytest.mark.parametrize("supported,unrestricted", [(True, False), (False, False), (True, True)])
def test_scheduled_campaign_search(
    migrated_engine: Engine, supported: bool, unrestricted: bool
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client, include_keywords=["equipment 0", "Network equipment 0"])
        campaign(client, include_keywords=["no match", "equipment 0"])
        campaign(client, include_keywords=["ignored"], source_ids=["other"])
        if unrestricted:
            campaign(client, include_keywords=[])
        searched = supported and not unrestricted
        key = source(client, fixture)
        collect(client, key)
        with fixture.runtime.sessions.begin() as session:
            active = session.get(ActiveRecipeRow, key)
            assert active is not None
            row = session.get(RecipeRevisionRow, (key, active.version))
            assert row is not None
            revision = RecipeRevision.model_validate(row.revision)
            definition = revision.recipe.model_dump(mode="json")
            if supported:
                steps = json.loads(json.dumps(definition["steps"]))
                steps[0]["parameters"]["query"] = {"q": {"$ref": "search_keyword"}}
                definition["search"] = {"steps": steps}
            definition["version"] = "search-v1"
            session.add(
                RecipeRevisionRow(
                    source_id=key,
                    version="search-v1",
                    created_at=fixture.runtime.clock.now(),
                    revision=revision.model_copy(
                        update={"recipe": SourceRecipe.model_validate(definition)}
                    ).model_dump(mode="json"),
                )
            )
            session.flush()
            active.version = "search-v1"
            control = session.get(OperatorConfigRow, f"source/{key}")
            assert control is not None
            control.value = {**control.value, "manual_only": False}
            control.value = {
                k: v for k, v in control.value.items() if k != "approved_execution_limits"
            }
            state = session.get(SourceStateRow, key)
            assert state is not None
            state.checkpoint = {"cursor": "preserve"}
            baseline = state.last_full_count
        fixture.requests.clear()
        fixture.runtime.config = fixture.runtime.config.model_copy(
            update={
                "runtime": fixture.runtime.config.runtime.model_copy(update={"manual_only": False})
            }
        )
        assert fixture.runtime.schedule_due() == 1
        with fixture.runtime.sessions() as session:
            op = session.scalar(
                select(OperationRow).where(OperationRow.payload["scheduled"].as_boolean().is_(True))
            )
            assert op is not None
            operation_id = op.id
            assert set(cast(list[str], op.payload["search_keywords"])) == (
                set()
                if unrestricted
                else {
                    "equipment 0",
                    "Network equipment 0",
                    "no match",
                }
            )
        assert fixture.runtime.tick(operation_id)
        with fixture.runtime.sessions() as session:
            op = session.get(OperationRow, operation_id)
            assert op is not None
            assert op.status == "completed", op.result
            assert op.result["records_processed"] == (1 if searched else 2)
            state = session.get(SourceStateRow, key)
            assert state is not None
            if searched:
                assert state.checkpoint == {"cursor": "preserve"}
                assert state.last_full_count == baseline
            tenders = list(session.scalars(select(TenderRow)))
            assert len(tenders) == 2
            assert all(tender.missing_successful_runs == 0 for tender in tenders)
        from urllib.parse import parse_qs, urlsplit

        queries = [
            parse_qs(urlsplit(path).query).get("q", [""])[0]
            for path in fixture.requests
            if urlsplit(path).path == "/api"
        ]
        assert set(queries) == (
            {"equipment 0", "Network equipment 0", "no match"} if searched else {""}
        )
