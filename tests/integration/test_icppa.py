import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import Engine

from tender_system.collection.adapters import MemoryEvidenceStore
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
DEFINITION = ROOT / "sources" / "legacy" / "icppa"
FIXTURES = ROOT / "tests" / "fixtures" / "icppa"
SOURCE = SourceRecord.model_validate_json((DEFINITION / "source.json").read_text())
NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


def policy() -> SourceSettings:
    return load_configuration(DEFINITION / "config.toml").for_source(SOURCE.id)


class IcppaFixture:
    """Anonymous observed API. Unexpected requests fail; no network fallback."""

    def __init__(self, variant: str = "healthy") -> None:
        self.rows = TypeAdapter(list[dict[str, JsonValue]]).validate_json(
            (FIXTURES / "listing.json").read_text()
        )
        self.details = TypeAdapter(dict[str, dict[str, JsonValue]]).validate_json(
            (FIXTURES / "details.json").read_text()
        )
        self.operators = TypeAdapter(dict[str, dict[str, JsonValue]]).validate_json(
            (FIXTURES / "operators.json").read_text()
        )
        self.title_rows: dict[str, list[dict[str, JsonValue]]] = {}
        self.page = TypeAdapter(dict[str, JsonValue]).validate_json(
            (FIXTURES / "page.json").read_text()
        )
        self.variant = variant
        self.requests: list[HttpRequest] = []
        self.first_requests = 0

    def request(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        parts = urlsplit(request.url)
        assert parts.scheme == "https"
        if parts.netloc == "apietender.uzex.uz":
            if parts.path == "/api/common/TradeList":
                assert request.method == "POST" and request.body is not None
                body = json.loads(request.body)
                assert {k: v for k, v in body.items() if k != "Keyword"} == {
                    "From": 1,
                    "To": 10,
                    "TypeId": 2,
                    "System_Id": 0,
                }
                return HttpResponse(
                    request.url, 200, json.dumps(self.title_rows.get(body["Keyword"], [])).encode()
                )
            assert request.method == "GET" and parts.path.startswith("/api/common/GetTrade/")
            payload = deepcopy(self.operators[parts.path.split("/")[-2]])
            if self.variant == "operator_unavailable":
                return HttpResponse(request.url, 503, b"unavailable")
            if self.variant == "operator_wrong_number":
                payload["display_no"] = "26111006000000"
            if self.variant == "operator_wrong_buyer":
                payload["customer_tin"] = "999999999"
            if self.variant == "operator_wrong_value":
                payload["start_cost"] = 1
            if self.variant == "operator_wrong_id":
                payload["id"] = 42
            if self.variant == "operator_wrong_type":
                payload["type_name"] = "Selection"
            if self.variant == "operator_bad_products":
                payload["budget_products"] = "not JSON"
        elif parts.path == "/api/v1/lot/searchlots":
            assert parts.netloc == "xarid.icppa.uz"
            assert request.method == "POST" and request.body is not None
            assert json.loads(request.body) == {
                "purchaseType": "TENDER",
                "lotStatus": "IN_PROCESS",
                "platformId": "1",
                "organizationType": "BUDGET",
                "hasContract": True,
            }
            query = parse_qs(parts.query)
            assert query["size"] == ["10"] and query["sort"] == ["lotId,desc"]
            page = int(query["page"][0])
            if page == 0:
                self.first_requests += 1
            if self.variant == "session_expired":
                return HttpResponse(request.url, 401, b"Unauthorized")
            if self.variant == "maintenance":
                return HttpResponse(request.url, 200, b"<html>maintenance</html>")
            if self.variant == "late_failure" and page == 1:
                return HttpResponse(request.url, 503, b"unavailable")
            rows = deepcopy(self.rows[page * 10 : (page + 1) * 10])
            if self.variant == "anchor_drift" and self.first_requests > 2:
                rows[0]["enktNames"] = "Changed while paging"
            total = len(self.rows)
            payload = deepcopy(self.page)
            payload.update(
                content=TypeAdapter(JsonValue).validate_python(rows),
                number=page,
                size=10,
                numberOfElements=len(rows),
                totalElements=total,
                totalPages=max(1, (total + 9) // 10),
                first=page == 0,
                last=(page + 1) * 10 >= total,
                empty=not rows,
            )
            payload["pageable"] = {
                "pageNumber": page,
                "pageSize": 10,
                "offset": page * 10,
                "sort": payload["sort"],
                "paged": True,
                "unpaged": False,
            }
            if self.variant == "wrong_page":
                payload["number"] = page + 1
            if self.variant == "wrong_size":
                payload["size"] = 20
            if self.variant == "ignored_sort":
                payload["sort"] = {"sorted": False, "unsorted": True, "empty": True}
            if self.variant == "total_drift" and page == 1:
                payload["totalElements"] = total + 1
            if self.variant == "premature_last" and page == 0:
                payload["last"] = True
            if self.variant == "partial" and page == 1:
                payload.update(content=[], numberOfElements=0)
            if self.variant == "wrong_length":
                payload["numberOfElements"] = 99
        else:
            assert parts.netloc == "xarid.icppa.uz"
            assert request.method == "GET" and parts.path.startswith("/api/v1/lot/public/")
            payload = deepcopy(self.details[parts.path.rsplit("/", 1)[1]])
            if self.variant == "wrong_id":
                payload["lotId"] = 42
            if self.variant == "wrong_value":
                payload["startAmount"] = 1
            if self.variant == "unknown_status":
                payload["lotStatus"] = "UNPROVEN"
            if self.variant == "wrong_buyer_identity":
                assert isinstance(payload["orgInfo"], dict)
                payload["orgInfo"]["inn"] = "999999999"
        return HttpResponse(
            request.url, 200, json.dumps(payload).encode(), {"content-type": "application/json"}
        )


def execute(http: IcppaFixture, evidence: MemoryEvidenceStore) -> CollectionRun | SourceFailure:
    return RecipeExecutor(
        http, evidence, policy(), clock=FakeClock(NOW), sleep=lambda _: None
    ).execute(
        FileRecipeLoader().load(str(DEFINITION / "recipe.json")), ExecutionContext(run_id=uuid4())
    )


def test_observed_central_protocol_reuses_engine_and_maps_only_proven_fields() -> None:
    http = IcppaFixture()
    evidence = MemoryEvidenceStore()
    result = execute(http, evidence)
    assert isinstance(result, CollectionRun)
    assert len(result.raw_observations) == result.expected_count == 11
    assert len(result.diagnostics.pages) == 2
    normalized = [
        ConfigurableTenderNormalizer().normalize(raw, SOURCE, policy())
        for raw in result.raw_observations
    ]
    candidates = tuple(n.candidate for n in normalized if n.candidate is not None)
    assert len(candidates) == 11 and all(not n.issues for n in normalized)
    sample = next(c for c in candidates if c.external_tender_id == "26111006517270")
    assert sample.title == "Kasalxona uchun jihozlar"
    assert sample.value == Decimal(
        "76296000000.00"
    )  # independently checked against rendered detail
    assert sample.currency == "UZS"
    assert sample.deadline is not None and sample.published_at is None
    assert sample.description and "Product_Name" not in sample.description
    assert sample.source_url == "https://xarid.icppa.uz/lot-info/26111006517270"
    closed = next(c for c in candidates if c.external_tender_id == "26111006507659")
    listing = closed.raw_data["listing"]
    assert isinstance(listing, dict) and listing["lotStatus"] == "IN_PROCESS"
    assert closed.status == "closed"
    health = ConservativeRunHealth().evaluate(
        result.model_copy(update={"candidates": candidates}),
        policy(),
        SourceSnapshot(),
        (),
        now=NOW,
    )
    assert health.publication_allowed and health.completeness == "complete"
    assert not health.absence_evidence_allowed
    assert evidence.get(result.evidence_reference)["observations"]
    assert len(http.requests) == 26  # pages, central/operator details, two witnesses


@pytest.mark.parametrize(
    "variant",
    [
        "session_expired",
        "maintenance",
        "late_failure",
        "wrong_page",
        "wrong_size",
        "ignored_sort",
        "total_drift",
        "premature_last",
        "partial",
        "wrong_length",
        "anchor_drift",
        "wrong_id",
        "wrong_value",
        "unknown_status",
        "wrong_buyer_identity",
        "operator_unavailable",
        "operator_wrong_number",
        "operator_wrong_buyer",
        "operator_wrong_value",
        "operator_wrong_id",
        "operator_wrong_type",
        "operator_bad_products",
    ],
)
def test_protocol_failures_retain_evidence(variant: str) -> None:
    evidence = MemoryEvidenceStore()
    result = execute(IcppaFixture(variant), evidence)
    assert isinstance(result, SourceFailure)
    assert result.failed_step and evidence.get(result.failed_evidence_reference)["events"]


class Collector:
    def __init__(self, engine: Engine) -> None:
        self.sessions = session_factory(engine)
        self.clock = FakeClock(NOW)
        self.evidence = MemoryEvidenceStore()
        self.store = TransactionalRunStore(self.sessions, clock=self.clock)
        with self.sessions.begin() as session:
            SqlSourceRepository(session).add(SOURCE)

    def collect(self, http: IcppaFixture, run_id: UUID | None = None) -> RunReceipt:
        self.clock.instant += timedelta(minutes=1)
        service = SourceRunService(
            RecipeExecutor(http, self.evidence, policy(), clock=self.clock, sleep=lambda _: None),
            ConfigurableTenderNormalizer(),
            self.store,
            clock=self.clock,
        )
        return service.run(
            run_id or uuid4(),
            FileRecipeLoader().load(str(DEFINITION / "recipe.json")),
            SOURCE,
            policy(),
        )

    def state(self) -> tuple[tuple[str, int, int], ...]:
        with self.sessions() as session:
            repository = SqlTenderRepository(session)
            return tuple(
                (
                    t.external_tender_id or "",
                    len(repository.history(t.internal_id)),
                    t.presence.missing_successful_runs,
                )
                for t in repository.for_source(SOURCE.id)
            )


@pytest.mark.database
def test_replay_volatile_evidence_meaningful_changes_and_absence(migrated_engine: Engine) -> None:
    collector = Collector(migrated_engine)
    run_id = uuid4()
    first = collector.collect(IcppaFixture(), run_id)
    assert first.status == "accepted"
    before = collector.state()
    replay = IcppaFixture()
    assert collector.collect(replay, run_id) == first and not replay.requests
    volatile = IcppaFixture()
    identifier = str(volatile.rows[0]["lotId"])
    volatile.details[identifier].update(
        paidAmount=123, savedPercent=42, analysisInfo={"isBlacklist": True}
    )
    volatile.operators[identifier[-6:]]["rest_time"] = "Changing countdown"
    receipt = collector.collect(volatile)
    assert receipt.status == "accepted" and not receipt.rematch_tender_ids
    assert collector.state() == before
    with collector.sessions() as session:
        tender = next(
            t
            for t in SqlTenderRepository(session).for_source(SOURCE.id)
            if t.external_tender_id == identifier
        )
        detail = tender.raw_data["detail"]
        assert isinstance(detail, dict) and detail["paidAmount"] == 123
    changed = IcppaFixture()
    changed.rows[0]["enktNames"] = "Changed product summary"
    receipt = collector.collect(changed)
    assert receipt.status == "accepted" and len(receipt.rematch_tender_ids) == 1
    assert sum(row[1] for row in collector.state()) == 12
    changed.rows.pop()
    assert collector.collect(changed).missing_incremented == 0
    assert all(row[2] == 0 for row in collector.state())


@pytest.mark.database
@pytest.mark.parametrize(
    "variant",
    [
        "duplicate",
        "missing_content",
        "empty",
        "late_failure",
        "partial",
        "ignored_sort",
        "operator_wrong_number",
        "operator_unavailable",
    ],
)
def test_bad_runs_cannot_replace_persisted_state(migrated_engine: Engine, variant: str) -> None:
    collector = Collector(migrated_engine)
    assert collector.collect(IcppaFixture()).status == "accepted"
    before = collector.state()
    http = IcppaFixture(variant)
    if variant == "duplicate":
        http.rows[-1] = deepcopy(http.rows[0])
    elif variant == "missing_content":
        identifier = str(http.rows[-1]["lotId"])
        http.rows[-1]["enktNames"] = None
        http.operators[identifier[-6:]].update(
            budget_products="[]",
            description=None,
            addon_description=None,
            technical_description=None,
        )
    elif variant == "empty":
        http.rows.clear()
    receipt = collector.collect(http)
    assert receipt.status != "accepted" and receipt.missing_incremented == 0
    if variant in {"duplicate", "missing_content"}:
        assert receipt.health is not None
        code = "duplicate_identity" if variant == "duplicate" else "missing_required_content"
        assert code in {reason.code for reason in receipt.health.reasons}
    assert collector.state() == before
    assert collector.store.snapshot(SOURCE.id).accepted_full_runs == 0


def test_source_does_not_claim_publication_cursor_or_authoritative_absence() -> None:
    recipe = FileRecipeLoader().load(str(DEFINITION / "recipe.json"))
    settings = policy()
    assert recipe.coverage == settings.calibration.expected_mode == "incremental"
    assert recipe.checkpoint is None and recipe.data_as_of is None
    assert not settings.calibration.full_inventory_supported
    assert settings.calibration.empty_policy == "block" and not settings.health.allow_empty
    assert "title" not in settings.health.required_fields
    assert settings.health.required_any_of == (("title", "description"),)
    assert settings.field_change_policy["paidAmount"] == "IGNORE"
    assert settings.field_change_policy["analysisInfo"] == "VOLATILE"


@pytest.mark.database
def test_same_external_number_at_another_portal_keeps_separate_identity(
    migrated_engine: Engine,
) -> None:
    from test_uzex import Collector as UzexCollector
    from test_uzex import UzexFixture

    collector = Collector(migrated_engine)
    assert collector.collect(IcppaFixture()).status == "accepted"
    other = UzexCollector(migrated_engine)
    http = UzexFixture()
    old = str(http.rows[0]["id"])
    identifier = IcppaFixture().rows[0]["lotId"]
    http.rows[0]["id"] = identifier
    http.details[str(identifier)] = http.details.pop(old)
    http.details[str(identifier)]["id"] = identifier
    assert other.collect(http).status == "accepted"
    with collector.sessions() as session:
        repository = SqlTenderRepository(session)
        central = next(
            t for t in repository.for_source(SOURCE.id) if t.external_tender_id == str(identifier)
        )
        uzex = next(
            t
            for t in repository.for_source("uzex-etender")
            if t.external_tender_id == str(identifier)
        )
        assert central.internal_id != uzex.internal_id
        assert (
            len(repository.history(central.internal_id))
            == len(repository.history(uzex.internal_id))
            == 1
        )
