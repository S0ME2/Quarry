import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Never, cast
from uuid import uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import Engine

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.source_runs import SourceRunService
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    CollectionRun,
    FixResult,
    Reason,
    RecipeRevision,
    SampleVerification,
    SourceFailure,
    SourceRecipe,
    SourceRecord,
)
from tender_system.core.ports import (
    BrowserAdapter,
    HttpRequest,
    HttpResponse,
)
from tender_system.core.registry import Registry
from tender_system.persistence.recipe_storage import (
    SqlValidationEnvironment,
)
from tender_system.persistence.storage import (
    SqlSourceRepository,
    SqlTenderRepository,
    TransactionalRunStore,
    session_factory,
)
from tender_system.repair.repair_adapters import (
    RecipeRepairExecutors,
    StagedPlugin,
)
from tender_system.repair.repair_validation import IndependentRepairValidator, dry_run
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.fakes import (
    FakeBrowserAdapter,
    FakeClock,
    FakePage,
    FunctionHttpAdapter,
)

pytestmark = pytest.mark.database
NOW = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE = SourceRecord(id="s", name="Fixture")
RAW: list[dict[str, JsonValue]] = [
    {
        "external_tender_id": str(index),
        "title": f"Router {index}",
        "buyer": "Agency",
        "deadline": "2026-12-01",
        "source_url": f"https://fixture/detail/{index}",
    }
    for index in range(3)
]


def inline_recipe(
    rows: list[dict[str, JsonValue]],
    version: str = "2",
    coverage: str = "full",
    browser: bool = False,
) -> SourceRecipe:
    steps: list[dict[str, JsonValue]] = []
    if browser:
        steps.append(
            {
                "type": "browser_action",
                "parameters": {
                    "action": "navigate",
                    "value": "https://fixture",
                    "inventory_page": True,
                },
            }
        )
    steps.append(
        {
            "type": "transform",
            "parameters": {"name": "identity", "input": cast(list[JsonValue], rows), "emit": True},
        }
    )
    return SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": version,
            "coverage": coverage,
            "expected_count": len(rows),
            "completion_signal": {"kind": "total_reconciled", "value": len(rows)},
            "steps": steps,
        }
    )


class Samples:
    def __init__(self) -> None:
        self.override: tuple[Reason, ...] = ()

    def verify(
        self, run: CollectionRun, source: SourceRecord, settings: SourceSettings
    ) -> SampleVerification:
        expected = {row["external_tender_id"]: row for row in RAW}
        differences = list(self.override)
        for tender in run.candidates:
            if tender.external_tender_id in expected:
                for field in ("title", "buyer"):
                    if getattr(tender, field) != expected[tender.external_tender_id][field]:
                        differences.append(
                            Reason(
                                code="sample_mismatch",
                                detail="Independent source fixture disagrees",
                                field=field,
                            )
                        )
        return SampleVerification(checked=len(run.candidates), mismatches=tuple(differences))


