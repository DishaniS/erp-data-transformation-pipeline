"""Identity rules, serialization guarantees, and Phase 0 compatibility.

The compatibility tests import BOTH ``bpi2020.common.stable_ids`` and
``erp_pipeline.schemas.identity`` to prove the two packages agree on identity
principles. Only the tests do this: the ``erp_pipeline`` package itself never
imports ``bpi2020``, so the framework carries no dependency on the prototype.
"""

import uuid

import pytest

# Phase 0 (protected baseline)
from bpi2020.common import stable_ids as phase0
# Phase 1 (new generic framework)
from erp_pipeline.schemas import identity as phase1
from erp_pipeline.schemas import (
    CANONICAL_MODEL_VERSION,
    MAPPING_MODEL_VERSION,
    RUN_MODEL_VERSION,
    SOURCE_MODEL_VERSION,
    IdentityError,
    SerializationError,
    to_json_value,
    to_rfc3339,
    utc_now,
)


# A corpus that exercises the awkward cases: spaces, mixed case, punctuation,
# leading underscores, repeated separators, unicode and numerics.
NORMALIZATION_CORPUS = [
    "declaration 100000",
    "travel permit 76455",
    "Request For Payment 73550",
    "INV-001",
    "_id",
    "  padded  value  ",
    "weird/chars*here",
    "Fin.Invoice",
    "UPPER_CASE_NAME",
    "multiple___underscores",
    "trailing___",
    "___leading",
    "tab\tseparated",
    "new\nline",
    "café_münchen",
    "12345",
    "a:b:c",
    "erp:sys:invoice:1",
]


# ============================================================
# Phase 0 <-> Phase 1 compatibility
# ============================================================

@pytest.mark.parametrize("value", NORMALIZATION_CORPUS)
def test_normalization_matches_phase0_byte_for_byte(value):
    """Both packages must normalize identically or ids would silently diverge."""
    assert phase1.normalize_identifier(value) == phase0.normalize_key_component(value)


@pytest.mark.parametrize("value", [None, "", "   ", "___", "***"])
def test_both_packages_reject_the_same_unusable_values(value):
    with pytest.raises(phase0.StableIdError):
        phase0.normalize_key_component(value)

    with pytest.raises(IdentityError):
        phase1.normalize_identifier(value)


def test_uuid_derivation_uses_the_same_algorithm_as_phase0():
    """Same algorithm (uuid5 over NAMESPACE_URL), different namespace prefix."""
    record_id = "case:domestic_declarations:declaration_100000"

    phase0_uuid = phase0.make_qdrant_point_id(record_id)
    phase1_uuid = phase1.make_deterministic_uuid(record_id, namespace_prefix="bpi2020")

    # Given the same prefix, the two derivations are identical.
    assert phase1_uuid == phase0_uuid
    assert uuid.UUID(phase1_uuid).version == 5


def test_default_namespaces_keep_the_two_id_spaces_apart():
    """The same string must not map to the same UUID in both packages."""
    record_id = "case:domestic_declarations:declaration_100000"

    assert phase1.make_deterministic_uuid(record_id) != phase0.make_qdrant_point_id(
        record_id
    )


def test_phase0_and_phase1_id_namespaces_cannot_collide():
    """Phase 0 ids start with event:/case:/document:, Phase 1 ids with erp:."""
    phase0_ids = [
        phase0.make_event_record_id("domestic_declarations_raw", 1),
        phase0.make_case_record_id("domestic_declarations", "declaration 100000"),
        phase0.make_document_record_id("a102d03b6986f92816520534"),
    ]
    phase1_ids = [
        phase1.make_canonical_record_id("finance_erp_pg", "invoice", "INV-001"),
        phase1.make_canonical_document_id("policy_library", "a102d03b"),
    ]

    for identifier in phase0_ids:
        assert not identifier.startswith(f"{phase1.CANONICAL_ID_PREFIX}:")

    for identifier in phase1_ids:
        assert identifier.startswith(f"{phase1.CANONICAL_ID_PREFIX}:")

    assert not (set(phase0_ids) & set(phase1_ids))


