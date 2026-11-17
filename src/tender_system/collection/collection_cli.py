"""Explicit collection command; deployment recipes remain outside the source engine."""

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from tender_system.collection.adapters import FileEvidenceStore, HttpxAdapter, MemorySessionStore
from tender_system.collection.browser import PlaywrightBrowserAdapter
from tender_system.collection.diagnostics import receipt_report
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.performance import PerformanceTrace
from tender_system.collection.recipes import FileRecipeLoader
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import load_configuration
from tender_system.core.domain import SourceRecord
from tender_system.core.ports import BrowserAdapter, HttpAdapter
from tender_system.core.registry import Registry
from tender_system.persistence.recipe_storage import SqlRecipeCatalog
from tender_system.persistence.storage import (
    SqlSourceRepository,
    SqlTenderRepository,
    TransactionalRunStore,
    build_engine,
    database_url_from_environment,
    session_factory,
)
from tender_system.repair.repair_adapters import CatalogSourceRunner, RecipeRepairExecutors


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicitly collect one configured source.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--recipe", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--performance-report",
        type=Path,
        help="Enable bounded performance tracing and write a machine-readable JSON report.",
    )
    args = parser.parse_args()
    if not 1 <= args.repeat <= 3:
        parser.error("repeat must be between 1 and 3")
    source = SourceRecord.model_validate_json(args.source.read_text())
    recipe = FileRecipeLoader().load(str(args.recipe))
    if recipe.source_id != source.id:
        parser.error("recipe and source IDs differ")
    configuration = load_configuration(args.config)
    settings = configuration.for_source(source.id)
    engine = build_engine(
        database_url_from_environment(configuration.global_settings.database_url_env)
    )
    http = HttpxAdapter()
    try:
        sessions = session_factory(engine)
        with sessions.begin() as session:
            sources = SqlSourceRepository(session)
            existing = sources.get(source.id)
            if existing is None:
                sources.add(source)
            elif existing != source:
                raise ValueError("source registration differs; refusing to change identity scope")
        store = TransactionalRunStore(sessions, configuration=configuration)
        http_adapters: Registry[HttpAdapter] = Registry()
        http_adapters.register("http", http)
        browsers: Registry[Callable[[], BrowserAdapter]] = Registry()

        def browser() -> BrowserAdapter:
            if not configuration.runtime.browser_enabled:
                raise ValueError("browser disabled by QUARRY_BROWSER_ENABLED")
            return PlaywrightBrowserAdapter(
                MemorySessionStore(), configuration.runtime.browser_timeout_seconds
            )

        browsers.register("playwright", browser)
        clock = SystemClock()
        service = CatalogSourceRunner(
            SqlRecipeCatalog(sessions),
            RecipeRepairExecutors(
                lambda: http_adapters.get(settings.acquisition_adapter),
                lambda: browsers.get(settings.browser_adapter)(),
                FileEvidenceStore(args.evidence or configuration.runtime.evidence_directory),
                clock,
            ),
            ConfigurableTenderNormalizer(),
            store,
            clock,
        )
        for _ in range(args.repeat):
            run_id = uuid4()
            trace = PerformanceTrace() if args.performance_report else None
            if trace is None:
                receipt = service.run(run_id, recipe, source, settings)
            else:
                with trace.activate():
                    receipt = service.run(run_id, recipe, source, settings)
            # An exact receipt replay must not execute or publish again.
            if service.run(run_id, recipe, source, settings) != receipt:
                raise RuntimeError("run replay changed its durable receipt")
            with sessions() as session:
                repository = SqlTenderRepository(session)
                tenders = repository.for_source(source.id)
                report = {
                    **receipt_report(receipt),
                    "stored_tenders": len(tenders),
                    "stored_versions": sum(len(repository.history(t.internal_id)) for t in tenders),
                    "exact_replay_verified": True,
                }
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            if trace is not None:
                performance_report = trace.write_report(
                    args.performance_report,
                    collection={
                        "run_id": str(run_id),
                        "source_id": source.id,
                        "recipe_version": recipe.version,
                        "publication": receipt.status,
                        "tenders_extracted": receipt.health.raw_count if receipt.health else None,
                        "tenders_stored": report["stored_tenders"],
                        "pagination_pages": len(receipt.health.diagnostics.pages)
                        if receipt.health
                        else None,
                        "normalization_issues": receipt.health.malformed_count
                        if receipt.health
                        else None,
                        "failures": 0 if receipt.status == "accepted" else 1,
                    },
                )
                trace.log_summary(performance_report)
            if receipt.status != "accepted":
                raise SystemExit(1)
    finally:
        http.close()
        engine.dispose()


if __name__ == "__main__":
    main()
