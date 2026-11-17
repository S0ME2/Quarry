"""Measured localhost acquisition budgets; never estimates third-party latency."""

import json
import time
import tracemalloc
from pathlib import Path
from uuid import uuid4

import pytest

from tender_system.collection.adapters import FileEvidenceStore, HttpxAdapter
from tender_system.collection.recipes import RecipeExecutor
from tender_system.core.domain import CollectionRun, ExecutionContext, ExecutionLimits
from tender_system.testing.acceptance_site import acceptance_site
from tender_system.testing.calibration import fixture_source_settings


@pytest.mark.parametrize("limit", [100, None], ids=["bounded100", "full3000"])
def test_measured_local_acquisition_scope_and_requests(limit: int | None) -> None:
    root = Path(__file__).resolve().parents[2] / "artifacts" / "tests" / "reliability-audit"
    evidence = FileEvidenceStore(root / "performance-evidence" / uuid4().hex)
    http = HttpxAdapter()
    tracing_owned = not tracemalloc.is_tracing()
    try:
        with acceptance_site(count=3000) as site:
            recipe = site.recipe(delayed_total=False)
            if tracing_owned:
                tracemalloc.start()
            started = time.perf_counter()
            run = RecipeExecutor(http, evidence, fixture_source_settings()).execute(
                recipe,
                ExecutionContext(
                    run_id=uuid4(),
                    limits=ExecutionLimits(
                        max_tenders=limit, max_requests=10000, max_duration_seconds=600
                    ),
                ),
            )
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            expected = limit or 3000
            assert isinstance(run, CollectionRun), run
            assert len(run.raw_observations) == expected
            assert all(
                isinstance(row["product_items"], list) and len(row["product_items"]) == 2
                for row in run.raw_observations
            )
            assert (
                run.diagnostics.transport_attempts
                == len(site.requests)
                == expected + expected // 50
            )
            assert (run.coverage == "sampled") is (limit is not None)
            report = {
                "scope": "sampled" if limit else "full",
                "records": expected,
                "source_records": 3000,
                "physical_http_requests": len(site.requests),
                "elapsed_seconds": elapsed,
                "tracemalloc_peak_bytes": peak,
                "response_bytes": run.diagnostics.response_bytes,
                "measurement": "local HTTP acquisition + file evidence only; tracemalloc enabled",
                "database_writes": False,
                "browser_requests": 0,
                "live_duration_estimate": None,
            }
            (root / f"performance-collection-{expected}.json").write_text(
                json.dumps(report, indent=2)
            )
    finally:
        if tracing_owned:
            tracemalloc.stop()
        http.close()
