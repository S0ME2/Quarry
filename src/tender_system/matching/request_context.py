"""Bound matching requests without treating omitted document content as examined."""

import json
import re
from dataclasses import replace

from pydantic import JsonValue

from tender_system.core.configuration import ModelSettings
from tender_system.core.ports import ModelFailure, ModelRequest


def request_bytes(request: ModelRequest) -> int:
    return len(
        json.dumps(dict(request.payload), ensure_ascii=False, separators=(",", ":")).encode()
    ) + len(json.dumps(request.output_schema or {}, ensure_ascii=False).encode())


def bounded_context(request: ModelRequest, model: ModelSettings) -> ModelRequest:
    # Each UTF-8 byte is a conservative token bound, including non-Latin/OCR input.
    limit = min(
        model.max_input_bytes, model.context_window_tokens - request.max_output_tokens - 4096
    )
    fields = request.payload.get("tender")
    if not isinstance(fields, dict):
        raise ModelFailure("invalid_request", failure_stage="request_construction")
    document = fields.get("document_text")
    if request_bytes(request) <= limit:
        return request
    if not isinstance(document, str) or not document.strip():
        raise ModelFailure("request_too_large", failure_stage="request_construction")
    # OCR often contains pages of indentation. Preserve words, ordering and paragraphs.
    compact = "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in document.splitlines())
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    fields = {**fields, "document_text": compact}
    request = replace(request, payload={**request.payload, "tender": fields})
    if request_bytes(request) <= limit:
        return request

    # Select across the ENTIRE document, including its tail. Never trim requirements,
    # conditions, title, description or campaign constraints to fit a request.
    selection: dict[str, JsonValue] = {
        "complete": False,
        "original_document_bytes": len(document.encode()),
        "included_passages": 0,
        "total_passages": 0,
    }
    fields = {**fields, "document_text": ""}
    request = replace(
        request,
        payload={
            **request.payload,
            "tender": fields,
            "document_selection": selection,
            "instructions": str(request.payload.get("instructions", ""))
            + "\nDocument passages were omitted. Return unsure: omitted requirements or "
            "conditions could change relevance. Cite only supplied passages.",
        },
    )
    if request_bytes(request) > limit:
        raise ModelFailure("request_too_large", failure_stage="request_construction")
    # Keep chunks small while retaining word boundaries. The source remains unchanged.
    passages = re.findall(r".{1,700}(?:\s+|$)|\S{1,700}", compact, flags=re.DOTALL)
    terms = set(re.findall(r"\w{3,}", json.dumps(request.payload.get("campaign", {})).casefold()))
    obligations = re.compile(
        r"\b(must|shall|required|requirements|conditions|delivery|deadline|eligib\w*|"
        r"not|except|mandatory|payment|garant\w*|exigen\w*|oblig\w*)\b",
        re.I,
    )
    ranked = sorted(
        range(len(passages)),
        key=lambda i: (
            -sum(term in passages[i].casefold() for term in terms)
            - 2 * len(obligations.findall(passages[i])),
            i,
        ),
    )
    selection["total_passages"] = len(passages)
    chosen: set[int] = set()
    for index in ranked:
        candidate = chosen | {index}
        text = "\n[omitted passage boundary]\n".join(passages[i] for i in sorted(candidate))
        fields["document_text"] = text
        selection["included_passages"] = len(candidate)
        if request_bytes(request) <= limit:
            chosen = candidate
    fields["document_text"] = "\n[omitted passage boundary]\n".join(
        passages[i] for i in sorted(chosen)
    )
    selection["included_passages"] = len(chosen)
    if not chosen or request_bytes(request) > limit:
        raise ModelFailure("request_too_large", failure_stage="request_construction")
    return request
