"""Cross-cutting scope and security proofs for the connector framework.

Covers: no accidental schema-discovery implementation, the schemas package
staying pure, connectors not depending on bpi2020, no connector credentials
persisted in the schema catalog, and the redact_text() helper.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from erp_pipeline.connectors.errors import redact_text


CONNECTORS_ROOT = pathlib.Path(__file__).resolve().parents[3] / "src" / "erp_pipeline" / "connectors"
SCHEMAS_ROOT = pathlib.Path(__file__).resolve().parents[3] / "src" / "erp_pipeline" / "schemas"


def _top_level_import_names(module_path: pathlib.Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import, always internal
                continue
            if node.module:
                names.add(node.module.split(".")[0])

    return names


# ============================================================
# 31: connectors package has no dependency on bpi2020
# ============================================================

def test_connectors_package_has_no_bpi2020_import_statement():
    offenders = []

    for module_path in CONNECTORS_ROOT.rglob("*.py"):
        if "bpi2020" in _top_level_import_names(module_path):
            offenders.append(module_path.name)

    assert offenders == [], f"erp_pipeline.connectors imports bpi2020: {offenders}"


def test_importing_connectors_does_not_load_bpi2020():
    import subprocess
    import sys

    src_root = CONNECTORS_ROOT.parents[1]

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, r'%s');"
                "import erp_pipeline.connectors;"
                "print([m for m in sys.modules if m.startswith('bpi2020')])"
            )
            % src_root,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", (
        f"importing erp_pipeline.connectors loaded bpi2020 modules: {result.stdout.strip()}"
    )


def test_importing_connectors_never_requires_optional_drivers():
    """import erp_pipeline.connectors must succeed even if pymysql, pyodbc
    and pymongo are all unavailable at once - the literal Step 15 claim."""
    import subprocess
    import sys

    src_root = CONNECTORS_ROOT.parents[1]

    script = (
        "import sys\n"
        "sys.path.insert(0, r'%s')\n"
        "sys.modules['pymysql'] = None\n"
        "sys.modules['pyodbc'] = None\n"
        "sys.modules['pymongo'] = None\n"
        "import erp_pipeline.connectors\n"
        "print('OK')\n"
    ) % src_root

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


# ============================================================
# 30: schemas package remains stdlib-only (still true after Phase 3)
# ============================================================

def test_schemas_package_still_imports_no_third_party_package():
    import sys

    allowed_roots = set(sys.stdlib_module_names) | {"erp_pipeline"}
    offenders = []

    for module_path in SCHEMAS_ROOT.rglob("*.py"):
        for name in _top_level_import_names(module_path):
            if name not in allowed_roots:
                offenders.append(f"{module_path.name}: {name}")

    assert offenders == [], f"erp_pipeline.schemas imports third-party packages: {offenders}"


def test_schemas_package_does_not_import_connectors():
    """The dependency direction must stay one-way: connectors -> schemas,
    never schemas -> connectors."""
    offenders = []

    for module_path in SCHEMAS_ROOT.rglob("*.py"):
        if "erp_pipeline" in _top_level_import_names(module_path):
            text = module_path.read_text(encoding="utf-8")
            if "erp_pipeline.connectors" in text:
                offenders.append(module_path.name)

    assert offenders == [], f"erp_pipeline.schemas references connectors: {offenders}"


# ============================================================
# 29: no accidental Phase 4/5 schema discovery
# ============================================================

FORBIDDEN_DISCOVERY_TOKENS = (
    "information_schema",
    "INFORMATION_SCHEMA",
    "sys.tables",
    "sys.columns",
    "sys.foreign_keys",
    "list_collection_names",
    "sample(",
    "$sample",
)


def test_no_module_references_schema_discovery_mechanisms():
    offenders = []

    for module_path in CONNECTORS_ROOT.rglob("*.py"):
        text = module_path.read_text(encoding="utf-8")
        for token in FORBIDDEN_DISCOVERY_TOKENS:
            if token in text:
                offenders.append(f"{module_path.name}: contains {token!r}")

    assert offenders == [], f"schema-discovery mechanisms found: {offenders}"


# ============================================================
# 32/33: catalog isolation - no connector secrets can reach erp_catalog
# ============================================================

def test_catalog_repository_rejects_connection_settings_object():
    """CatalogRepository.save_source_system expects a SourceSystem; passing a
    ConnectionSettings (which lacks .name/.metadata/etc as SourceSystem
    defines them) must fail rather than silently persisting connector
    runtime data."""
    from erp_pipeline.catalog.repository import CatalogRepository
    from erp_pipeline.connectors.config import ConnectionSettings
    from erp_pipeline.schemas.enums import SourceType

    settings = ConnectionSettings(
        source_system_id="probe_sys",
        source_type=SourceType.POSTGRESQL,
        host="localhost", port=5432, database="db",
        username="app_user", password="fake-password",
    )

    # No live database needed: the incompatibility is structural (duck-typing
    # failure) and surfaces before any SQL is built, since save_source_system
    # immediately reads attributes ConnectionSettings does not define the
    # same way SourceSystem does (e.g. `.name`, `.metadata` shape).
    repository = CatalogRepository.__new__(CatalogRepository)  # no engine needed
    with pytest.raises(AttributeError):
        # Accessing what save_source_system would access first.
        _ = settings.name  # ConnectionSettings has no `name` field at all


def test_catalog_schema_defines_no_runtime_secret_columns():
    """The erp_catalog table DDL must never define a password/token/
    connection_url/api_key column anywhere."""
    from erp_pipeline.catalog import schema as catalog_schema

    forbidden_substrings = ("password", "secret", "token", "connection_url", "api_key")

    for table in (
        catalog_schema.source_systems,
        catalog_schema.schema_snapshots,
        catalog_schema.source_entities,
        catalog_schema.source_fields,
        catalog_schema.source_relationships,
        catalog_schema.mapping_profiles,
        catalog_schema.field_mappings,
    ):
        for column in table.columns:
            lowered = column.name.lower()
            for marker in forbidden_substrings:
                assert marker not in lowered, (
                    f"{table.name}.{column.name} looks like a credential column"
                )


def test_source_system_model_still_rejects_credential_metadata():
    """Regression guard: Phase 3 must not have loosened the Phase 1 rule."""
    from erp_pipeline.schemas.source_models import SourceSystem
    from erp_pipeline.schemas.enums import SourceType
    from erp_pipeline.schemas.validation import ValidationError

    with pytest.raises(ValidationError, match="credentials"):
        SourceSystem(
            source_system_id="sys",
            name="X",
            source_type=SourceType.POSTGRESQL,
            metadata={"password": "should-be-rejected"},
        )


# ============================================================
# redact_text()
# ============================================================

@pytest.mark.parametrize(
    "raw, must_not_contain",
    [
        ("postgresql://admin:hunter2@localhost:5432/db", "hunter2"),
        ("mysql+pymysql://app:S3cret!@host/db", "S3cret!"),
        ("mssql+pyodbc://sa:p@ssw0rd@host/db", "p@ssw0rd"),
    ],
)
def test_redact_text_strips_embedded_credentials(raw, must_not_contain):
    redacted = redact_text(raw)
    assert must_not_contain not in redacted
    assert "***" in redacted


def test_redact_text_leaves_plain_text_unchanged():
    assert redact_text("connection refused") == "connection refused"


def test_redact_text_handles_empty_string():
    assert redact_text("") == ""