def test_erp_pipeline_has_no_bpi2020_import_statement():
    """The framework must not depend on the source-specific prototype.

    Only import statements count. The modules do *mention* bpi2020 in
    docstrings, which is deliberate: the relationship between the two identity
    schemes has to be documented where a reader will find it.
    """
    import ast
    import pathlib

    package_root = pathlib.Path(phase1.__file__).resolve().parents[1]
    offenders = []

    for module_path in package_root.rglob("*.py"):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue

            for name in names:
                if name.split(".")[0] == "bpi2020":
                    offenders.append(f"{module_path.name}: imports {name}")

    assert offenders == [], f"erp_pipeline imports bpi2020: {offenders}"


def test_importing_erp_pipeline_does_not_load_bpi2020():
    """Runtime proof, complementing the static check above."""
    import pathlib
    import subprocess
    import sys

    src_root = pathlib.Path(phase1.__file__).resolve().parents[2]

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, r'%s');"
                "import erp_pipeline.schemas;"
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
        f"importing erp_pipeline loaded bpi2020 modules: {result.stdout.strip()}"
    )


def test_erp_pipeline_schemas_imports_no_third_party_package():
    """The Phase 1 contracts are stdlib-only.

    Scoped to ``erp_pipeline/schemas/`` specifically, not the whole
    ``erp_pipeline`` package: Phase 2 added ``erp_pipeline.catalog``, which is
    explicitly allowed to depend on SQLAlchemy/psycopg2 for persistence (see
    ``erp_pipeline.catalog``'s own module docstring). The boundary this test
    protects is that ``schemas/`` - the pure contract layer - never gains such
    a dependency, not that no subpackage of ``erp_pipeline`` ever does.
    """
    import ast
    import pathlib
    import sys

    package_root = pathlib.Path(phase1.__file__).resolve().parent
    allowed_roots = set(sys.stdlib_module_names) | {"erp_pipeline"}
    offenders = []

    for module_path in package_root.rglob("*.py"):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import, always internal
                    continue
                names = [node.module or ""]
            else:
                continue

            for name in names:
                root = name.split(".")[0]
                if root and root not in allowed_roots:
                    offenders.append(f"{module_path.name}: {name}")

    assert offenders == [], f"unexpected third-party imports: {offenders}"


def test_content_hash_shares_phase0_properties():
    """Different envelopes by design, but the same determinism guarantees."""
    phase0_hash = phase0.compute_content_hash("r1", "text", {"b": 2, "a": 1})
    phase1_hash = phase1.compute_content_hash("r1", {"b": 2, "a": 1}, "text")

    # Both are SHA-256 hex digests.
    for digest in (phase0_hash, phase1_hash):
        assert len(digest) == 64
        int(digest, 16)

    # Both are key-order independent and None-insensitive.
    assert phase0_hash == phase0.compute_content_hash(
        "r1", "text", {"a": 1, "b": 2, "c": None}
    )
    assert phase1_hash == phase1.compute_content_hash(
        "r1", {"a": 1, "b": 2, "c": None}, "text"
    )


# ============================================================
# Canonical id rules
# ============================================================

def test_canonical_id_format():
    assert (
        phase1.make_canonical_record_id("finance_erp_pg", "invoice", "INV-001")
        == "erp:finance_erp_pg:invoice:inv-001"
    )


def test_canonical_document_id_uses_the_same_grammar():
    document_id = phase1.make_canonical_document_id("policy_library", "abc123")

    assert document_id == "erp:policy_library:document:abc123"
    assert phase1.parse_canonical_id(document_id)[1] == phase1.DOCUMENT_ENTITY_TYPE