class Fixture:
    def __init__(self, engine: Engine) -> None:
        self.sessions = session_factory(engine)
        self.clock = FakeClock(NOW)
        self.evidence = MemoryEvidenceStore()
        self.settings = fixture_source_settings(
            SourceSettings.model_validate(
                {
                    "repair": {"mode": "automatic"},
                    "fixer_model": {"provider": "normal"},
                    "stronger_fixer_model": {"provider": "strong"},
                    "health": {"minimum_retained_ratio": 0.5},
                }
            )
        )
        self.normalizer = ConfigurableTenderNormalizer()
        self.artifacts: Registry[StagedPlugin] = Registry()
        self.body = json.dumps({"items": RAW})
        self.browser_factory: Callable[[], BrowserAdapter] = lambda: FakeBrowserAdapter(
            {"https://fixture": FakePage(html="Inventory")}
        )
        self.executors = RecipeRepairExecutors(
            lambda: FunctionHttpAdapter(
                lambda request: HttpResponse(request.url, 200, self.body.encode())
            ),
            self.browser_factory,
            self.evidence,
            self.clock,
            self.artifacts,
        )
        self.environment = SqlValidationEnvironment(self.sessions, self.executors)
        self.samples = Samples()
        with self.sessions.begin() as session:
            SqlSourceRepository(session).add(SOURCE)
        self.old = inline_recipe(RAW, "1")
        receipt = SourceRunService(
            self.executors(RecipeRevision(recipe=self.old), self.settings),
            self.normalizer,
            TransactionalRunStore(self.sessions, clock=self.clock),
            self.clock,
        ).run(uuid4(), self.old, SOURCE, self.settings)
        assert receipt.status == "accepted"
        self.clock.instant += timedelta(seconds=1)
        failed_ref = self.evidence.put(uuid4(), {"observations": [], "error": "broken"})
        self.failure = SourceFailure(
            run_id=uuid4(),
            source_id="s",
            recipe=self.old,
            error_code="broken",
            error_detail="Source changed",
            failed_evidence_reference=failed_ref,
            last_good_evidence_reference=receipt.evidence_reference,
            started_at=self.clock.now(),
            finished_at=self.clock.now(),
        )

    def proposal(self, recipe: SourceRecipe | None = None) -> FixResult:
        return FixResult(
            status="fixed",
            updated_recipe=recipe or inline_recipe(RAW),
            explanation="Restore collection",
            confidence=1,
        )

    def current(self) -> tuple[int, int, tuple[int, ...]]:
        with self.sessions() as session:
            tenders = SqlTenderRepository(session).for_source("s")
            return (
                len(tenders),
                sum(len(SqlTenderRepository(session).history(t.internal_id)) for t in tenders),
                tuple(t.presence.missing_successful_runs for t in tenders),
            )


@pytest.fixture
def fixture(migrated_engine: Engine) -> Fixture:
    return Fixture(migrated_engine)


@pytest.mark.parametrize(
    "kind",
    ["collapse", "wrong_field", "wrong_id", "incomplete", "malformed", "empty", "wrong_source"],
)
def test_false_healing_rejected_and_never_published(fixture: Fixture, kind: str) -> None:
    rows = [dict(item) for item in RAW]
    recipe = inline_recipe(rows)
    if kind == "collapse":
        recipe = inline_recipe(rows[:1])
    elif kind == "empty":
        recipe = inline_recipe([])
    elif kind == "wrong_field":
        rows[0]["buyer"] = "Plausible but incorrect agency"
        recipe = inline_recipe(rows)
    elif kind == "wrong_id":
        rows[0]["external_tender_id"] = "invented"
        recipe = inline_recipe(rows)
    elif kind == "malformed":
        rows[0]["deadline"] = "99/99/2026"
        recipe = inline_recipe(rows)
    elif kind == "incomplete":
        recipe = recipe.model_copy(update={"completion_signal": None})
    elif kind == "wrong_source":
        recipe = recipe.model_copy(update={"source_id": "other"})
    before = fixture.current()
    if kind == "wrong_source":
        with pytest.raises(ValueError, match="changed run identity"):
            dry_run(
                fixture.environment,
                fixture.normalizer,
                RecipeRevision(recipe=recipe),
                fixture.failure,
                SOURCE,
                fixture.settings,
                uuid4(),
            )
        assert fixture.current() == before
        return
    report = IndependentRepairValidator(
        fixture.environment,
        fixture.normalizer,
        SOURCE,
        fixture.settings,
        fixture.samples,
        fixture.evidence,
        fixture.clock,
    ).validate(
        fixture.failure,
        fixture.proposal(recipe),
        dry_run(
            fixture.environment,
            fixture.normalizer,
            RecipeRevision(recipe=recipe),
            fixture.failure,
            SOURCE,
            fixture.settings,
            uuid4(),
        ),
    )
    assert not report.passed and fixture.current() == before


