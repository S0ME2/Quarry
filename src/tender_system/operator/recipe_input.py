"""Shared validation for manually supplied declarative recipes."""

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tender_system.operator.operations import OperationRestriction, sanitized
from tender_system.operator.operator_contracts import SourceURL


class ManualCommandError(OperationRestriction):
    """Messages authored by this CLI, safe to show without provider exception text."""


def validate_recipe_content(value: Any) -> None:
    """Reject credential-bearing recipes without changing their declarative semantics."""

    def check(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: check(child) for key, child in item.items()}
        if isinstance(item, list):
            return [check(child) for child in item]
        if isinstance(item, str) and item.startswith(("https://", "http://")):
            SourceURL(url=item)
            url = urlsplit(item)
            public = urlunsplit((url.scheme, url.netloc, url.path, "", ""))
            if sanitized(item) != public:
                raise ManualCommandError("recipe contains secret values")
            return item
        if sanitized(item) != item:
            raise ManualCommandError("recipe contains secret values")
        return item

    check(value)

    # Key-based credential protection; compare after removing URL query strings only.
    def without_queries(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: without_queries(child) for key, child in item.items()}
        if isinstance(item, list):
            return [without_queries(child) for child in item]
        if isinstance(item, str) and item.startswith(("https://", "http://")):
            return sanitized(item)
        return item

    if sanitized(value) != without_queries(value):
        raise ManualCommandError("recipe contains credential-bearing keys")


def recipe_url(recipe: Any) -> str:
    def urls(value: Any) -> Any:
        if isinstance(value, dict):
            for child in value.values():
                found = urls(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = urls(child)
                if found:
                    return found
        elif isinstance(value, str) and value.startswith(("https://", "http://")):
            return value
        return None

    url = urls(recipe.model_dump(mode="json"))
    if not url:
        raise ManualCommandError("recipe must contain an HTTP(S) source URL")
    return SourceURL(url=url).url
