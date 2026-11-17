"""Customer source eligibility, shared by API, scheduler and collection worker."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_system.collection.validation_evidence import fingerprint
from tender_system.persistence.storage import (
    AccountSourceRow,
    ActiveRecipeRow,
    OperationRow,
    OperatorConfigRow,
    RecipeRevisionRow,
)


def decision(session: Session, source_id: str) -> str:
    review = session.get(OperatorConfigRow, "source-review/" + source_id, populate_existing=True)
    if review:
        value = review.value.get("decision")
        return value if isinstance(value, str) and value in {"approved", "rejected"} else "pending"
    # The original shared catalog was already administrator-controlled. Private
    # customer submissions have never had an independent administrator decision.
    return "pending" if session.get(AccountSourceRow, source_id) else "approved"


def verified(session: Session, source_id: str) -> bool:
    active = session.get(ActiveRecipeRow, source_id)
    if active is None:
        return False
    revision = session.get(RecipeRevisionRow, (source_id, active.version))
    recipe = revision.revision.get("recipe") if revision else None
    if not isinstance(recipe, dict):
        return False
    expected = fingerprint(recipe)
    operations = session.scalars(
        select(OperationRow).where(
            OperationRow.source_id == source_id, OperationRow.status == "activated"
        )
    )
    return any(
        op.result.get("validated_recipe_fingerprint") == expected
        and isinstance(op.result.get("validation_evidence_reference"), str)
        and isinstance(op.result.get("activation_operation_id"), str)
        for op in operations
    )


def eligible(session: Session, source_id: str) -> bool:
    if decision(session, source_id) != "approved" or not verified(session, source_id):
        return False
    control = session.get(OperatorConfigRow, "source/" + source_id, populate_existing=True)
    return not control or (
        bool(control.value.get("enabled", True)) and not control.value.get("manual_only", False)
    )
