import json
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import Engine

from tender_system.collection import collection_cli
from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore
from tender_system.collection.health import ConservativeRunHealth
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.recipes import FileRecipeLoader, RecipeExecutor
from tender_system.collection.source_runs import RunReceipt, SourceRunService
from tender_system.core.configuration import SourceSettings, load_configuration
from tender_system.core.domain import (
    CollectionRun,
    ExecutionContext,
    SourceFailure,
    SourceRecord,
    SourceSnapshot,
)
from tender_system.core.ports import HttpRequest, HttpResponse
from tender_system.persistence.storage import (
    SqlSourceRepository,
    SqlTenderRepository,
    TransactionalRunStore,
    session_factory,
)
from tender_system.testing.fakes import FakeClock

ROOT = Path(__file__).resolve().parents[2]
DEFINITION = ROOT / "sources" / "legacy" / "uzex"
FIXTURES = ROOT / "tests" / "fixtures" / "uzex"
SOURCE = SourceRecord.model_validate_json((DEFINITION / "source.json").read_text())
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


class UzexFixture:
    """Observed anonymous protocol; every URL must match a known read-only endpoint."""

    def __init__(self, variant: str = "healthy") -> None:
        self.rows = TypeAdapter(list[dict[str, JsonValue]]).validate_json(
            (FIXTURES / "listing.json").read_text()
        )
        self.details = TypeAdapter(dict[str, dict[str, JsonValue]]).validate_json(
            (FIXTURES / "details.json").read_text()
        )
        self.variant = variant
        self.requests: list[HttpRequest] = []
        self.first_requests = 0

    def request(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if request.url == "https://apietender.uzex.uz/api/common/TradeList":
            assert request.method == "POST" and request.body is not None
            body = json.loads(request.body)
            assert body["TypeId"] == 2 and body["System_Id"] == 0
            start, end = body["From"], body["To"]
            assert type(start) is int and type(end) is int
            if start == 1:
                self.first_requests += 1
            if self.variant == "late_failure" and start == 21:
                return HttpResponse(request.url, 503, b"temporarily unavailable")
            if self.variant == "session_expired":
                return HttpResponse(request.url, 401, b"not authorized")
            if self.variant == "parser_break":
                return HttpResponse(request.url, 200, b"<html>maintenance</html>")
            rows = deepcopy(self.rows[start - 1 : end])
            if self.variant == "partial" and start == 21:
                rows = []
            if self.variant == "ordinal" and start == 21 and rows:
                rows[0]["rn"] = 22
            if self.variant == "total_drift" and start == 21 and rows:
                rows[0]["total_count"] = 22
            if self.variant == "anchor_drift" and self.first_requests > 1:
                rows[0]["name"] = "Changed mid-sweep"
            payload: JsonValue = TypeAdapter(JsonValue).validate_python(rows)
        else:
            prefix = "https://apietender.uzex.uz/api/common/GetTrade/"
            assert (
                request.method == "GET"
                and request.url.startswith(prefix)
                and request.url.endswith("/0")
            )
            identifier = request.url.removeprefix(prefix).removesuffix("/0")
            payload = deepcopy(self.details[identifier])
            if self.variant == "wrong_value":
                payload["start_cost"] = 1
            if self.variant == "wrong_id":
                payload["id"] = 42
            if self.variant == "unknown_status":
                payload["status_id"] = 999
        return HttpResponse(
            request.url, 200, json.dumps(payload).encode(), {"content-type": "application/json"}
        )


def policy() -> SourceSettings:
    return load_configuration(DEFINITION / "config.toml").for_source(SOURCE.id)


def test_observed_protocol_recipe_paginates_and_normalizes_without_custom_code() -> None:
    http = UzexFixture()
    evidence = MemoryEvidenceStore()
    recipe = FileRecipeLoader().load(str(DEFINITION / "recipe.json"))
    clock = FakeClock(NOW)
    result = RecipeExecutor(http, evidence, policy(), clock=clock, sleep=lambda _: None).execute(
        recipe, ExecutionContext(run_id=uuid4())
    )
    assert isinstance(result, CollectionRun)
    assert len(result.raw_observations) == 21 and result.expected_count == 21
    assert len(result.diagnostics.pages) == len(set(result.diagnostics.pages)) == 3
    normalizer = ConfigurableTenderNormalizer()
    normalized = [normalizer.normalize(raw, SOURCE, policy()) for raw in result.raw_observations]
    assert all(n.candidate is not None and not n.issues for n in normalized)
    candidates = tuple(n.candidate for n in normalized if n.candidate is not None)
    first = candidates[0]
    assert first.external_tender_id == "513636" and first.buyer == http.rows[0]["seller_name"]
    assert first.source_url == "https://etender.uzex.uz/lot/513636"
    assert first.deadline == datetime(2026, 10, 5, 16, 46, 35, tzinfo=UTC)
    assert first.published_at is None
    assert first.description is not None and "Texnik topshiriq" in first.description
    assert {c.currency for c in candidates} == {"UZS", "USD"}
    assert first.raw_data["listing"] == http.rows[0]
    run = result.model_copy(update={"candidates": candidates})
    health = ConservativeRunHealth().evaluate(run, policy(), SourceSnapshot(), (), now=NOW)
    assert health.publication_allowed and health.overall_status == "degraded"
    assert {r.code for r in health.reasons if r.severity == "warning"} == {
        "pagination_consistency_unproven"
    }
    assert health.completeness == "complete" and not health.absence_evidence_allowed
    assert evidence.get(result.evidence_reference)["observations"]


@pytest.mark.parametrize(
    "variant",
    [
        "late_failure",
        "session_expired",
        "parser_break",
        "ordinal",
        "total_drift",
        "anchor_drift",
        "wrong_value",
        "wrong_id",
        "unknown_status",
    ],
)
def test_source_protocol_breaks_produce_failure_evidence(variant: str) -> None:
    evidence = MemoryEvidenceStore()
    result = RecipeExecutor(
        UzexFixture(variant), evidence, policy(), clock=FakeClock(NOW), sleep=lambda _: None
    ).execute(
        FileRecipeLoader().load(str(DEFINITION / "recipe.json")), ExecutionContext(run_id=uuid4())
    )
    assert isinstance(result, SourceFailure)
    assert evidence.get(result.failed_evidence_reference)["failed_step"]


class Collector:
    def __init__(self, engine: Engine) -> None:
        self.sessions = session_factory(engine)
        self.clock = FakeClock(NOW)
        self.store = TransactionalRunStore(self.sessions, clock=self.clock)
        self.evidence = MemoryEvidenceStore()
        with self.sessions.begin() as session:
            SqlSourceRepository(session).add(SOURCE)

    def collect(self, http: UzexFixture, run_id: UUID | None = None) -> RunReceipt:
        self.clock.instant += timedelta(minutes=1)
        settings = policy()
        service = SourceRunService(
            RecipeExecutor(http, self.evidence, settings, clock=self.clock, sleep=lambda _: None),
            ConfigurableTenderNormalizer(),
            self.store,
            clock=self.clock,
        )
        return service.run(
            run_id or uuid4(),
            FileRecipeLoader().load(str(DEFINITION / "recipe.json")),
            SOURCE,
            settings,
        )

    def state(self) -> tuple[tuple[str, int, int, str | None], ...]:
        with self.sessions() as session:
            repository = SqlTenderRepository(session)
            return tuple(
                (
                    t.external_tender_id or "",
                    len(repository.history(t.internal_id)),
                    t.presence.missing_successful_runs,
                    t.title,
                )
                for t in repository.for_source(SOURCE.id)
            )


@pytest.mark.database
def test_uzex_repetition_replay_changes_and_unseen_records(migrated_engine: Engine) -> None:
    collector = Collector(migrated_engine)
    run_id = uuid4()
    first = collector.collect(UzexFixture(), run_id)
    assert first.status == "accepted"
    before = collector.state()
    replay_http = UzexFixture()
    assert collector.collect(replay_http, run_id) == first and not replay_http.requests
    assert collector.collect(UzexFixture()).status == "accepted" and collector.state() == before
    changed = UzexFixture()
    changed.rows[0]["name"] = "One changed title"
    receipt = collector.collect(changed)
    assert receipt.status == "accepted" and len(receipt.rematch_tender_ids) == 1
    assert sum(row[1] for row in collector.state()) == 22
    absent = UzexFixture()
    absent.rows = absent.rows[:20]
    for row in absent.rows:
        row["total_count"] = 20
    receipt = collector.collect(absent)
    assert receipt.status == "accepted" and receipt.missing_incremented == 0
    assert all(row[2] == 0 for row in collector.state())
    snapshot = collector.store.snapshot(SOURCE.id)
    assert snapshot.last_incremental_at is not None
    assert snapshot.last_full_verified_at is None and snapshot.accepted_full_runs == 0


@pytest.mark.database
def test_new_lot_does_not_change_identity_or_history_of_shifted_existing_rows(
    migrated_engine: Engine,
) -> None:
    collector = Collector(migrated_engine)
    assert collector.collect(UzexFixture()).status == "accepted"
    expanded = UzexFixture()
    new_row = deepcopy(expanded.rows[0])
    old_id = str(new_row["id"])
    new_row.update(id=999999, display_no="FIXTURE-NEW-999999", name="New fixture tender")
    new_detail = deepcopy(expanded.details[old_id])
    new_detail.update(id=999999, display_no="FIXTURE-NEW-999999")
    expanded.details["999999"] = new_detail
    expanded.rows.insert(0, new_row)
    for number, row in enumerate(expanded.rows, 1):
        row.update(rn=number, total_count=22)
    receipt = collector.collect(expanded)
    assert receipt.status == "accepted" and len(receipt.rematch_tender_ids) == 1
    assert len(collector.state()) == 22 and all(row[1] == 1 for row in collector.state())
    assert receipt.missing_incremented == 0


def test_configuration_does_not_claim_full_inventory_or_unproven_cursor() -> None:
    settings = policy()
    recipe = FileRecipeLoader().load(str(DEFINITION / "recipe.json"))
    assert not settings.calibration.full_inventory_supported
    assert settings.calibration.expected_mode == recipe.coverage == "incremental"
    assert settings.calibration.empty_policy == "block" and not settings.health.allow_empty
    assert recipe.checkpoint is None and recipe.data_as_of is None


@pytest.mark.database
@pytest.mark.parametrize(
    "variant",
    [
        "partial",
        "empty",
        "malformed_date",
        "duplicate",
        "mass_corruption",
        "late_failure",
        "ordinal",
        "anchor_drift",
    ],
)
def test_bad_source_runs_never_mutate_accepted_state(migrated_engine: Engine, variant: str) -> None:
    collector = Collector(migrated_engine)
    collector.collect(UzexFixture())
    before, snapshot = collector.state(), collector.store.snapshot(SOURCE.id)
    broken = UzexFixture(variant)
    if variant == "empty":
        broken.rows = []
    elif variant == "duplicate":
        broken.rows[-1] = {**broken.rows[0], "rn": 21}
    elif variant == "malformed_date":
        broken.rows[0]["end_date"] = "31/99/2026"
        broken.details[str(broken.rows[0]["id"])]["end_date"] = "31/99/2026"
    elif variant == "mass_corruption":
        for row in broken.rows:
            row["name"] = "Plausible but wrong title"
    receipt = collector.collect(broken)
    assert receipt.status in {"failed", "rejected"}
    assert receipt.health is not None and not receipt.health.absence_evidence_allowed
    assert receipt.missing_incremented == 0
    assert collector.state() == before and collector.store.snapshot(SOURCE.id) == snapshot


@pytest.mark.database
@pytest.mark.parametrize("variant", ["healthy", "partial"])
def test_explicit_live_command_can_be_run_against_offline_transport(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    variant: str,
) -> None:
    fixture = UzexFixture(variant)

    def handler(request: httpx.Request) -> httpx.Response:
        response = fixture.request(
            HttpRequest(
                str(request.url), request.method, dict(request.headers), request.content or None
            )
        )
        return httpx.Response(response.status_code, content=response.body, headers=response.headers)

    http = HttpxAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(collection_cli, "HttpxAdapter", lambda: http)
    monkeypatch.setenv("DATABASE_URL", migrated_engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect",
            "--source",
            str(DEFINITION / "source.json"),
            "--recipe",
            str(DEFINITION / "recipe.json"),
            "--config",
            str(DEFINITION / "config.toml"),
            "--evidence",
            str(tmp_path),
            "--repeat",
            "2",
        ],
    )
    if variant == "healthy":
        collection_cli.main()
        reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert len(reports) == 2 and all(r["exact_replay_verified"] for r in reports)
        assert [r["stored_tenders"] for r in reports] == [21, 21]
        assert [r["stored_versions"] for r in reports] == [21, 21]
    else:
        with pytest.raises(SystemExit, match="1"):
            collection_cli.main()
        report = json.loads(capsys.readouterr().out)
        assert report["publication_result"] == "rejected"
        assert report["stored_tenders"] == report["stored_versions"] == 0


@pytest.mark.parametrize("repeat", ["0", "4"])
def test_live_command_refuses_unbounded_repetition(
    monkeypatch: pytest.MonkeyPatch, repeat: str
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect",
            "--source",
            "unused",
            "--recipe",
            "unused",
            "--config",
            "unused",
            "--evidence",
            "unused",
            "--repeat",
            repeat,
        ],
    )
    with pytest.raises(SystemExit, match="2"):
        collection_cli.main()
