"""Identity rules, serialization guarantees, and normalization stability.

HISTORY
-------
These tests once imported ``bpi2020.common.stable_ids`` alongside
``erp_pipeline.schemas.identity`` to prove the framework and the dataset
prototype agreed on identity. The prototype has been consolidated away, so
there is no second implementation left to compare against.

Comparing the algorithm against a FROZEN CORPUS instead is strictly stronger.
An agreement test only proved the two implementations were the same as each
other - they could have drifted together. The corpus below pins the exact
output byte-for-byte, so any change to ``normalize_identifier`` fails loudly,
which matters because changing it silently re-identifies every stored record
and orphans every derived vector.

The boundary tests further down are kept deliberately: they still guard
against a dataset package being reintroduced into the framework.
"""

import uuid

import pytest

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
# Normalization stability (frozen corpus)
# ============================================================

#: The exact output ``normalize_identifier`` must produce, pinned literally.
#: Changing this function re-identifies every stored record and orphans every
#: derived vector, so it is a MAJOR contract change and must never happen by
#: accident. These expectations were carried over unchanged from the identity
#: contract the dataset prototype established.
FROZEN_NORMALIZATION = [
    ("declaration 100000", "declaration_100000"),
    ("travel permit 76455", "travel_permit_76455"),
    ("Request For Payment 73550", "request_for_payment_73550"),
    ("INV-001", "inv-001"),
    ("_id", "id"),
    ("  padded  value  ", "padded_value"),
    ("weird/chars*here", "weird_chars_here"),
    ("Fin.Invoice", "fin.invoice"),
    ("UPPER_CASE_NAME", "upper_case_name"),
    ("multiple___underscores", "multiple_underscores"),
    ("trailing___", "trailing"),
    ("___leading", "leading"),
    ("tab\tseparated", "tab_separated"),
    ("new\nline", "new_line"),
    ("café_münchen", "caf_m_nchen"),
    ("12345", "12345"),
    ("a:b:c", "a_b_c"),
    ("erp:sys:invoice:1", "erp_sys_invoice_1"),
]


@pytest.mark.parametrize("raw,expected", FROZEN_NORMALIZATION)
def test_normalization_output_is_frozen(raw, expected):
    """Pins the algorithm byte-for-byte against a literal expectation.

    Stronger than the agreement test this replaced: two implementations
    compared against each other could have drifted together, whereas a literal
    expectation cannot.
    """
    assert phase1.normalize_identifier(raw) == expected


@pytest.mark.parametrize("value", NORMALIZATION_CORPUS)
def test_normalization_never_emits_the_id_separator(value):
    """What makes ``parse_canonical_id`` unambiguous."""
    assert phase1.CANONICAL_ID_SEPARATOR not in phase1.normalize_identifier(value)


@pytest.mark.parametrize("value", [None, "", "   ", "___", "***"])
def test_unusable_values_are_refused_rather_than_silently_emptied(value):
    with pytest.raises(IdentityError):
        phase1.normalize_identifier(value)


def test_uuid_derivation_is_uuid5_over_the_url_namespace():
    """The derivation an external vector store depends on."""
    record_id = "erp:finance_erp_pg:invoice:inv-001"
    derived = phase1.make_deterministic_uuid(record_id)

    assert derived == str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, f"{phase1.UUID_NAMESPACE_PREFIX}/{record_id}"
        )
    )
    assert uuid.UUID(derived).version == 5


def test_uuid_derivation_is_namespaced():
    """Two namespaces must never map the same string to the same UUID, or two
    deployments sharing a vector store would collide."""
    record_id = "erp:finance_erp_pg:invoice:inv-001"

    assert phase1.make_deterministic_uuid(record_id) != (
        phase1.make_deterministic_uuid(record_id, namespace_prefix="other")
    )


def test_every_canonical_id_carries_the_canonical_prefix():
    identifiers = [
        phase1.make_canonical_record_id("finance_erp_pg", "invoice", "INV-001"),
        phase1.make_canonical_document_id("policy_library", "a102d03b"),
    ]

    for identifier in identifiers:
        assert identifier.startswith(f"{phase1.CANONICAL_ID_PREFIX}:")


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


def test_content_hash_is_a_sha256_hex_digest():
    digest = phase1.compute_content_hash("r1", {"b": 2, "a": 1}, "text")

    assert len(digest) == 64
    int(digest, 16)


def test_content_hash_is_key_order_independent():
    """Two records built in a different order are the same record."""
    assert phase1.compute_content_hash(
        "r1", {"b": 2, "a": 1}, "text"
    ) == phase1.compute_content_hash("r1", {"a": 1, "b": 2}, "text")


def test_content_hash_treats_absent_and_null_alike():
    """Otherwise adding an empty optional field would re-embed the record."""
    assert phase1.compute_content_hash(
        "r1", {"a": 1}, "text"
    ) == phase1.compute_content_hash("r1", {"a": 1, "c": None}, "text")


def test_content_hash_changes_when_the_ai_text_changes():
    assert phase1.compute_content_hash(
        "r1", {"a": 1}, "text"
    ) != phase1.compute_content_hash("r1", {"a": 1}, "different text")


def test_content_hash_changes_when_the_content_changes():
    assert phase1.compute_content_hash(
        "r1", {"a": 1}, "text"
    ) != phase1.compute_content_hash("r1", {"a": 2}, "text")


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
