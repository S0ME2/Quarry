"""Ignored business fields never hide newly malformed source evidence at approval."""

from typing import cast
from uuid import uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import Engine

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.core.configuration import load_configuration
from tender_system.core.domain import ExecutionLimits, SourceRecord
from tender_system.operator.operations import OperationRestriction
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import session_factory
from tender_system.testing.acceptance_site import acceptance_site
from tender_system.testing.calibration import fixture_source_settings

pytestmark = pytest.mark.database


def test_fresh_approval_rejects_parse_failures_even_for_ignored_business_fields(
    migrated_engine: Engine,
) -> None:
    config = load_configuration(environ={}, validate_models=False)
    policy = fixture_source_settings().model_copy(
        update={"field_change_policy": {"value": "IGNORE"}}
    )
    policy = policy.model_copy(
        update={
            "normalization": policy.normalization.model_copy(
                update={"parse_failure_policy": "null_with_issue"}
            )
        }
    )
    with acceptance_site() as site:
        recipe = site.recipe(delayed_total=False)
        normalizer = ConfigurableTenderNormalizer()
        source = SourceRecord(id="acceptance", name="Fixture")
        raw = [cast(dict[str, JsonValue], dict(site.record(i), value="100")) for i in range(3)]
        normalized = [normalizer.normalize(row, source, policy).candidate for row in raw]
        assert all(candidate is not None for candidate in normalized)
        validation: dict[str, JsonValue] = {
            "execution_limits": ExecutionLimits(max_tenders=100).model_dump(mode="json"),
            "validation_scope": "sampled",
            "calibration": policy.calibration.model_dump(mode="json"),
            "approval_witnesses": [
                OperatorRuntime.candidate_state(candidate, policy)
                for candidate in normalized
                if candidate is not None
            ],
        }
        original = site.record
        site.record = lambda key: dict(original(key), value="malformed amount")  # type: ignore[method-assign]
        runtime = OperatorRuntime(
            session_factory(migrated_engine), config, evidence=MemoryEvidenceStore()
        )
        try:
            with pytest.raises(OperationRestriction, match="source policy"):
                runtime.fresh_source_check(uuid4(), "acceptance", recipe, policy, validation)
        finally:
            runtime.close()
