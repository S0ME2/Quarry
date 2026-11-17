"""Explicit contracts for the existing finite/local fixture sources, never production defaults."""

from tender_system.core.configuration import SourceSettings


def fixture_source_settings(settings: SourceSettings | None = None) -> SourceSettings:
    values = (settings or SourceSettings()).model_dump()
    values["calibration"] = {
        "version": "local-fixture-v1",
        "status": "provisional",
        "evidence_references": [
            "tests/integration/test_source_runs.py",
            "tests/integration/test_browser.py",
        ],
        "full_inventory_supported": True,
        "completion_signals": ["snapshot", "total_reconciled", "end_marker"],
        "empty_policy": "reported_zero",
        "freshness": {
            "basis": "observed",
            "max_age_seconds": 86400,
            "timestamp_meaning": (
                "Local fixture exposes its complete current state at collection time."
            ),
        },
    }
    return SourceSettings.model_validate(values)
