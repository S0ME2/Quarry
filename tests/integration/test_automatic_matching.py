"""Automatic outbox, matching reuse and spending reservations; fake providers only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from tender_system.core.ports import ModelFailure, ModelRequest, ModelResponse
from tender_system.operator.matching_budget import BudgetedClient
from tender_system.operator.web_matching import bounded_config
from tender_system.persistence.matching_storage import SqlMatchJobRepository, matching_planner
from tender_system.persistence.storage import (
    MatchingSpendRow,
    MatchJobRow,
    OperationRow,
    SourceRunRow,
    SqlTenderRepository,
)
from tender_system.testing.fakes import FunctionModelClient
from tender_system.testing.web import WebFixture, web_fixture

pytestmark = pytest.mark.database


def campaign(client: TestClient, **changes: object) -> dict[str, Any]:
    reply = client.post(
        "/api/v1/web/campaigns",
        json={
            "criteria": {
                "name": "Network",
                "include_keywords": ["network"],
                "automatic_matching": True,
                "notifications": {"enabled": True, "channel": "telegram"},
                "ai_budget_usd": 10,
                **changes,
            }
        },
    )
    assert reply.status_code == 201, reply.text
    return dict(reply.json())


def source(client: TestClient, fixture: WebFixture) -> str:
    reply = client.post("/api/v1/web/sources", json={"url": fixture.source_url})
    assert reply.status_code == 201, reply.text
    key = str(reply.json()["source_id"])
    reply = client.post(
        f"/api/v1/web/sources/{key}/recipe",
        json={
            "text": fixture.recipe,
            "max_tenders": 2,
        },
    )
    assert reply.status_code == 202, reply.text
    return key


def collect(client: TestClient, key: str) -> str:
    reply = client.post(f"/api/v1/web/sources/{key}/collect", json={"max_tenders": 2})
    assert reply.status_code == 202, reply.text
    op = client.get("/api/v1/web/operations/" + reply.json()["id"]).json()
    assert op["status"] == "completed", op
    return str(op["id"])


def automatic(fixture: WebFixture) -> list[OperationRow]:
    with fixture.runtime.sessions() as session:
        return list(
            session.scalars(
                select(OperationRow)
                .where(OperationRow.kind == "automatic_matching")
                .order_by(OperationRow.created_at)
            )
        )


def results(client: TestClient, key: str) -> dict[str, Any]:
    page = dict(
        client.get("/api/v1/web/results", params={"campaign_id": key, "view": "current"}).json()
    )
    assert all(
        row["outcome"]["processing_status"] == "finalized"
        and row["outcome"]["relevance"] == "RELEVANT"
        for row in page["items"]
    )
    return page


def test_publication_changes_multiple_campaigns_no_backfill(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        a, b = campaign(client), campaign(client)
        disabled = campaign(client, automatic_matching=False)
        key = source(client, fixture)
        collect(client, key)
        ops = automatic(fixture)
        assert len(ops) == 1
        assert ops[0].status == "completed"
        assert results(client, a["id"])["total"] == results(client, b["id"])["total"] == 2
        assert results(client, disabled["id"])["total"] == 0
        assert not fixture.calls
        collect(client, key)
        assert len(automatic(fixture)) == 2  # Run association reuses existing jobs.
        fixture.changed_title = True
        collect(client, key)
        ops = automatic(fixture)
        assert len(ops) == 3
        fixture.runtime.tick(ops[-1].id)
        assert results(client, a["id"])["total"] == 2
        later = campaign(client)
        assert results(client, later["id"])["total"] == 0
        edited = client.post(
            f"/api/v1/web/campaigns/{a['id']}/criteria",
            json={
                "criteria": {**a["criteria"], "include_keywords": ["equipment"]},
                "expected_version_id": a["current_criteria_version_id"],
            },
        )
        assert edited.status_code == 200
        assert len(automatic(fixture)) == 3
        with fixture.runtime.sessions() as session:
            assert session.scalar(select(func.count()).select_from(MatchJobRow)) == 8


def test_default_disabled_and_manual_independent(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        c = campaign(client, automatic_matching=False, automatic_ai_authorized=False)
        key = source(client, fixture)
        collect(client, key)
        assert automatic(fixture) == []  # Disabled campaigns create no downstream work.
        tender = client.get("/api/v1/web/tenders").json()["items"][0]
        preview = client.post(
            "/api/v1/web/matching/preview",
            json={
                "campaign_id": c["id"],
                "tender_ids": [tender["id"]],
                "mode": "semantic",
            },
        ).json()
        client.post(f"/api/v1/web/matching/{preview['id']}/confirm", json={})
        assert len(fixture.calls) == 1
        assert results(client, c["id"])["total"] == 1


@pytest.mark.parametrize("authorization", [False, True])
def test_authorization_and_run_limits_preserve_pending(
    migrated_engine: Engine, authorization: bool
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        c = campaign(client, automatic_mode="semantic", automatic_ai_authorized=authorization)
        settings = client.get("/api/v1/web/ai-budget").json()["settings"]
        client.put("/api/v1/web/ai-budget", json={**settings, "max_requests_per_run": 1})
        key = source(client, fixture)
        collection = collect(client, key)
        op = automatic(fixture)[0]
        fixture.runtime.tick(op.id)
        state = client.get(f"/api/v1/web/operations/{op.id}").json()
        assert state["status"] == "failed"
        assert state["result"]["pending"] == (1 if authorization else 2)
        assert len(fixture.calls) == (1 if authorization else 0)
        assert client.get(f"/api/v1/web/operations/{collection}").json()["status"] == "completed"
        if authorization:
            reply = client.post(f"/api/v1/web/operations/{op.id}/resume", json={})
            assert reply.status_code == 202
            assert len(fixture.calls) == 2
            assert results(client, c["id"])["total"] == 2
            # Replaying the same publication work does not repeat paid requests.
            fixture.runtime.tick(op.id)
            assert len(fixture.calls) == 2


@pytest.mark.parametrize("scope", ["global", "campaign", "unpriced", "tokens", "run_tokens"])
def test_budget_denial_and_explicit_safe_fallback(migrated_engine: Engine, scope: str) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        c = campaign(
            client,
            automatic_mode="semantic",
            automatic_ai_authorized=True,
            ai_budget_usd=0 if scope == "campaign" else 10,
            ai_request_limit=2,
        )
        settings = client.get("/api/v1/web/ai-budget").json()["settings"]
        if scope == "global":
            settings["global_limit_usd"] = 0
        if scope == "unpriced":
            settings["model"] = "unknown"
        if scope == "tokens":
            settings["max_input_tokens"] = 1
        if scope == "run_tokens":
            settings["max_tokens_per_run"] = 1
        client.put("/api/v1/web/ai-budget", json=settings)
        key = source(client, fixture)
        collect(client, key)
        op = automatic(fixture)[0]
        fixture.runtime.tick(op.id)
        assert not fixture.calls
        assert results(client, c["id"])["total"] == 0
        assert client.get(f"/api/v1/web/operations/{op.id}").json()["result"]["pending"] == 2
        if scope == "unpriced":
            client.put("/api/v1/web/ai-budget", json={**settings, "unpriced_request_limit": 1})
            client.post(f"/api/v1/web/operations/{op.id}/resume", json={})
            assert len(fixture.calls) == 1
            summary = client.get("/api/v1/web/ai-budget").json()
            assert summary["estimated_cost_usd"] is None
            assert summary["provider_reported_cost_usd"] is None
            assert summary["pricing_matches_model"] is False
            assert client.get(f"/api/v1/web/operations/{op.id}").json()["result"]["pending"] == 1


@pytest.mark.parametrize("priced", [True, False])
@pytest.mark.parametrize("explicit_cap", [False, True])
def test_inherited_budget_tracks_current_policy_without_overriding_explicit_caps(
    migrated_engine: Engine, priced: bool, explicit_cap: bool
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(
            client,
            automatic_mode="semantic",
            automatic_ai_authorized=True,
            ai_budget_usd=0 if explicit_cap and priced else None,
            ai_request_limit=0 if explicit_cap and not priced else None,
        )
        settings = client.get("/api/v1/web/ai-budget").json()["settings"]
        settings["global_limit_usd"] = 0
        settings["unpriced_request_limit"] = 0
        if not priced:
            settings["model"] = "unknown"
        client.put("/api/v1/web/ai-budget", json=settings)
        key = source(client, fixture)
        collect(client, key)
        op = automatic(fixture)[0]
        fixture.runtime.tick(op.id)
        assert not fixture.calls
        assert client.get(f"/api/v1/web/operations/{op.id}").json()["result"]["pending"] == 2

        client.put(
            "/api/v1/web/ai-budget",
            json={**settings, "global_limit_usd": 10, "unpriced_request_limit": 10},
        )
        client.post(f"/api/v1/web/operations/{op.id}/resume", json={})
        assert len(fixture.calls) == (0 if explicit_cap else 2)


@pytest.mark.parametrize("scope", ["global", "campaign", "duplicate"])
def test_concurrent_reservations_failures_and_duplicate_requests(
    migrated_engine: Engine, scope: str
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        c = campaign(client, ai_budget_usd=0.006 if scope == "campaign" else 10)
        settings = client.get("/api/v1/web/ai-budget").json()["settings"]
        if scope == "global":
            settings["global_limit_usd"] = 0.006
        client.put("/api/v1/web/ai-budget", json=settings)
        key = source(client, fixture)
        collect(client, key)
        with fixture.runtime.sessions.begin() as session:
            config = bounded_config(fixture.runtime.config, UUID(c["id"]), True)
            tenders = SqlTenderRepository(session).for_source(key)
            jobs = matching_planner(session, config).for_pairs(
                tuple((t.internal_id, UUID(c["id"])) for t in tenders), fixture.runtime.clock.now()
            )
        with fixture.runtime.sessions.begin() as session:
            SqlMatchJobRepository(session).claim_batch(
                fixture.runtime.clock.now(), 2, job_ids=tuple(j.id for j in jobs)
            )
        calls = []

        def fail(request: ModelRequest) -> ModelResponse:
            calls.append(request)
            assert request.max_attempts == 1
            raise ModelFailure("timeout")

        def invoke(job_id: UUID) -> str:
            guarded = BudgetedClient(
                lambda: FunctionModelClient(fail),
                fixture.runtime.sessions,
                job_id,
                UUID(c["id"]),
                uuid4(),
                "groq",
                False,
            )
            try:
                guarded.generate(ModelRequest(model="qwen/qwen3.8-27b", task="test", payload={}))
            except ModelFailure as error:
                return error.code
            raise AssertionError("fake must fail")

        identities = (jobs[0].id, jobs[0].id) if scope == "duplicate" else tuple(j.id for j in jobs)
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(invoke, identities))
        assert sorted(codes) == ["matching_budget_stopped", "timeout"]
        assert len(calls) == 1
        for job_id in identities:
            assert invoke(job_id) == "matching_budget_stopped"
        with fixture.runtime.sessions() as session:
            rows = tuple(session.scalars(select(MatchingSpendRow)))
            assert len(rows) == 1 and rows[0].state == "uncertain"
            assert float(rows[0].accounted_usd) <= 0.006


def test_matching_exception_does_not_undo_collection(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tender_system.operator import automatic_matching

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client)
        key = source(client, fixture)

        def fail(*args: object, **kwargs: object) -> object:
            raise RuntimeError("fake matching failure")

        monkeypatch.setattr(automatic_matching, "run_jobs", fail)
        collection = collect(client, key)
        op = automatic(fixture)[0]
        assert client.get(f"/api/v1/web/operations/{op.id}").json()["status"] == "failed"
        assert client.get(f"/api/v1/web/operations/{collection}").json()["status"] == "completed"
        with fixture.runtime.sessions() as session:
            assert session.get(SourceRunRow, UUID(collection)) is not None
        assert client.get("/api/v1/web/tenders").json()["total"] == 2


def test_hard_restrictions_precede_automatic_ai(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        c = campaign(
            client, automatic_mode="semantic", automatic_ai_authorized=True, currencies=["UZS"]
        )
        key = source(client, fixture)
        collect(client, key)
        assert not fixture.calls
        assert results(client, c["id"])["total"] == 0
        with fixture.runtime.sessions() as session:
            rows = SqlMatchJobRepository(session).results(campaign_id=UUID(c["id"]))
            assert len(rows) == 2 and all(r.outcome.result == "no_match" for r in rows)
        assert automatic(fixture)[0].status == "completed"


def test_provider_errors_do_not_repeat_paid_work_and_force_is_explicit(
    migrated_engine: Engine,
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        c = campaign(client, automatic_mode="semantic", automatic_ai_authorized=True)
        fixture.fail_model = True
        key = source(client, fixture)
        collect(client, key)
        op = automatic(fixture)[0]
        assert op.status == "failed"
        assert len(fixture.calls) == 2
        history = client.get("/api/v1/web/results", params={"campaign_id": c["id"]}).json()
        assert len(history["items"]) == 2
        assert all(
            r["outcome"]["processing_status"] == "failed" and r["outcome"]["relevance"] is None
            for r in history["items"]
        )
        assert all(r.max_attempts == 1 and r.max_output_tokens <= 600 for r in fixture.calls)
        for request in fixture.calls:
            payload_campaign = request.payload["campaign"]
            assert isinstance(payload_campaign, dict)
            assert "automatic_ai_authorized" not in payload_campaign
        client.post(f"/api/v1/web/operations/{op.id}/resume", json={})
        assert len(fixture.calls) == 2
        with fixture.runtime.sessions() as session:
            reservations = tuple(session.scalars(select(MatchingSpendRow)))
            assert len(reservations) == 2
            assert all(
                r.state == "uncertain" and r.accounted_usd == r.reserved_usd for r in reservations
            )
        fixture.fail_model = False
        tender = client.get("/api/v1/web/tenders").json()["items"][0]
        preview = client.post(
            "/api/v1/web/matching/preview",
            json={
                "campaign_id": c["id"],
                "tender_ids": [tender["id"]],
                "mode": "semantic",
                "force_new": True,
            },
        ).json()
        client.post(f"/api/v1/web/matching/{preview['id']}/confirm", json={})
        assert len(fixture.calls) == 3
        assert results(client, c["id"])["total"] == 1


@pytest.mark.parametrize("priced", [True, False])
def test_provider_bound_violation_is_visible_and_stops_further_ai(
    migrated_engine: Engine, priced: bool
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        c = campaign(
            client, automatic_mode="semantic", automatic_ai_authorized=True, ai_request_limit=10
        )
        if not priced:
            settings = client.get("/api/v1/web/ai-budget").json()["settings"]
            settings.update(
                input_usd_per_million=None, output_usd_per_million=None, unpriced_request_limit=10
            )
            assert client.put("/api/v1/web/ai-budget", json=settings).status_code == 200
        fixture.model_behavior = "overrun"
        key = source(client, fixture)
        collect(client, key)
        op = automatic(fixture)[0]
        assert op.status == "failed" and "exceeded" in str(op.result["message"])
        assert len(fixture.calls) == 1 and op.result["pending"] == 1
        assert results(client, c["id"])["total"] == 1
        client.post(f"/api/v1/web/operations/{op.id}/resume", json={})
        assert len(fixture.calls) == 1
        with fixture.runtime.sessions() as session:
            assert session.scalar(select(MatchingSpendRow.state)) == "overrun"


def test_paused_ai_does_not_block_other_campaigns_rules(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        a = campaign(client)
        b = campaign(client, automatic_mode="semantic", automatic_ai_authorized=False)
        c = campaign(client)
        key = source(client, fixture)
        collect(client, key)
        assert not fixture.calls
        assert results(client, a["id"])["total"] == results(client, c["id"])["total"] == 2
        assert results(client, b["id"])["total"] == 0
        op = automatic(fixture)[0]
        assert op.result["completed"] == 4 and op.result["pending"] == 2


def test_unicode_input_bounds_cover_compact_utf8_wire(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        c = campaign(client)
        key = source(client, fixture)
        collect(client, key)
        settings = client.get("/api/v1/web/ai-budget").json()["settings"]
        client.put("/api/v1/web/ai-budget", json={**settings, "max_input_tokens": 10000})
        with fixture.runtime.sessions.begin() as session:
            tender = SqlTenderRepository(session).for_source(key)[0]
            config = bounded_config(fixture.runtime.config, UUID(c["id"]), True)
            job = matching_planner(session, config).for_pairs(
                ((tender.internal_id, UUID(c["id"])),), fixture.runtime.clock.now()
            )[0]
            SqlMatchJobRepository(session).claim_batch(
                fixture.runtime.clock.now(), 1, job_ids=(job.id,)
            )

        def forbidden() -> FunctionModelClient:
            raise AssertionError("No provider may be constructed before input bounds pass")

        guarded = BudgetedClient(
            forbidden, fixture.runtime.sessions, job.id, UUID(c["id"]), uuid4(), "groq", False
        )
        with pytest.raises(ModelFailure, match="matching_budget_stopped"):
            guarded.generate(
                ModelRequest(model="qwen/qwen3.8-27b", task="test", payload={"text": "я" * 4000})
            )
        assert guarded.stopped and "Input token" in guarded.stopped
        with fixture.runtime.sessions() as session:
            assert session.scalar(select(func.count()).select_from(MatchingSpendRow)) == 0


@pytest.mark.parametrize("charge", [None, 0.0, 0.005])
def test_estimates_and_provider_charges_are_distinct(
    migrated_engine: Engine, charge: float | None
) -> None:
    from decimal import Decimal

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client, automatic_mode="semantic", automatic_ai_authorized=True)
        budget = client.get("/api/v1/web/ai-budget").json()["settings"]
        # Pricing identity omitted: bind once to the active server model.
        budget.pop("provider")
        budget.pop("model")
        budget.update(input_usd_per_million=0.8, output_usd_per_million=4.0)
        saved = client.put("/api/v1/web/ai-budget", json=budget)
        assert saved.status_code == 200
        assert saved.json()["model"] == "qwen/qwen3.8-27b"

        def respond(request: ModelRequest) -> ModelResponse:
            return ModelResponse(
                {"decision": "match", "confidence": 0.95, "explanation": "Offline opinion"},
                "offline",
                input_tokens=3864,
                output_tokens=241,
                cost=charge,
            )

        fixture.runtime.models = lambda _: FunctionModelClient(respond)
        key = source(client, fixture)
        collect(client, key)
        fixture.runtime.tick(automatic(fixture)[0].id)
        summary = client.get("/api/v1/web/ai-budget").json()
        assert summary["requests"] == 2
        assert summary["pricing_matches_model"] is True
        assert summary["provider_reported_cost_usd"] == (None if charge is None else charge * 2)
        expected = max(Decimal("0.0040552"), Decimal(str(charge or 0))) * 2
        assert summary["accounted_cost_usd"] == float(expected)
        assert summary["estimated_cost_usd"] == float(expected)


def test_missing_rates_are_explicit_and_legacy_zero_rates_remain_readable() -> None:
    from tender_system.operator.matching_budget import MatchingBudget

    unknown = MatchingBudget()
    assert unknown.input_usd_per_million is None and unknown.output_usd_per_million is None
    legacy = MatchingBudget(input_usd_per_million=0, output_usd_per_million=0)
    assert legacy.input_usd_per_million == legacy.output_usd_per_million == 0


def test_collection_documents_before_matching_and_relevant_visibility(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tender_system.collection.documents import DocumentProcessor
    from tender_system.persistence.storage import TenderDocumentRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        wanted = campaign(client, include_keywords=["photocopier"])
        other = campaign(client, include_keywords=["lifts"])
        fixture.document_urls = ["https://example.org/spec.txt"]
        key = source(client, fixture)
        entered = []

        def process(
            self: DocumentProcessor, ids: tuple[UUID, ...], *args: object, **kwargs: object
        ) -> None:
            entered.append(True)
            # A concurrent poll cannot claim the match before extraction returns.
            assert not fixture.runtime.tick()
            with fixture.runtime.sessions.begin() as session:
                for index, tender_id in enumerate(ids):
                    row = session.get(TenderDocumentRow, (tender_id, fixture.document_urls[0]))
                    if row is None:
                        row = TenderDocumentRow(tender_id=tender_id, url=fixture.document_urls[0])
                        session.add(row)
                    success = index == 0 and len(entered) != 3
                    row.status = "success" if success else "failed"
                    row.extracted_text = (
                        (
                            "Photocopier procurement"
                            if len(entered) in (1, 4)
                            else "Lifts procurement"
                        )
                        if success
                        else None
                    )
                    row.error = None if success else "download failed"
                    row.checked_at = fixture.runtime.clock.now()

        monkeypatch.setattr(DocumentProcessor, "process", process)
        collect(client, key)
        assert entered
        with fixture.runtime.sessions() as session:
            rows = SqlMatchJobRepository(session).results()
            by_campaign: dict[UUID, list[str]] = {UUID(wanted["id"]): [], UUID(other["id"]): []}
            for row in rows:
                by_campaign[row.campaign_id].append(row.outcome.result)
                if row.outcome.result == "unsure":
                    assert row.outcome.processing_status == "needs_review"
                    assert row.outcome.relevance is None
                else:
                    assert row.outcome.processing_status == "finalized"
                    assert row.outcome.relevance in {"RELEVANT", "NOT_RELEVANT"}
            assert sorted(by_campaign[UUID(wanted["id"])]) == ["match", "unsure"]
            assert sorted(by_campaign[UUID(other["id"])]) == ["no_match", "unsure"]
        assert results(client, wanted["id"])["total"] == 1
        assert results(client, other["id"])["total"] == 0
        # Updated attachment content is reevaluated even if the tender itself did not change.
        collect(client, key)
        assert results(client, wanted["id"])["total"] == 0
        assert results(client, other["id"])["total"] == 1
        collect(client, key)
        assert results(client, other["id"])["total"] == 0
        collect(client, key)
        assert results(client, wanted["id"])["total"] == 1


def test_incomplete_documents_skip_paid_requests_and_recover_without_duplicate_calls(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tender_system.collection.documents import DocumentProcessor
    from tender_system.persistence.storage import TenderDocumentRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        wanted = campaign(
            client,
            automatic_mode="semantic",
            automatic_ai_authorized=True,
            notifications={"enabled": False},
        )
        fixture.document_urls = ["https://example.org/spec.txt"]
        key = source(client, fixture)
        recovered = False

        def process(
            self: DocumentProcessor, ids: tuple[UUID, ...], *args: object, **kwargs: object
        ) -> None:
            with fixture.runtime.sessions.begin() as session:
                for tender_id in ids:
                    row = session.get(TenderDocumentRow, (tender_id, fixture.document_urls[0]))
                    if row is None:
                        row = TenderDocumentRow(tender_id=tender_id, url=fixture.document_urls[0])
                        session.add(row)
                    row.status = "success" if recovered else "failed"
                    row.extracted_text = "Network equipment specification" if recovered else None
                    row.error = None if recovered else "corrupt document"
                    row.checked_at = fixture.runtime.clock.now()

        monkeypatch.setattr(DocumentProcessor, "process", process)
        collect(client, key)
        assert not fixture.calls
        assert results(client, wanted["id"])["total"] == 0
        with fixture.runtime.sessions() as session:
            rows = SqlMatchJobRepository(session).results()
            assert len(rows) == 2
            assert all(row.outcome.processing_status == "needs_review" for row in rows)
            assert all(not row.outcome.model_calls for row in rows)
            assert session.scalar(select(func.count()).select_from(MatchingSpendRow)) == 0

        recovered = True
        collect(client, key)
        assert len(fixture.calls) == 2
        assert results(client, wanted["id"])["total"] == 2
        collect(client, key)
        assert len(fixture.calls) == 2
        with fixture.runtime.sessions() as session:
            assert session.scalar(select(func.count()).select_from(MatchingSpendRow)) == 2


def test_document_persistence_failure_cannot_become_a_match_decision(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tender_system.collection.documents import DocumentFailure, DocumentProcessor

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        wanted = campaign(client)
        fixture.document_urls = ["https://example.org/spec.txt"]
        key = source(client, fixture)
        attempts = []

        def fail(
            self: DocumentProcessor, ids: tuple[UUID, ...], *args: object, **kwargs: object
        ) -> None:
            attempts.append(ids)
            raise DocumentFailure("could not persist document processing results")

        monkeypatch.setattr(DocumentProcessor, "process", fail)
        run_id = collect(client, key)
        run = client.get(f"/api/v1/web/operations/{run_id}").json()
        assert run["result"]["document_processing_failed"] is True
        assert len(attempts) == 2
        assert automatic(fixture)[0].status == "failed"
        assert results(client, wanted["id"])["total"] == 0
        with fixture.runtime.sessions() as session:
            assert not SqlMatchJobRepository(session).results()


def test_real_document_processor_feeds_automatic_matching(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tender_system.collection.documents import DocumentProcessor

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        wanted = campaign(client, include_keywords=["photocopier"])
        other = campaign(client, include_keywords=["elevator"])
        fixture.document_urls = ["https://example.org/spec.txt"]
        key = source(client, fixture)
        original_init = DocumentProcessor.__init__
        downloads = []

        def fetch(url: str, headers: object, budget: object) -> tuple[int, bytes, dict[str, str]]:
            downloads.append(url)
            assert not fixture.runtime.tick()
            return 200, b"Photocopier procurement", {"content-type": "text/plain"}

        def init(self: DocumentProcessor, sessions: sessionmaker[Session]) -> None:
            original_init(self, sessions, fetch=fetch)

        monkeypatch.setattr(DocumentProcessor, "__init__", init)
        operation_id = collect(client, key)
        operation = client.get(f"/api/v1/web/operations/{operation_id}").json()
        assert operation["result"]["document_outcomes"] == {
            "succeeded": 2,
            "failed": 0,
            "reused": 0,
        }
        assert len(downloads) == 2
        assert results(client, wanted["id"])["total"] == 2
        assert results(client, other["id"])["total"] == 0
        with fixture.runtime.sessions() as session:
            rows = SqlMatchJobRepository(session).results(campaign_id=UUID(other["id"]))
            assert len(rows) == 2 and all(row.outcome.result == "no_match" for row in rows)


def test_telegram_delivery_documents_campaigns_and_deduplication(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from tender_system.collection.documents import DocumentProcessor
    from tender_system.persistence.storage import TelegramNotificationRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("QUARRY_TELEGRAM_CHAT_ID", "123")
    messages = []

    def send(url: str, *, json: dict[str, str], timeout: float) -> httpx.Response:
        messages.append(json["text"])
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", send)
    original_init = DocumentProcessor.__init__

    def init(self: DocumentProcessor, sessions: sessionmaker[Session]) -> None:
        original_init(
            self,
            sessions,
            fetch=lambda *args: (200, b"photocopier", {"content-type": "text/plain"}),
        )

    monkeypatch.setattr(DocumentProcessor, "__init__", init)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client, name="First", include_keywords=["photocopier"])
        campaign(client, name="Second", include_keywords=["photocopier"])
        campaign(client, name="Rejected", include_keywords=["elevator"])
        fixture.document_urls = ["https://example.org/spec.txt"]
        key = source(client, fixture)
        collect(client, key)
        assert len(messages) == 4
        assert sum("Campaign: First" in m for m in messages) == 2
        assert sum("Campaign: Second" in m for m in messages) == 2
        for message in messages:
            assert all(
                field in message
                for field in (
                    "Tender:",
                    "Buyer:",
                    "Price:",
                    "Currency:",
                    "Deadline:",
                    "Platform:",
                    "Relevance:",
                    "Link:",
                )
            )
        collect(client, key)
        fixture.runtime.tick()
        assert len(messages) == 4
        with fixture.runtime.sessions() as session:
            assert set(session.scalars(select(TelegramNotificationRow.status))) == {"delivered"}


def test_telegram_rejections_retry_and_timeouts_are_uncertain(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    import httpx

    from tender_system.operator.telegram import dispatch_notifications
    from tender_system.persistence.storage import TelegramNotificationRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("QUARRY_TELEGRAM_CHAT_ID", "123")
    calls = []

    def reject(*args: object, **kwargs: object) -> httpx.Response:
        calls.append(True)
        return httpx.Response(429, json={"ok": False})

    monkeypatch.setattr(httpx, "post", reject)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client)
        key = source(client, fixture)
        collect(client, key)
        assert len(calls) == 2
        with fixture.runtime.sessions() as session:
            assert set(session.scalars(select(TelegramNotificationRow.status))) == {"pending"}

        def timeout(*args: object, **kwargs: object) -> httpx.Response:
            calls.append(True)
            raise httpx.ReadTimeout("unknown")

        monkeypatch.setattr(httpx, "post", timeout)
        dispatch_notifications(
            fixture.runtime.sessions, fixture.runtime.clock.now() + timedelta(hours=2)
        )
        assert len(calls) == 4
        dispatch_notifications(
            fixture.runtime.sessions, fixture.runtime.clock.now() + timedelta(hours=4)
        )
        assert len(calls) == 4
        with fixture.runtime.sessions() as session:
            rows = list(session.scalars(select(TelegramNotificationRow)))
            assert all(
                row.status == "uncertain" and row.attempts == 2 and row.last_error for row in rows
            )
        assert client.get("/api/v1/web/tenders").json()["total"] == 2


def test_telegram_confirmed_failure_retries_after_restart(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    import httpx

    from tender_system.operator.telegram import dispatch_notifications
    from tender_system.persistence.storage import TelegramNotificationRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("QUARRY_TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(
        httpx, "post", lambda *args, **kwargs: httpx.Response(503, json={"ok": False})
    )
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client)
        collect(client, source(client, fixture))
        calls = []

        def send(*args: object, **kwargs: object) -> httpx.Response:
            calls.append(True)
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx, "post", send)
        dispatch_notifications(
            fixture.runtime.sessions, fixture.runtime.clock.now() + timedelta(hours=2)
        )
        dispatch_notifications(
            fixture.runtime.sessions, fixture.runtime.clock.now() + timedelta(hours=4)
        )
        assert len(calls) == 2
        with fixture.runtime.sessions() as session:
            rows = list(session.scalars(select(TelegramNotificationRow)))
            assert all(row.status == "delivered" and row.attempts == 2 for row in rows)


def test_telegram_never_sends_when_match_persistence_fails(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from tender_system.operator import telegram
    from tender_system.persistence.storage import TelegramNotificationRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("QUARRY_TELEGRAM_CHAT_ID", "123")
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: calls.append(True))

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("persistence failure")

    monkeypatch.setattr(telegram, "enqueue_notification", fail)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client)
        key = source(client, fixture)
        collect(client, key)
        assert not calls
        assert client.get("/api/v1/web/tenders").json()["total"] == 2
        with fixture.runtime.sessions() as session:
            assert session.scalar(select(func.count()).select_from(TelegramNotificationRow)) == 0


def test_automatic_matching_respects_campaign_sources(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        key = source(client, fixture)
        included = campaign(client, source_ids=[key])
        excluded = campaign(client, source_ids=["another-source"])
        all_sources = campaign(client)
        collect(client, key)
        assert results(client, included["id"])["total"] == 2
        assert results(client, all_sources["id"])["total"] == 2
        assert results(client, excluded["id"])["total"] == 0


def test_worker_automatically_collects_and_matches(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from time import monotonic, sleep

    import httpx

    from tender_system.collection.documents import DocumentProcessor
    from tender_system.persistence.storage import OperatorConfigRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("QUARRY_TELEGRAM_CHAT_ID", "123")
    messages: list[str] = []

    def send(url: str, *, json: dict[str, str], timeout: float) -> httpx.Response:
        messages.append(json["text"])
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", send)
    original_init = DocumentProcessor.__init__

    def init(self: DocumentProcessor, sessions: sessionmaker[Session]) -> None:
        original_init(
            self, sessions, fetch=lambda *args: (200, b"network", {"content-type": "text/plain"})
        )

    monkeypatch.setattr(DocumentProcessor, "__init__", init)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        fixture.document_urls = ["https://example.org/spec.txt"]
        key = source(client, fixture)
        selected = campaign(client, source_ids=[key])
        with fixture.runtime.sessions.begin() as session:
            control = session.get(OperatorConfigRow, f"source/{key}")
            assert control is not None
            control.value = {**control.value, "enabled": True, "manual_only": False}
        settings = fixture.runtime.config.runtime.model_copy(
            update={
                "manual_only": False,
                "operator_worker_enabled": True,
                "worker_poll_seconds": 0.05,
            }
        )
        fixture.runtime.config = fixture.runtime.config.model_copy(update={"runtime": settings})
        fixture.runtime.start()
        try:
            deadline = monotonic() + 10
            while monotonic() < deadline and len(messages) < 2:
                sleep(0.05)
            assert results(client, selected["id"])["total"] == 2
            assert len(messages) == 2
            with fixture.runtime.sessions() as session:
                operation = session.scalar(
                    select(OperationRow).where(
                        OperationRow.kind == "collection",
                        OperationRow.payload["scheduled"].as_boolean().is_(True),
                    )
                )
                assert operation is not None and operation.status == "completed"
        finally:
            fixture.runtime.close()


def test_monitoring_counts_distinct_tenders_and_reused_results(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client)
        campaign(client)
        key = source(client, fixture)
        first = collect(client, key)
        second = collect(client, key)
        with fixture.runtime.sessions() as session:
            first_operation = session.get(OperationRow, UUID(first))
            second_operation = session.get(OperationRow, UUID(second))
            assert first_operation is not None and second_operation is not None
            one = cast(dict[str, Any], first_operation.result["monitoring"])
            two = cast(dict[str, Any], second_operation.result["monitoring"])
            assert one["status"] == two["status"] == "completed"
            assert one["found_tenders"] == two["found_tenders"] == 2
            assert one["relevant_tenders"] == two["relevant_tenders"] == 2
            assert one["errors"] == two["errors"] == 0
            assert two["matching_results_reused"] == 4
            assert two["completed_at"] is not None

        with fixture.runtime.sessions.begin() as session:
            parent = session.get(OperationRow, UUID(second))
            assert parent is not None
            parent.status = "running"
        fixture.runtime.tick()
        with fixture.runtime.sessions() as session:
            operation = session.get(OperationRow, UUID(second))
            assert operation is not None
            recovered = operation.result["monitoring"]
            assert recovered == two


def test_requirement_condition_changes_reach_automatic_matching(migrated_engine: Engine) -> None:
    from tender_system.persistence.storage import TenderVersionRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client, automatic_mode="semantic", automatic_ai_authorized=True)
        fixture.requirements = "Provide network routers with installation"
        fixture.conditions = "Delivery within 30 days"
        key = source(client, fixture)
        collect(client, key)
        assert len(fixture.calls) == 2
        for request in fixture.calls:
            supplied = request.payload["tender"]
            assert isinstance(supplied, dict)
            assert supplied["requirements"] == fixture.requirements
            assert supplied["conditions"] == fixture.conditions
        collect(client, key)
        assert len(fixture.calls) == 2
        fixture.conditions = "Delivery within 90 days"
        last = collect(client, key)
        assert len(fixture.calls) == 4
        supplied = fixture.calls[-1].payload["tender"]
        assert isinstance(supplied, dict) and supplied["conditions"] == fixture.conditions
        with fixture.runtime.sessions() as session:
            assert session.scalar(select(func.count()).select_from(TenderVersionRow)) == 2
            outcomes = SqlMatchJobRepository(session).results()
            assert len(outcomes) == 4 and all(row.outcome.result == "match" for row in outcomes)
            operation = session.get(OperationRow, UUID(last))
            assert operation is not None
            stats = cast(dict[str, Any], operation.result["monitoring"])
            assert stats["found_tenders"] == stats["relevant_tenders"] == 2
            assert stats["matching_results_processed"] == 2 and stats["errors"] == 0


def test_monitoring_document_failures_are_durable(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tender_system.collection.documents import DocumentFailure, DocumentProcessor

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client)
        fixture.document_urls = ["https://example.org/broken.txt"]
        original = DocumentProcessor.__init__

        def fetch(*args: object) -> tuple[int, bytes, dict[str, str]]:
            raise DocumentFailure("extraction failed")

        def init(self: DocumentProcessor, sessions: sessionmaker[Session]) -> None:
            original(self, sessions, fetch=fetch)

        monkeypatch.setattr(DocumentProcessor, "__init__", init)
        first = collect(client, source(client, fixture))
        with fixture.runtime.sessions() as session:
            operation = session.get(OperationRow, UUID(first))
            assert operation is not None
            stats = cast(dict[str, Any], operation.result["monitoring"])
            assert stats["documents_failed"] == 2
            assert stats["errors"] == 2
            assert stats["status"] == "partial_failure"
        fixture.runtime.tick()
        with fixture.runtime.sessions() as session:
            operation = session.get(OperationRow, UUID(first))
            assert operation is not None and operation.result["monitoring"] == stats


def test_telegram_configuration_persists_across_application_restart(
    migrated_engine: Engine,
) -> None:
    from tender_system.api.api import create_app
    from tender_system.persistence.storage import CriteriaRow, session_factory

    settings = {"enabled": True, "channel": "telegram", "chat_id": "-1001234567890", "email": None}
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        created = campaign(client, notifications=settings)
        key = created["id"]
        assert created["criteria"]["notifications"] == settings
        with fixture.runtime.sessions() as session:
            stored = session.get(CriteriaRow, UUID(created["current_criteria_version_id"]))
            assert stored is not None and stored.criteria["notifications"] == settings

    with TestClient(create_app(session_factory(migrated_engine))) as client:
        loaded = client.get(f"/api/v1/web/campaigns/{key}").json()["campaign"]
        assert loaded["criteria"]["notifications"] == settings
        updated = client.post(
            f"/api/v1/web/campaigns/{key}/criteria",
            json={
                "criteria": {**loaded["criteria"], "notifications": {**settings, "enabled": False}},
                "expected_version_id": loaded["current_criteria_version_id"],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["criteria"]["notifications"]["enabled"] is False
        history = client.get(f"/api/v1/web/campaigns/{key}").json()["versions"]
        assert {v["criteria"]["notifications"]["enabled"] for v in history} == {True, False}


def test_telegram_routes_persisted_campaigns_only_after_commit(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from tender_system.operator.telegram import dispatch_notifications
    from tender_system.persistence.storage import MatchResultRow, TelegramNotificationRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("QUARRY_TELEGRAM_CHAT_ID", "")
    sent = []
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        first = campaign(client, name="First", notifications={"chat_id": "123"})
        second = campaign(client, name="Second", notifications={"chat_id": "-456"})
        campaign(client, name="Disabled", notifications={"enabled": False, "chat_id": "789"})
        campaign(client, name="Unconfigured")
        campaign(
            client, name="Rejected", include_keywords=["elevator"], notifications={"chat_id": "123"}
        )

        def send(url: str, *, json: dict[str, str], timeout: float) -> httpx.Response:
            with fixture.runtime.sessions() as session:
                row = session.scalar(
                    select(TelegramNotificationRow).where(
                        TelegramNotificationRow.message == json["text"]
                    )
                )
                assert row is not None and row.status == "uncertain"
                result = session.get(MatchResultRow, row.result_id)
                assert result is not None and result.outcome["result"] == "match"
            assert "Buyer: Public procurement agency" in json["text"]
            assert "Price: 120000" in json["text"]
            assert "Currency: USD" in json["text"]
            assert "Deadline: 2026-12-01" in json["text"]
            assert f"Link: {fixture.source_url}/detail/" in json["text"]
            assert "Relevance: " in json["text"]
            sent.append(json)
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx, "post", send)
        key = source(client, fixture)
        collect(client, key)
        assert sorted(m["chat_id"] for m in sent) == ["-456", "-456", "123", "123"]
        assert all(("Campaign: First" in m["text"]) == (m["chat_id"] == "123") for m in sent)
        assert results(client, first["id"])["total"] == results(client, second["id"])["total"] == 2
        with fixture.runtime.sessions() as session:
            rows = list(session.scalars(select(TelegramNotificationRow)))
            assert len(rows) == 6
            assert sum(r.status == "pending" and r.attempts == 0 for r in rows) == 2
        collect(client, key)
        dispatch_notifications(fixture.runtime.sessions, fixture.runtime.clock.now())
        assert len(sent) == 4


@pytest.mark.parametrize("change", ["tender", "criteria", "inactive"])
def test_telegram_pending_matches_are_rechecked_before_delivery(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import httpx

    from tender_system.operator.telegram import dispatch_notifications
    from tender_system.persistence.storage import TelegramNotificationRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "")
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        saved = campaign(client, notifications={"chat_id": "123"})
        key = source(client, fixture)
        collect(client, key)
        if change == "tender":
            fixture.changed_title = True
            collect(client, key)
        elif change == "criteria":
            reply = client.post(
                f"/api/v1/web/campaigns/{saved['id']}/criteria",
                json={
                    "criteria": {**saved["criteria"], "notifications": {"enabled": False}},
                    "expected_version_id": saved["current_criteria_version_id"],
                },
            )
            assert reply.status_code == 200
        else:
            assert (
                client.patch(
                    f"/api/v1/web/campaigns/{saved['id']}/state", json={"active": False}
                ).status_code
                == 200
            )
        sent = []

        def send(*args: object, **kwargs: Any) -> httpx.Response:
            sent.append(kwargs["json"]["text"])
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx, "post", send)
        monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
        dispatch_notifications(fixture.runtime.sessions, fixture.runtime.clock.now())
        assert len(sent) == (2 if change == "tender" else 0)
        assert all("changed" in message for message in sent)
        with fixture.runtime.sessions() as session:
            rows = list(session.scalars(select(TelegramNotificationRow)))
            assert sum(row.status == "superseded" for row in rows) == 2


def test_telegram_rate_limit_and_concurrent_dispatch(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    import httpx

    from tender_system.operator.telegram import dispatch_notifications
    from tender_system.persistence.storage import TelegramNotificationRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("QUARRY_TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: httpx.Response(
            429, json={"ok": False, "parameters": {"retry_after": 7200}}
        ),
    )
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client, notifications={"chat_id": "123"})
        before = fixture.runtime.clock.now()
        collect(client, source(client, fixture))
        now = fixture.runtime.clock.now()
        with fixture.runtime.sessions() as session:
            rows = list(session.scalars(select(TelegramNotificationRow)))
            assert all(
                before + timedelta(seconds=7200)
                <= row.available_at
                <= now + timedelta(seconds=7200)
                for row in rows
            )
            earliest = min(row.available_at for row in rows)
            latest = max(row.available_at for row in rows)
        sent = []

        def send(*args: object, **kwargs: Any) -> httpx.Response:
            sent.append(kwargs["json"]["text"])
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx, "post", send)
        dispatch_notifications(fixture.runtime.sessions, earliest - timedelta(seconds=1))
        assert not sent
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(dispatch_notifications, fixture.runtime.sessions, latest)
                for _ in range(2)
            ]
            for future in futures:
                future.result()
        assert len(sent) == len(set(sent)) == 2
        with fixture.runtime.sessions() as session:
            rows = list(session.scalars(select(TelegramNotificationRow)))
            assert all(row.status == "delivered" and row.attempts == 2 for row in rows)


def test_telegram_dispatch_database_failure_does_not_interrupt_monitoring(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tender_system.operator import telegram

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("notification storage unavailable")

    monkeypatch.setattr(telegram, "dispatch_notifications", fail)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        saved = campaign(client)
        key = source(client, fixture)
        collect(client, key)
        assert results(client, saved["id"])["total"] == 2
        collect(client, key)
        assert results(client, saved["id"])["total"] == 2


def test_live_telegram_persisted_matching_workflow(migrated_engine: Engine) -> None:
    """Explicit opt-in: sends one real message using only owned fixture tender data."""
    import os

    from tender_system.persistence.storage import TelegramNotificationRow

    if os.environ.get("QUARRY_LIVE_TELEGRAM") != "1":
        pytest.skip("Set QUARRY_LIVE_TELEGRAM=1 to authorize one real Telegram notification")
    if not os.environ.get("QUARRY_TELEGRAM_BOT_TOKEN") or not os.environ.get(
        "QUARRY_TELEGRAM_CHAT_ID"
    ):
        pytest.fail(
            "Live delivery requires QUARRY_TELEGRAM_BOT_TOKEN "
            "and authorized QUARRY_TELEGRAM_CHAT_ID"
        )
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        fixture.record_count = 1
        saved = campaign(
            client,
            name="Quarry live verification",
            notifications={"chat_id": os.environ["QUARRY_TELEGRAM_CHAT_ID"]},
        )
        key = source(client, fixture)
        collect(client, key)
        assert results(client, saved["id"])["total"] == 1
        with fixture.runtime.sessions() as session:
            row = session.scalar(select(TelegramNotificationRow))
            assert row is not None and row.status == "delivered", (
                "Telegram acceptance unverified; inspect outbox"
            )
            assert row.delivered_at is not None and row.attempts == 1
        collect(client, key)
        with fixture.runtime.sessions() as session:
            assert session.scalar(select(func.count()).select_from(TelegramNotificationRow)) == 1


@pytest.mark.parametrize("behavior", ["unsure", "invalid_response", "invalid_evidence", "failure"])
def test_telegram_never_notifies_nonrelevant_or_failed_analysis(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, behavior: str
) -> None:
    import httpx

    from tender_system.persistence.storage import TelegramNotificationRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
    sent = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: sent.append(True))
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        saved = campaign(
            client,
            automatic_mode="semantic",
            automatic_ai_authorized=True,
            notifications={"chat_id": "123"},
        )
        fixture.model_behavior = behavior
        fixture.fail_model = behavior == "failure"
        collect(client, source(client, fixture))
        assert len(fixture.calls) == 2
        assert not sent
        assert results(client, saved["id"])["total"] == 0
        assert client.get("/api/v1/web/tenders").json()["total"] == 2
        with fixture.runtime.sessions() as session:
            assert session.scalar(select(func.count()).select_from(TelegramNotificationRow)) == 0


def test_telegram_refreshes_unsent_results_but_never_repeats_delivered_version(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from tender_system.operator.telegram import dispatch_notifications
    from tender_system.persistence.storage import TelegramNotificationRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "")
    sent = []

    def send(*args: object, **kwargs: Any) -> httpx.Response:
        sent.append(kwargs["json"])
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", send)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        saved = campaign(client, name="Old", notifications={"chat_id": "123"})
        key = source(client, fixture)
        collect(client, key)
        for name in ("Current", "After delivery"):
            reply = client.post(
                f"/api/v1/web/campaigns/{saved['id']}/criteria",
                json={
                    "criteria": {**saved["criteria"], "name": name},
                    "expected_version_id": saved["current_criteria_version_id"],
                },
            )
            assert reply.status_code == 200
            saved = reply.json()
            collect(client, key)
            monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
            dispatch_notifications(fixture.runtime.sessions, fixture.runtime.clock.now())
            assert len(sent) == 2
            assert all("Campaign: Current" in message["text"] for message in sent)
        with fixture.runtime.sessions() as session:
            rows = list(session.scalars(select(TelegramNotificationRow)))
            assert len(rows) == 2 and all(row.status == "delivered" for row in rows)


@pytest.mark.parametrize("reply", ["invalid_json", "non_object", "server_error"])
def test_telegram_unknown_responses_preserve_matches_without_resending(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    from datetime import timedelta

    import httpx

    from tender_system.operator.telegram import dispatch_notifications
    from tender_system.persistence.storage import TelegramNotificationRow

    monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
    sent = []

    def send(*args: object, **kwargs: Any) -> httpx.Response:
        sent.append(kwargs["json"]["text"])
        if reply == "invalid_json":
            return httpx.Response(200, text="invalid test-token")
        if reply == "non_object":
            return httpx.Response(200, json=["test-token"])
        return httpx.Response(503, json={"description": "test-token"})

    monkeypatch.setattr(httpx, "post", send)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        saved = campaign(client, notifications={"chat_id": "123"})
        key = source(client, fixture)
        collect(client, key)
        assert len(sent) == 2
        assert results(client, saved["id"])["total"] == 2
        collect(client, key)
        dispatch_notifications(
            fixture.runtime.sessions, fixture.runtime.clock.now() + timedelta(days=1)
        )
        assert len(sent) == 2
        with fixture.runtime.sessions() as session:
            rows = list(session.scalars(select(TelegramNotificationRow)))
            assert all(row.status == "uncertain" and row.attempts == 1 for row in rows)
            assert all("test-token" not in (row.last_error or "") for row in rows)


@pytest.mark.parametrize(
    "failure",
    [
        "rate_limited",
        "rate_limited_exhausted",
        "rate_limited_long",
        "rate_limited_usage",
        "timeout",
        "authentication_error",
        "request_too_large",
    ],
)
def test_bounded_retry_only_for_explicit_unbilled_rate_limit(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr("tender_system.operator.matching_budget.sleep", lambda _: None)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client, automatic_mode="semantic", automatic_ai_authorized=True)
        key = source(client, fixture)
        delegate = fixture.runtime.models(fixture.runtime.config.for_campaign("test").model)
        attempts = 0

        def generate(request: ModelRequest) -> ModelResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1 or failure != "rate_limited":
                raise ModelFailure(
                    "rate_limited" if failure.startswith("rate_limited") else failure,
                    http_status=429
                    if failure.startswith("rate_limited")
                    else {
                        "timeout": None,
                        "authentication_error": 401,
                        "request_too_large": 413,
                    }[failure],
                    retry_after_seconds=3600 if failure == "rate_limited_long" else None,
                    metadata=ModelResponse(
                        output={}, model_version="fixture", input_tokens=10, output_tokens=0
                    )
                    if failure == "rate_limited_usage"
                    else None,
                )
            return delegate.generate(request)

        fixture.runtime.models = lambda _: FunctionModelClient(generate)
        collect(client, key)
        operation = automatic(fixture)[0]
        fixture.runtime.tick(operation.id)
        with fixture.runtime.sessions() as session:
            rows = SqlMatchJobRepository(session).results()
            spend = list(session.scalars(select(MatchingSpendRow)))
        assert len(rows) == len(spend) == 2
        if failure == "rate_limited":
            assert attempts == 3  # first job rejected once, both then succeed
            assert rows[0].outcome.model_calls[0].attempts == 2
            assert not any(call.error_code for row in rows for call in row.outcome.model_calls)
        else:
            assert attempts == (6 if failure == "rate_limited_exhausted" else 2)
            expected_code = "rate_limited" if failure.startswith("rate_limited") else failure
            assert all(row.outcome.result == "unsure" for row in rows)
            assert all(row.outcome.model_calls[0].error_code == expected_code for row in rows)
            assert all(
                row.outcome.model_calls[0].attempts
                == (3 if failure == "rate_limited_exhausted" else 1)
                for row in rows
            )


@pytest.mark.parametrize(
    "behavior,expected,errors",
    [
        ("invalid_evidence", "partial_failure", 2),
        ("unsure", "needs_review", 0),
    ],
)
def test_monitoring_reports_unresolved_and_invalid_evidence(
    migrated_engine: Engine, behavior: str, expected: str, errors: int
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client, automatic_mode="semantic", automatic_ai_authorized=True)
        fixture.model_behavior = behavior
        key = source(client, fixture)
        parent_id = collect(client, key)
        with fixture.runtime.sessions() as session:
            parent = session.get(OperationRow, UUID(parent_id))
            assert parent is not None
            monitoring = parent.result["monitoring"]
        assert isinstance(monitoring, dict)
        assert monitoring["status"] == expected
        assert monitoring["unresolved_evaluations"] == 2
        assert monitoring["relevant_tenders"] == 0
        assert monitoring["errors"] == errors
        assert monitoring["completed_at"] is not None


def test_budget_denial_after_lease_loss_preserves_jobs_and_nonnegative_totals(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        wanted = campaign(client, automatic_mode="semantic", automatic_ai_authorized=True)
        key = source(client, fixture)

        def lose_lease(self: BudgetedClient, request: ModelRequest) -> ModelResponse:
            with self.sessions.begin() as session:
                row = session.get(MatchJobRow, self.job_id)
                assert row is not None
                row.lease_expires_at = fixture.runtime.clock.now()
            self.stopped = "Authorization changed while evaluating"
            raise ModelFailure("matching_budget_stopped")

        with monkeypatch.context() as patch:
            patch.setattr(BudgetedClient, "generate", lose_lease)
            collect(client, key)
        operation = automatic(fixture)[0]
        assert operation.status == "failed"
        assert operation.result["completed"] == 0
        assert operation.result["lost_leases"] == 2
        assert operation.result["pending"] == 2
        assert not fixture.calls
        with fixture.runtime.sessions() as session:
            assert not SqlMatchJobRepository(session).results()
            assert all(row.attempts == 1 for row in session.scalars(select(MatchJobRow)))
        # Explicit new work remains recoverable; expired single-attempt jobs do not
        # silently repeat potentially paid work after losing their completion lease.
        tenders = client.get("/api/v1/web/tenders").json()["items"]
        preview = client.post(
            "/api/v1/web/matching/preview",
            json={
                "campaign_id": wanted["id"],
                "tender_ids": [tender["id"] for tender in tenders],
                "mode": "semantic",
                "force_new": True,
            },
        ).json()
        assert (
            client.post(f"/api/v1/web/matching/{preview['id']}/confirm", json={}).status_code == 202
        )
        assert len(fixture.calls) == 2
        assert results(client, wanted["id"])["total"] == 2


def test_budget_blocked_monitoring_is_not_permanently_running(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client, automatic_mode="semantic", automatic_ai_authorized=True, ai_budget_usd=0)
        key = source(client, fixture)
        parent_id = collect(client, key)
        with fixture.runtime.sessions() as session:
            parent = session.get(OperationRow, UUID(parent_id))
            assert parent is not None
            monitoring = parent.result["monitoring"]
        assert isinstance(monitoring, dict)
        assert monitoring["status"] == "blocked"
        assert monitoring["pending_evaluations"] == 2
        assert not fixture.calls


def test_automatic_job_limit_continues_after_restart_without_duplicate_jobs(
    migrated_engine: Engine,
) -> None:
    from tender_system.operator.operator_runtime import OperatorRuntime

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        for _ in range(51):
            campaign(client, notifications={"enabled": False})
        key = source(client, fixture)
        collect(client, key)
        operation = automatic(fixture)[0]
        assert operation.status == "queued"
        assert operation.result["pending"] == 2
        assert operation.error_code is None
        with fixture.runtime.sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(MatchJobRow)
                    .where(MatchJobRow.status == "done")
                )
                == 100
            )
        # New runtime has no in-memory continuation state. Concurrent polling must
        # preserve operation ownership and canonical campaign/tender evaluations.
        restarted = OperatorRuntime(
            fixture.runtime.sessions, fixture.runtime.config, evidence=fixture.runtime.evidence
        )
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                ticks = tuple(pool.map(lambda _: restarted.tick(), range(2)))
            assert any(ticks)
            with fixture.runtime.sessions() as session:
                stored = session.get(OperationRow, operation.id)
                assert stored and stored.status == "completed" and stored.result["pending"] == 0
                assert session.scalar(select(func.count()).select_from(MatchJobRow)) == 102
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(MatchJobRow)
                        .where(MatchJobRow.status == "done")
                    )
                    == 102
                )
            assert restarted.tick() is False
            assert not fixture.calls
        finally:
            restarted.close()


def test_locked_source_prefix_does_not_starve_other_sources(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text

    with web_fixture(migrated_engine) as fixture:
        now = fixture.runtime.clock.now()
        with fixture.runtime.sessions.begin() as session:
            for index in range(101):
                session.add(
                    OperationRow(
                        id=uuid4(),
                        kind="collection",
                        source_id="busy" if index < 100 else "healthy",
                        status="queued",
                        payload={},
                        result={},
                        created_at=now + timedelta(microseconds=index),
                    )
                )
        executed: list[str | None] = []

        def execute(
            key: UUID, kind: str, source_id: str | None, payload: object, config: object
        ) -> tuple[dict[str, Any], str]:
            executed.append(source_id)
            return {}, "completed"

        monkeypatch.setattr(fixture.runtime, "execute", execute)
        with fixture.runtime.sessions.begin() as ownership:
            ownership.execute(text("select pg_advisory_xact_lock(1364546124, hashtext('busy'))"))
            assert fixture.runtime.tick()
            assert executed == ["healthy"]
            assert not fixture.runtime.tick()
            with fixture.runtime.sessions() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(OperationRow)
                        .where(OperationRow.source_id == "busy", OperationRow.status == "queued")
                    )
                    == 100
                )
        assert fixture.runtime.tick()
        assert executed == ["healthy", "busy"]


@pytest.mark.parametrize("paid", [False, True])
def test_restart_recovers_automatic_jobs_without_repeating_reserved_requests(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, paid: bool
) -> None:
    from tender_system.persistence.matching_storage import TransactionalMatchingStore
    from tender_system.persistence.storage import MatchResultRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(
            client,
            automatic_mode="semantic" if paid else "rules",
            automatic_ai_authorized=paid,
            notifications={"enabled": False},
        )
        key = source(client, fixture)
        # Stop exactly at result commit: jobs retain their live leases, and paid
        # requests have durable reservations. Model/network fixtures remain real.
        with monkeypatch.context() as interrupted:
            interrupted.setattr(TransactionalMatchingStore, "complete", lambda *args: False)
            collect(client, key)
        operation = automatic(fixture)[0]
        calls = len(fixture.calls)
        with fixture.runtime.sessions.begin() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(MatchJobRow)
                    .where(MatchJobRow.status == "running")
                )
                == 2
            )
            row = session.get(OperationRow, operation.id)
            assert row is not None
            # A hard disconnect leaves the operation running without its owner lock.
            row.status, row.finished_at = "running", None
        assert fixture.runtime.tick()
        assert automatic(fixture)[0].status == "queued"
        assert len(fixture.calls) == calls
        assert not fixture.runtime.tick()  # Waiting leases do not occupy the polling window.
        with fixture.runtime.sessions() as session:
            expires = session.scalar(select(func.max(MatchJobRow.lease_expires_at)))
        assert expires is not None
        from tender_system.testing.fakes import FakeClock

        fixture.runtime.clock = FakeClock(expires + timedelta(seconds=1))
        assert fixture.runtime.tick()
        with fixture.runtime.sessions() as session:
            row = session.get(OperationRow, operation.id)
            assert row and row.status == ("failed" if paid else "completed")
            assert row.result["recovered_after_disconnect"] is True
            jobs = list(session.scalars(select(MatchJobRow)))
            assert all(job.status == ("failed" if paid else "done") for job in jobs)
            if paid:
                assert all(job.last_error_code == "uncertain_paid_request" for job in jobs)
            assert session.scalar(select(func.count()).select_from(MatchResultRow)) == (
                0 if paid else 2
            )
        assert len(fixture.calls) == calls
        assert not fixture.runtime.tick()


def test_automatic_continuations_preserve_per_run_spending_limit(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        for _ in range(51):
            campaign(
                client,
                automatic_mode="semantic",
                automatic_ai_authorized=True,
                notifications={"enabled": False},
            )
        settings = client.get("/api/v1/web/ai-budget").json()["settings"]
        reply = client.put("/api/v1/web/ai-budget", json={**settings, "max_requests_per_run": 100})
        assert reply.status_code == 200
        key = source(client, fixture)
        collect(client, key)
        operation = automatic(fixture)[0]
        assert operation.status == "queued"
        assert len(fixture.calls) == 100
        assert fixture.runtime.tick()
        operation = automatic(fixture)[0]
        assert operation.status == "failed"
        assert operation.result["pending"] == 2
        assert operation.error_code == "matching_budget_stopped"
        assert len(fixture.calls) == 100
        assert not fixture.runtime.tick()
        assert (
            client.post(f"/api/v1/web/operations/{operation.id}/resume", json={}).status_code == 202
        )
        assert len(fixture.calls) == 102
        assert automatic(fixture)[0].status == "completed"
