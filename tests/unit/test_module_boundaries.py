"""Enforce dependency directions for every core/matching module, including new files."""

import ast
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "tender_system"


def imports(node: ast.AST, module: str = "") -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Import):
            names.update(alias.name for alias in item.names)
        elif isinstance(item, ast.ImportFrom):
            base = item.module or ""
            if item.level:
                assert module, "Use package-qualified imports in business modules"
                parent = module.rsplit(".", item.level)[0]
                base = f"{parent}.{base}" if base else parent
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in item.names)
    return names


@pytest.mark.parametrize("path", sorted((PACKAGE / "core").glob("*.py")), ids=lambda p: p.name)
def test_core_depends_only_on_core(path: Path) -> None:
    for name in imports(ast.parse(path.read_text())):
        if name.startswith("tender_system."):
            assert name.startswith("tender_system.core"), (
                f"{path.name}: core cannot import {name}; inject a port instead"
            )
        assert name.split(".")[0] not in {"sqlalchemy", "psycopg", "httpx", "playwright"}, (
            f"{path.name}: infrastructure dependency {name} belongs in an adapter"
        )


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in sorted((PACKAGE / "matching").glob("*.py"))
        if p.stem not in {"matching_cli", "model_providers", "matcher_evaluation"}
    ],
    ids=lambda p: p.name,
)
def test_matching_business_modules_depend_only_on_core_and_matching(path: Path) -> None:
    for name in imports(ast.parse(path.read_text())):
        if name.startswith("tender_system."):
            assert name.startswith(("tender_system.core.", "tender_system.matching.")), (
                f"{path.name}: matching cannot import {name}; inject a port instead"
            )
        assert name.split(".")[0] not in {
            "sqlalchemy",
            "psycopg",
            "httpx",
            "playwright",
            "groq",
            "openai",
        }, f"{path.name}: concrete dependency {name} must stay behind a port"
        assert not name.startswith("tender_system.matching.model_providers"), (
            f"{path.name}: provider selection belongs at composition boundary"
        )


def test_import_time_graph_is_acyclic() -> None:
    modules = {
        "tender_system." + ".".join(path.relative_to(PACKAGE).with_suffix("").parts): path
        for path in PACKAGE.rglob("*.py")
    }
    graph = {}
    for module, path in modules.items():
        tree = ast.parse(path.read_text())
        # Deferred imports used for atomic publication/rematching are deliberate;
        # this rule prevents moving those edges into import-time initialization.
        dependencies = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                dependencies.update(imports(node, module) & modules.keys())
        graph[module] = dependencies
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as error:
        pytest.fail(f"Import-time dependency cycle: {error.args[1]}")


def test_generic_collection_contains_no_portal_names_or_endpoint_dispatch() -> None:
    for path in (PACKAGE / "collection").glob("*.py"):
        text = path.read_text().casefold()
        for portal in ("uzex", "uztender", "icppa", "tradelist", "gettrade"):
            assert portal not in text, (
                f"{path.name}: portal-specific {portal!r} belongs in recipes/fixtures"
            )


def test_witness_verification_is_independent_of_runtime_and_persistence() -> None:
    path = PACKAGE / "collection" / "verification.py"
    for name in imports(ast.parse(path.read_text())):
        assert not name.startswith(("tender_system.operator", "tender_system.persistence")), (
            f"Witness implementation imports {name}; use SampleVerifier/BrowserAdapter ports"
        )
