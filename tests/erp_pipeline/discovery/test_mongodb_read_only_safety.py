"""Step 33: production MongoDB inference must be read-only.

Relational discovery gets this guarantee structurally - a SQLAlchemy
``Inspector`` offers no way to write. A pymongo ``Database`` offers every way
to write, so the equivalent guarantee has to be checked rather than assumed.

These tests walk the AST of the production inference modules and assert that
no mutating driver call appears anywhere in them, and that the only driver
operations used at all are the three reads the phase needs.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

DISCOVERY_ROOT = (
    pathlib.Path(__file__).resolve().parents[3] / "src" / "erp_pipeline" / "discovery"
)

#: The production modules Phase 5 added. Test fixtures may seed an isolated
#: test database; these files may not write anything, anywhere, ever.
MONGO_MODULES = (
    DISCOVERY_ROOT / "mongodb.py",
    DISCOVERY_ROOT / "mongodb_inference.py",
)

#: Every pymongo call that creates, changes or destroys data or structure.
MUTATION_METHODS = frozenset(
    {
        "insert_one", "insert_many",
        "update_one", "update_many", "replace_one",
        "delete_one", "delete_many",
        "find_one_and_update", "find_one_and_replace", "find_one_and_delete",
        "bulk_write", "write", "save", "remove",
        "drop", "drop_collection", "drop_database", "drop_index", "drop_indexes",
        "create_collection", "create_index", "create_indexes", "create_search_index",
        "rename", "rename_collection",
        "command", "run_command",
        "map_reduce", "aggregate",
        "start_session", "with_transaction",
    }
)

#: The complete set of driver operations Phase 5 is allowed to perform.
ALLOWED_DRIVER_CALLS = frozenset(
    {"list_collections", "find", "estimated_document_count"}
)


def _tree(module_path: pathlib.Path) -> ast.Module:
    return ast.parse(module_path.read_text(encoding="utf-8"))


def _called_attribute_names(module_path: pathlib.Path) -> set[str]:
    """Every ``something.name(...)`` method name called in the module."""
    names: set[str] = set()

    for node in ast.walk(_tree(module_path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)

    return names


@pytest.mark.parametrize("module_path", MONGO_MODULES, ids=lambda p: p.name)
def test_no_mutating_driver_call_appears_in_production_inference(module_path):
    offenders = sorted(_called_attribute_names(module_path) & MUTATION_METHODS)

    assert offenders == [], f"{module_path.name} performs mutations: {offenders}"


@pytest.mark.parametrize("module_path", MONGO_MODULES, ids=lambda p: p.name)
def test_no_mutating_method_is_even_referenced(module_path):
    """Catches an indirect call - ``getattr(collection, "drop")()`` or a
    method handed around as a value - that a call-site scan would miss."""
    referenced: set[str] = set()

    for node in ast.walk(_tree(module_path)):
        if isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in MUTATION_METHODS:
                referenced.add(node.value)

    offenders = sorted(referenced & MUTATION_METHODS)
    assert offenders == [], f"{module_path.name} references mutations: {offenders}"


def test_the_only_driver_operations_used_are_the_three_reads():
    """Positive control: the read surface is small, stated, and enforced."""
    used = _called_attribute_names(DISCOVERY_ROOT / "mongodb.py")

    driver_calls = used & (ALLOWED_DRIVER_CALLS | MUTATION_METHODS)
    assert driver_calls == ALLOWED_DRIVER_CALLS


def test_inference_never_constructs_its_own_client():
    """It must go through the Phase 3 connector seam, so credentials, TLS and
    timeouts stay owned by one validated place."""
    for module_path in MONGO_MODULES:
        tree = _tree(module_path)

        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and not node.level
        }

        assert "pymongo" not in imported, f"{module_path.name} imports pymongo"
        assert "bson" not in imported, f"{module_path.name} imports bson"

        constructed = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "MongoClient" not in constructed, (
            f"{module_path.name} builds its own MongoClient"
        )


def test_the_inference_engine_is_driver_free_entirely():
    """``mongodb_inference`` must stay pure: no driver call of any kind, so
    every structural rule is unit-testable with plain dicts."""
    used = _called_attribute_names(DISCOVERY_ROOT / "mongodb_inference.py")

    assert not (used & ALLOWED_DRIVER_CALLS)
    assert not (used & MUTATION_METHODS)


def test_the_sample_query_filter_is_always_empty_and_bounded():
    """No caller-supplied query is ever executed: Phase 5 is not a remote
    query tool, and a filter would also break sampling reproducibility."""
    tree = _tree(DISCOVERY_ROOT / "mongodb.py")

    find_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "find"
    ]
    assert find_calls, "expected to find the sampling calls"

    for call in find_calls:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert "filter" in keywords, "find() must state its filter explicitly"
        assert isinstance(keywords["filter"], ast.Dict)
        assert keywords["filter"].keys == [], "the sample filter must be empty"
        assert "limit" in keywords, "find() must always be bounded by a limit"
