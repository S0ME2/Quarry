"""Manual tender entry uses normal domain validation and provenance/versioning writes."""

from pydantic import ValidationError

from tender_system.core.domain import SourceRecord, Tender, TenderCandidate, TenderObservation
from tender_system.operator.operations import OperationRestriction, OperatorService
from tender_system.operator.operator_contracts import ManualTender, SourceURL
from tender_system.persistence.matching_storage import catalog_lock
from tender_system.persistence.storage import SqlSourceRepository, SqlTenderRepository


def enter_tender(svc: OperatorService, body: ManualTender) -> Tender:
    if body.source_url:
        SourceURL(url=body.source_url)
    try:
        candidate = TenderCandidate.model_validate(
            {
                **body.model_dump(exclude={"request_id"}),
                "source_id": "manual",
                "external_tender_id": str(body.request_id),
                "raw_data": {"provenance": "manual"},
            }
        )
    except ValidationError:
        raise OperationRestriction("Check the amount and timezone-aware deadline") from None
    catalog_lock(svc.session)
    sources_repo = SqlSourceRepository(svc.session)
    if sources_repo.get("manual") is None:
        sources_repo.add(SourceRecord(id="manual", name="Manually entered"))
        svc.write("source/manual", {"enabled": False, "manual_only": True})
    repo = SqlTenderRepository(svc.session)
    existing = repo.find(candidate)
    if existing:
        if existing.model_dump(include=set(TenderCandidate.model_fields)) != candidate.model_dump():
            raise OperationRestriction("Request ID already used for different tender data")
        return existing
    result = repo.observe(
        TenderObservation(
            id=body.request_id,
            candidate=candidate,
            observed_at=svc.now,
            evidence_reference="manual:" + str(body.request_id),
        ),
        svc.config.for_source("manual").rematch_fields,
    )
    return result.tender
