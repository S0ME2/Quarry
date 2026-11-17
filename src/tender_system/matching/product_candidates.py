"""Bounded, high-recall product context for semantic matching."""

import json
import re
from dataclasses import dataclass

from pydantic import JsonValue

from tender_system.core.domain import CampaignCriteria, Tender

_MAX_PRODUCTS = 18
_MAX_CONTEXT_CHARS = 6_000
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class ProductSelection:
    candidates: tuple[dict[str, JsonValue], ...]
    complete: bool
    total: int


def select_product_candidates(
    tender: Tender, criteria: CampaignCriteria, *, compact_context: bool = False
) -> ProductSelection:
    """Rank all products; omission is explicit and forces abstention on negatives."""
    from tender_system.matching.semantic_matching import normalize_product_items

    products = normalize_product_items(tender)
    if not products:
        return ProductSelection((), True, 0)

    campaign_text = " ".join(
        (
            tender.title or "",
            criteria.name,
            criteria.description,
            criteria.custom_notes,
            *criteria.include_keywords,
        )
    )
    terms = set(_WORD.findall(campaign_text.casefold()))

    def candidate(index: int) -> dict[str, JsonValue]:
        item = products[index]
        attributes = item["attributes"]
        assert isinstance(attributes, dict)
        source_id = next(
            (
                value
                for key, value in attributes.items()
                if re.sub(r"[^a-z0-9]", "", key.casefold())
                in {"id", "productid", "itemid", "goodsid", "productcode", "sku"}
                and isinstance(value, (str, int, float))
                and not isinstance(value, bool)
            ),
            None,
        )
        # Keep unknown specifications and distinct translations/category descriptions.
        # Drop only source bookkeeping, empty values and repeated text.
        seen = {value for value in (item["name"], item["description"]) if isinstance(value, str)}

        def compact(value: JsonValue, *, text_alias: bool = False) -> JsonValue:
            if isinstance(value, dict):
                result: dict[str, JsonValue] = {}
                for key, entry in value.items():
                    if key in {"uid", "category_id", "lot_id"}:
                        continue
                    if (
                        key == "id"
                        and isinstance(entry, str)
                        and re.fullmatch(
                            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", entry
                        )
                    ):
                        continue
                    duplicate_label = re.sub(r"[^a-z0-9]", "", key.casefold()) in {
                        "name",
                        "title",
                        "description",
                        "translatename",
                        "productname",
                        "itemname",
                        "goodsname",
                        "servicename",
                        "productdescription",
                        "itemdescription",
                        "goodsdescription",
                        "servicedescription",
                    }
                    retained = compact(entry, text_alias=text_alias or duplicate_label)
                    if retained is not None and retained != {} and retained != []:
                        result[key] = retained
                return result
            if isinstance(value, list):
                # Preserve list positions for citation paths.
                return [compact(entry) for entry in value]
            if isinstance(value, str):
                if not value.strip() or (text_alias and value in seen):
                    return None
                if text_alias:
                    seen.add(value)
            return value

        return {
            "source_index": index,
            "source_product_id": source_id,
            "name": item["name"],
            "description": item["description"],
            "attributes": compact(attributes) if compact_context else attributes,
        }

    ranked = sorted(
        range(len(products)),
        key=lambda index: (
            -len(
                terms
                & set(
                    _WORD.findall(
                        " ".join(
                            str(products[index][field] or "") for field in ("name", "description")
                        ).casefold()
                    )
                )
            ),
            index,
        ),
    )

    serialized = [candidate(index) for index in ranked]
    all_chars = sum(len(json.dumps(item, ensure_ascii=False)) for item in serialized)
    if len(products) <= _MAX_PRODUCTS and all_chars <= _MAX_CONTEXT_CHARS:
        return ProductSelection(tuple(serialized), True, len(products))

    selected: list[dict[str, JsonValue]] = []
    used_chars = 0
    for item in serialized:
        size = len(json.dumps(item, ensure_ascii=False))
        if len(selected) == _MAX_PRODUCTS:
            break
        if used_chars + size > _MAX_CONTEXT_CHARS:
            continue
        selected.append(item)
        used_chars += size
    selected.sort(
        key=lambda item: item["source_index"] if type(item["source_index"]) is int else -1
    )
    complete = len(selected) == len(products) and all_chars <= _MAX_CONTEXT_CHARS
    return ProductSelection(tuple(selected), complete, len(products))