@pytest.mark.parametrize("change", ["real", "raw", "cosmetic"])
def test_repair_replay_compares_semantic_fields_not_volatile_evidence(
    fixture: Fixture, change: str
) -> None:
    calls = 0

    def responses(request: HttpRequest) -> HttpResponse:
        nonlocal calls
        calls += 1
        data = [dict(item) for item in RAW]
        if change == "raw":
            data[0]["time_left"] = 100 - calls
            data[0]["view_count"] = calls
        elif calls > 1:
            data[0]["title"] = "ROUTER 0" if change == "cosmetic" else "Changed during replay"
        return HttpResponse(request.url, 200, json.dumps({"items": data}).encode())

    fixture.environment.executors = RecipeRepairExecutors(
        lambda: FunctionHttpAdapter(responses),
        fixture.browser_factory,
        fixture.evidence,
        fixture.clock,
    )
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": "2",
            "expected_count": 3,
            "completion_signal": {"kind": "total_reconciled", "value": 3},
            "steps": [
                {
                    "type": "http_request",
                    "parameters": {"url": "https://fixture/api", "output": "response"},
                },
                {
                    "type": "extract",
                    "parameters": {
                        "format": "json",
                        "input": {"$ref": "response.body"},
                        "selector": "items",
                        "emit": True,
                    },
                },
            ],
        }
    )
    report = IndependentRepairValidator(
        fixture.environment,
        fixture.normalizer,
        SOURCE,
        fixture.settings,
        fixture.samples,
        fixture.evidence,
        fixture.clock,
    ).validate(
        fixture.failure,
        fixture.proposal(recipe),
        dry_run(
            fixture.environment,
            fixture.normalizer,
            RecipeRevision(recipe=recipe),
            fixture.failure,
            SOURCE,
            fixture.settings,
            uuid4(),
        ),
    )
    assert report.passed == (change != "real")
    assert fixture.current() == (3, 3, (0, 0, 0))


def test_recipe_catalog_uses_active_revision_and_replay_pins_recorded_version(
    fixture: Fixture,
) -> None:
    from tender_system.persistence.recipe_storage import SqlRecipeCatalog
    from tender_system.persistence.storage import ActiveRecipeRow, RecipeRevisionRow
    from tender_system.repair.repair_adapters import CatalogSourceRunner

    second, third = inline_recipe(RAW, "2"), inline_recipe(RAW, "3")
    with fixture.sessions.begin() as session:
        for recipe in (fixture.old, second, third):
            session.add(
                RecipeRevisionRow(
                    source_id="s",
                    version=recipe.version,
                    revision=RecipeRevision(recipe=recipe).model_dump(mode="json"),
                    created_at=fixture.clock.now(),
                )
            )
        session.flush()
        session.add(ActiveRecipeRow(source_id="s", version="2"))
    runner = CatalogSourceRunner(
        SqlRecipeCatalog(fixture.sessions),
        fixture.executors,
        fixture.normalizer,
        TransactionalRunStore(fixture.sessions, clock=fixture.clock),
        fixture.clock,
    )
    key = uuid4()
    receipt = runner.run(key, fixture.old, SOURCE, fixture.settings)
    assert receipt.status == "accepted" and receipt.recipe_version == "2"
    with fixture.sessions.begin() as session:
        active = session.get(ActiveRecipeRow, "s")
        assert active is not None
        active.version = "3"

    def forbidden(*args: object) -> Never:
        pytest.fail("recorded collection replay reached executor")

    runner.executors = forbidden
    assert runner.run(key, fixture.old, SOURCE, fixture.settings) == receipt
    before = fixture.current()
    rejected = SourceRunService(
        fixture.executors(RecipeRevision(recipe=fixture.old), fixture.settings),
        fixture.normalizer,
        TransactionalRunStore(fixture.sessions, clock=fixture.clock),
        fixture.clock,
    ).run(uuid4(), fixture.old, SOURCE, fixture.settings)
    assert rejected.status == "rejected" and "recipe_superseded" in rejected.reasons
    assert rejected.missing_incremented == 0 and fixture.current() == before