def test_parse_rejects_a_non_canonical_id():
    with pytest.raises(IdentityError, match="not a canonical id"):
        phase1.parse_canonical_id("case:domestic_declarations:declaration_1")


def test_parse_rejects_the_wrong_component_count():
    with pytest.raises(IdentityError):
        phase1.parse_canonical_id("erp:sys:invoice")


def test_boolean_keys_are_rejected_as_identity_components():
    with pytest.raises(IdentityError, match="boolean"):
        phase1.normalize_identifier(True)


def test_is_normalized_identifier_is_a_fixed_point_check():
    assert phase1.is_normalized_identifier("invoice_id") is True
    assert phase1.is_normalized_identifier("Invoice Id") is False
    assert phase1.is_normalized_identifier("") is False
    assert phase1.is_normalized_identifier(None) is False

    for value in NORMALIZATION_CORPUS:
        normalized = phase1.normalize_identifier(value)
        assert phase1.is_normalized_identifier(normalized) is True


def test_normalization_is_idempotent():
    for value in NORMALIZATION_CORPUS:
        once = phase1.normalize_identifier(value)
        assert phase1.normalize_identifier(once) == once


def test_deterministic_uuid_rejects_empty_input():
    with pytest.raises(IdentityError):
        phase1.make_deterministic_uuid("")


# ============================================================
# Serialization guarantees
# ============================================================

def test_utc_now_is_timezone_aware():
    assert utc_now().tzinfo is not None


def test_rfc3339_rendering_is_stable():
    from datetime import datetime, timezone

    value = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert to_rfc3339(value) == "2026-08-10T12:00:00Z"


def test_rfc3339_rejects_naive_datetime():
    from datetime import datetime

    with pytest.raises(SerializationError, match="naive datetime"):
        to_rfc3339(datetime(2026, 8, 10, 12, 0, 0))


@pytest.mark.parametrize(
    "value",
    [
        {1, 2, 3},
        object(),
        lambda x: x,
        b"raw bytes",
        complex(1, 2),
    ],
)
def test_unsupported_types_are_rejected(value):
    with pytest.raises(SerializationError, match="not JSON-serializable"):
        to_json_value(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(value):
    with pytest.raises(SerializationError, match="not representable in JSON"):
        to_json_value(value)


def test_enum_serializes_to_its_wire_value():
    from erp_pipeline.schemas import SourceType

    assert to_json_value(SourceType.SQL_SERVER) == "sql_server"
    # The member is also equal to its wire value, being a str subclass.
    assert SourceType.SQL_SERVER == "sql_server"


def test_nested_structures_serialize_recursively():
    from erp_pipeline.schemas import SourceType

    payload = to_json_value(
        {"types": [SourceType.MYSQL, SourceType.MONGODB], "nested": {"n": 1}}
    )
    assert payload == {"types": ["mysql", "mongodb"], "nested": {"n": 1}}


# ============================================================
# Model versioning
# ============================================================

def test_version_constants_exist_and_are_semver_shaped():
    for version in (
        CANONICAL_MODEL_VERSION,
        SOURCE_MODEL_VERSION,
        MAPPING_MODEL_VERSION,
        RUN_MODEL_VERSION,
    ):
        parts = version.split(".")
        assert len(parts) == 3
        assert all(part.isdigit() for part in parts)


def test_version_literals_are_not_scattered_across_model_modules():
    """Every model must read its version from erp_pipeline.version."""
    import pathlib

    package_root = pathlib.Path(phase1.__file__).resolve().parents[1]
    offenders = []

    for module_path in package_root.rglob("*.py"):
        if module_path.name == "version.py":
            continue
        text = module_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            # A model-version assignment to a literal outside version.py is
            # exactly the duplication this check exists to prevent.
            if stripped.startswith("CANONICAL_MODEL_VERSION =") or stripped.startswith(
                "SOURCE_MODEL_VERSION ="
            ):
                offenders.append(f"{module_path.name}: {stripped}")

    assert offenders == []
