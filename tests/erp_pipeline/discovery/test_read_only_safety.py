"""Read-only safety, package isolation, and discovery scope boundaries.

Proves by source inspection that the production discovery modules contain no
write SQL, do not depend on bpi2020, and have not strayed into Phase 6+
territory.

MongoDB-specific read-only proof (no insert/update/delete/drop/aggregate
anywhere in the Phase 5 modules) lives in
``test_mongodb_read_only_safety.py``, which walks their AST rather than
scanning for SQL.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

DISCOVERY_ROOT = pathlib.Path(__file__).resolve().parents[3] / "src" / "erp_pipeline" / "discovery"
SCHEMAS_ROOT = pathlib.Path(__file__).resolve().parents[3] / "src" / "erp_pipeline" / "schemas"

#: Production modules. Test fixtures/probes may legitimately create and drop
#: isolated test structures; these files may not.
PRODUCTION_MODULES = sorted(DISCOVERY_ROOT.rglob("*.py"))


def _source_of(module_path: pathlib.Path) -> str:
    return module_path.read_text(encoding="utf-8")


def _code_without_comments_or_docstrings(module_path: pathlib.Path) -> str:
    """Strip docstrings and comments so prose mentioning 'no DDL, no DML' does
    not trip a naive keyword scan."""
    source = _source_of(module_path)
    tree = ast.parse(source)

    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    stripped_lines = []
    for line in source.splitlines():
        without_comment = line.split("#", 1)[0]
        stripped_lines.append(without_comment)

    code = "\n".join(stripped_lines)
    for doc in docstrings:
        code = code.replace(doc, "")

    return code


# ============================================================
# No write SQL in production discovery modules
# ============================================================

WRITE_SQL_PATTERNS = (
    r"\bCREATE\s+TABLE\b",
    r"\bCREATE\s+SCHEMA\b",
    r"\bCREATE\s+INDEX\b",
    r"\bALTER\s+TABLE\b",
    r"\bDROP\s+TABLE\b",
    r"\bDROP\s+SCHEMA\b",
    r"\bINSERT\s+INTO\b",
    r"\bUPDATE\s+\w+\s+SET\b",
    r"\bDELETE\s+FROM\b",
    r"\bTRUNCATE\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
)


@pytest.mark.parametrize("module_path", PRODUCTION_MODULES, ids=lambda p: p.name)
def test_production_discovery_module_contains_no_write_sql(module_path):
    """No production discovery module may contain a write statement.

    Checked two ways: no write pattern anywhere in executable code, and no
    string literal that begins with a write keyword. Docstrings and comments
    are stripped first so prose such as "issues no DDL" is not a false hit.
    """
    code = _code_without_comments_or_docstrings(module_path).upper()

    offenders = [
        pattern for pattern in WRITE_SQL_PATTERNS if re.search(pattern, code)
    ]
    assert offenders == [], f"{module_path.name} contains write SQL: {offenders}"

    write_literals = [
        literal
        for literal in _sql_string_literals(module_path)
        if not literal.strip().upper().startswith("SELECT")
    ]
    assert write_literals == [], (
        f"{module_path.name} contains a non-SELECT SQL literal: {write_literals}"
    )


def test_only_select_statements_appear_in_profiling():
    """Every SQL literal built by the profiler must be a SELECT aggregate."""
    code = _code_without_comments_or_docstrings(DISCOVERY_ROOT / "profiling.py")

    sql_fragments = re.findall(r'f?"(SELECT[^"]*)"', code, flags=re.IGNORECASE)
    assert sql_fragments, "expected to find SQL literals in profiling.py"

    for fragment in sql_fragments:
        upper = fragment.upper()
        assert upper.startswith("SELECT"), fragment
        assert any(
            aggregate in upper for aggregate in ("COUNT(", "MIN(", "MAX(", "AVG(")
        ), f"non-aggregate SELECT in profiling: {fragment}"


def _sql_string_literals(module_path: pathlib.Path) -> list[str]:
    """Every string literal in the module that looks like a SQL statement.

    Parses the AST rather than scanning raw text, so an ordinary Python
    identifier that merely contains a SQL keyword (``selected``, ``updated``)
    cannot register as SQL.
    """
    tree = ast.parse(_source_of(module_path))
    # Word boundary matters: the identifier "create_inspector" must not be
    # mistaken for a CREATE statement.
    statement_start = re.compile(
        r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE)\b\s",
        flags=re.IGNORECASE,
    )

    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if statement_start.match(node.value):
                literals.append(node.value)

    return literals


def test_structural_discovery_module_builds_no_sql_at_all():
    """relational.py must go through the Inspector, never hand-written SQL."""
    literals = _sql_string_literals(DISCOVERY_ROOT / "relational.py")
    assert literals == [], (
        f"relational.py should use the Inspector, not raw SQL: {literals}"
    )


def test_no_raw_information_schema_or_system_catalog_sql():
    """Step 29: use the Inspector rather than engine-specific catalog SQL."""
    for module_path in PRODUCTION_MODULES:
        code = _code_without_comments_or_docstrings(module_path).upper()
        for token in ("INFORMATION_SCHEMA.", "SYS.TABLES", "SYS.COLUMNS", "PG_CATALOG."):
            assert token not in code, f"{module_path.name} queries {token} directly"


# ============================================================
# Package isolation
# ============================================================

def _top_level_imports(module_path: pathlib.Path) -> set[str]:
    tree = ast.parse(_source_of(module_path))
    names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                names.add(node.module.split(".")[0])

    return names


def test_discovery_package_has_no_bpi2020_import():
    offenders = [
        module_path.name
        for module_path in PRODUCTION_MODULES
        if "bpi2020" in _top_level_imports(module_path)
    ]
    assert offenders == [], f"erp_pipeline.discovery imports bpi2020: {offenders}"


def test_importing_discovery_does_not_load_bpi2020():
    import subprocess
    import sys

    src_root = DISCOVERY_ROOT.parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, r'%s');"
                "import erp_pipeline.discovery;"
                "print([m for m in sys.modules if m.startswith('bpi2020')])"
            )
            % src_root,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_schemas_package_remains_stdlib_only():
    """Phase 4 must not have loosened the Phase 1 purity boundary."""
    import sys

    allowed = set(sys.stdlib_module_names) | {"erp_pipeline"}
    offenders = []

    for module_path in SCHEMAS_ROOT.rglob("*.py"):
        for name in _top_level_imports(module_path):
            if name not in allowed:
                offenders.append(f"{module_path.name}: {name}")

    assert offenders == [], f"erp_pipeline.schemas imports third-party: {offenders}"


def test_schemas_package_does_not_import_discovery():
    """Dependency direction stays one-way: discovery -> schemas."""
    offenders = [
        module_path.name
        for module_path in SCHEMAS_ROOT.rglob("*.py")
        if "erp_pipeline.discovery" in _source_of(module_path)
    ]
    assert offenders == []


# ============================================================
# Scope boundary
# ============================================================

#: Two different kinds of boundary, both still enforced now that Phase 5 has
#: landed in this package.
#:
#: The MongoDB tokens are NOT "Phase 5 is unimplemented" markers any more -
#: they are how Phase 5 is required to be implemented. Inference must reach
#: MongoDB through the Phase 3 connector's ``create_database_handle()`` seam,
#: so no discovery module may import the driver or build its own client; and
#: sampling must be reproducible, so ``$sample`` and ``aggregate(`` stay
#: banned outright.
#:
#: The remaining tokens mark genuinely later phases: API, vector storage and
#: embeddings.
FORBIDDEN_SCOPE_TOKENS = (
    "pymongo",
    "MongoClient",
    "find_one",
    "aggregate(",
    "$sample",
    "fastapi",
    "FastAPI",
    "qdrant",
    "SentenceTransformer",
    "embedding",
)


def test_discovery_does_not_stray_into_later_phase_features():
    offenders = []

    for module_path in PRODUCTION_MODULES:
        code = _code_without_comments_or_docstrings(module_path)
        for token in FORBIDDEN_SCOPE_TOKENS:
            if token in code:
                offenders.append(f"{module_path.name}: {token}")

    assert offenders == [], f"out-of-scope features found: {offenders}"


def test_discovery_exposes_no_mapping_or_etl_api():
    import erp_pipeline.discovery as discovery

    forbidden = {
        "map_fields", "suggest_mapping", "apply_mapping", "transform_rows",
        "extract_rows", "run_etl", "infer_semantic_type",
    }
    assert not (set(dir(discovery)) & forbidden)
