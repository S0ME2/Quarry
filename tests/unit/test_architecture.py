"""Protect concrete provider and source boundaries independently of fixture behavior."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "tender_system"
MODULE_PACKAGES = {
    "domain": "core",
    "configuration": "core",
    "tenders": "core",
    "health": "collection",
    "matching": "matching",
    "fixing": "repair",
    "source_runs": "collection",
    "intelligent_matching": "matching",
    "matcher_rules": "matching",
    "matcher_policy": "matching",
    "semantic_matching": "matching",
    "calibrated_semantics": "matching",
    "matcher_uncertainty": "matching",
}


@pytest.mark.parametrize(
    "module",
    [
        "domain",
        "configuration",
        "tenders",
        "health",
        "matching",
        "fixing",
        "source_runs",
        "intelligent_matching",
        "matcher_rules",
        "matcher_policy",
        "semantic_matching",
        "calibrated_semantics",
        "matcher_uncertainty",
    ],
)
def test_business_modules_do_not_import_concrete_storage_or_providers(module: str) -> None:
    path = PACKAGE / MODULE_PACKAGES[module] / f"{module}.py"
    tree = ast.parse(path.read_text())
    forbidden = {"sqlalchemy", "psycopg", "httpx", "playwright"}
    concrete = {
        "tender_system.persistence.storage",
        "tender_system.persistence.matching_storage",
        "tender_system.persistence.recipe_storage",
        "tender_system.collection.adapters",
        "tender_system.collection.browser",
        "tender_system.matching.model_providers",
    }
    for node in ast.walk(tree):
        imports = (
            [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else []
        )
        for imported in imports:
            assert imported.split(".")[0] not in forbidden, (module, imported)
            assert imported not in concrete, (module, imported)


def test_generic_modules_do_not_contain_uzex_branches_or_endpoints() -> None:
    for path in PACKAGE.rglob("*.py"):
        assert "uzex" not in path.read_text().casefold(), path.name
