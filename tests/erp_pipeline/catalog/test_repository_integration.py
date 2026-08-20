"""Live PostgreSQL integration tests for the schema catalog.

Skipped (not failed) when the catalog database is unavailable - see
``catalog_engine`` in conftest.py. Every test uses a source_system_id/
schema_id/mapping_id under this module's own test prefix and is cleaned up
via the ``cleanup`` fixture, so nothing here pollutes shared research data.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from erp_pipeline.catalog.exceptions import (
    MappingProfileNotFoundError,
    SchemaIdentityConflictError,
    SchemaSnapshotNotFoundError,
    SourceSystemIdentityConflictError,
    SourceSystemNotFoundError,
)
from erp_pipeline.catalog.versioning import compare_schemas
from erp_pipeline.schemas import (
    EntityKind,
    FieldDataType,
    FieldMapping,
    MappingProfile,
    MappingStatus,
    RelationshipType,
    SchemaOrigin,
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
    SourceSystem,
    SourceType,
    TransformationOperation,
    TransformationRule,
)

from tests.erp_pipeline.catalog.conftest import TEST_PREFIX
from tests.erp_pipeline.catalog.fixtures import (
    build_finance_erp_schema,
    build_finance_erp_schema_v2,
    build_finance_erp_source_system,
)


def _sys_id(unique_id: str) -> str:
    return f"{TEST_PREFIX}_{unique_id}"


def _small_schema(schema_id: str, source_system_id: str, hash_seed: str = "a") -> SourceSchema:
    # compute_schema_hash() deliberately excludes metadata (Phase 1 design:
    # metadata is documentation, not structure), so the seed must vary a
    # structural attribute - here, the vendor precision on total_amount - to
    # actually change the resulting hash. Two calls with the same hash_seed
    # are structurally identical; different hash_seed values are not.
    # sum of ordinals gives a distinct-enough scale per distinct seed string
    # without relying on Python's randomized str hash() across runs.
    precision = f"NUMERIC(12,{sum(ord(c) for c in hash_seed)})"

    return SourceSchema(
        schema_id=schema_id,
        source_system_id=source_system_id,
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(
            SourceEntity(
                entity_id="fin_invoice",
                source_name="fin_invoice",
                normalized_name="fin_invoice",
                entity_kind=EntityKind.TABLE,
                primary_key_fields=("invoice_no",),
                fields=(
                    SourceField(
                        source_name="invoice_no",
                        normalized_name="invoice_no",
                        source_data_type="VARCHAR(20)",
                        normalized_data_type=FieldDataType.STRING,
                        is_primary_key=True,
                        nullable=False,
                    ),
                    SourceField(
                        source_name="total_amount",
                        normalized_name="total_amount",
                        source_data_type=precision,
                        normalized_data_type=FieldDataType.DECIMAL,
                    ),
                ),
            ),
        ),
        relationships=(),
    )


# ============================================================
# 1. Catalog bootstrap idempotency
# ============================================================

def test_bootstrap_is_idempotent(catalog_engine):
    from erp_pipeline.catalog.schema import bootstrap_catalog

    first = bootstrap_catalog(catalog_engine)
    second = bootstrap_catalog(catalog_engine)

    assert first.is_complete
    assert second.is_complete
    assert first.tables_present == second.tables_present


# ============================================================
# 2 & 3. SourceSystem insert/retrieve and identical re-save
# ============================================================

def test_source_system_insert_and_retrieve(repository, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    system = SourceSystem(
        source_system_id=system_id,
        name="Test Finance ERP",
        source_type=SourceType.POSTGRESQL,
        environment="research",
        metadata={"region": "apac"},
    )

    repository.save_source_system(system)
    retrieved = repository.get_source_system(system_id)

    assert retrieved.source_system_id == system_id
    assert retrieved.name == "Test Finance ERP"
    assert retrieved.source_type is SourceType.POSTGRESQL
    assert retrieved.metadata["region"] == "apac"


def test_source_system_identical_resave_is_idempotent(repository, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    system = SourceSystem(
        source_system_id=system_id, name="X", source_type=SourceType.MYSQL
    )

    repository.save_source_system(system)
    repository.save_source_system(system)

    systems = repository.list_source_systems()
    matching = [s for s in systems if s.source_system_id == system_id]
    assert len(matching) == 1


def test_source_system_descriptive_update_is_applied(repository, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    original = SourceSystem(
        source_system_id=system_id, name="Original Name", source_type=SourceType.MONGODB
    )
    repository.save_source_system(original)

    updated = SourceSystem(
        source_system_id=system_id,
        name="Renamed",
        source_type=SourceType.MONGODB,
        description="now with a description",
    )
    repository.save_source_system(updated)

    retrieved = repository.get_source_system(system_id)
    assert retrieved.name == "Renamed"
    assert retrieved.description == "now with a description"
    assert retrieved.created_at == original.created_at  # unchanged on update


# ============================================================
# 4. Source-type identity conflict
# ============================================================

def test_source_system_source_type_change_is_rejected(repository, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )

    with pytest.raises(SourceSystemIdentityConflictError):
        repository.save_source_system(
            SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.MONGODB)
        )

    # The original registration must be untouched.
    assert repository.get_source_system(system_id).source_type is SourceType.POSTGRESQL


def test_saving_schema_for_unregistered_system_fails(repository, unique_id, cleanup):
    system_id = _sys_id(unique_id)  # deliberately not registered
    schema_id = cleanup.schema_id(f"{system_id}_schema")

    with pytest.raises(SourceSystemNotFoundError):
        repository.save_schema_snapshot(_small_schema(schema_id, system_id))


# ============================================================
# 5 & 6. Schema snapshot save/retrieve, full reconstruction
# ============================================================

def test_schema_snapshot_save_and_full_reconstruction(repository, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id = cleanup.schema_id(f"{system_id}_schema")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )
    original = _small_schema(schema_id, system_id)
    repository.save_schema_snapshot(original)

    retrieved = repository.get_schema_snapshot(schema_id)

    original_payload = original.to_json_dict()
    retrieved_payload = retrieved.to_json_dict()

    # schema_hash is server-recomputed (Task 7), so it legitimately differs
    # from the caller's unset None; every other field must match exactly.
    original_payload.pop("schema_hash")
    retrieved_payload.pop("schema_hash")
    assert original_payload == retrieved_payload
    assert retrieved.schema_hash == original.compute_schema_hash()

    assert json.loads(json.dumps(retrieved_payload)) == retrieved_payload


def test_get_nonexistent_schema_snapshot_raises(repository):
    with pytest.raises(SchemaSnapshotNotFoundError):
        repository.get_schema_snapshot("__does_not_exist__")


# ============================================================
# 7 & 8. Idempotent identical save / version increments on change
# ============================================================

def test_identical_schema_does_not_create_new_catalog_version(
    repository, unique_id, cleanup
):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id = cleanup.schema_id(f"{system_id}_schema")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )
    schema = _small_schema(schema_id, system_id)

    first = repository.save_schema_snapshot(schema)
    second = repository.save_schema_snapshot(schema)

    assert first.created is True
    assert first.record.catalog_version == 1
    assert second.created is False
    assert second.record.catalog_version == 1
    assert second.record.schema_id == first.record.schema_id


def test_changed_schema_creates_catalog_version_plus_one(repository, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id_v1 = cleanup.schema_id(f"{system_id}_schema_v1")
    schema_id_v2 = cleanup.schema_id(f"{system_id}_schema_v2")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )

    v1 = _small_schema(schema_id_v1, system_id, hash_seed="a")
    v2 = _small_schema(schema_id_v2, system_id, hash_seed="b")  # different content

    result_v1 = repository.save_schema_snapshot(v1)
    result_v2 = repository.save_schema_snapshot(v2)

    assert result_v1.record.catalog_version == 1
    assert result_v2.created is True
    assert result_v2.record.catalog_version == 2
    assert result_v1.record.schema_hash != result_v2.record.schema_hash


def test_content_identical_schema_under_new_schema_id_deduplicates(
    repository, unique_id, cleanup
):
    """Same structural content, fresh schema_id: no new version (Task 6)."""
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id_a = cleanup.schema_id(f"{system_id}_schema_a")
    schema_id_b = cleanup.schema_id(f"{system_id}_schema_b")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )

    schema_a = _small_schema(schema_id_a, system_id, hash_seed="same")
    schema_b = SourceSchema(
        schema_id=schema_id_b,  # different id
        source_system_id=system_id,
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=schema_a.entities,  # identical structural content
    )

    result_a = repository.save_schema_snapshot(schema_a)
    result_b = repository.save_schema_snapshot(schema_b)

    assert result_a.created is True
    assert result_b.created is False
    assert result_b.record.schema_id == schema_id_a  # deduplicated onto the existing one
    assert result_b.record.catalog_version == 1


def test_semantic_type_only_change_creates_a_new_catalog_version(
    repository, unique_id, cleanup
):
    """Regression test (Phase 0-2 audit fix).

    Before the fix, compute_schema_hash() ignored semantic_type, so a schema
    revision whose only change was a field's semantic_type hashed identically
    to the original and was silently deduplicated by save_schema_snapshot()
    - no new catalog_version, the change never persisted. This proves the
    live save path now creates version 2 for exactly that change, and that
    re-saving the identical V2 afterwards stays idempotent at version 2.
    """
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id_v1 = cleanup.schema_id(f"{system_id}_v1")
    schema_id_v2 = cleanup.schema_id(f"{system_id}_v2")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )

    def build(semantic_type, schema_id):
        return SourceSchema(
            schema_id=schema_id,
            source_system_id=system_id,
            schema_name="public",
            origin=SchemaOrigin.DISCOVERED,
            entities=(
                SourceEntity(
                    entity_id="customer",
                    source_name="customer",
                    normalized_name="customer",
                    fields=(
                        SourceField(
                            source_name="email",
                            normalized_name="email",
                            normalized_data_type=FieldDataType.STRING,
                            semantic_type=semantic_type,
                        ),
                    ),
                ),
            ),
        )

    v1 = build(None, schema_id_v1)
    v2 = build("email_address", schema_id_v2)  # only semantic_type differs

    result_v1 = repository.save_schema_snapshot(v1)
    result_v2 = repository.save_schema_snapshot(v2)

    assert result_v1.record.catalog_version == 1
    assert result_v2.created is True, (
        "a semantic_type-only change was silently deduplicated instead of "
        "creating a new catalog version"
    )
    assert result_v2.record.catalog_version == 2
    assert result_v1.record.schema_hash != result_v2.record.schema_hash

    # Re-saving the identical V2 must remain idempotent: no version 3.
    result_v2_again = repository.save_schema_snapshot(v2)
    assert result_v2_again.created is False
    assert result_v2_again.record.catalog_version == 2


# ============================================================
# 9. Immutable schema ID conflict rejected
# ============================================================

def test_reusing_schema_id_with_different_content_is_rejected(
    repository, unique_id, cleanup
):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id = cleanup.schema_id(f"{system_id}_schema")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )
    repository.save_schema_snapshot(_small_schema(schema_id, system_id, hash_seed="a"))

    with pytest.raises(SchemaIdentityConflictError):
        repository.save_schema_snapshot(
            _small_schema(schema_id, system_id, hash_seed="different")
        )


# ============================================================
# 10 & 11. Latest schema retrieval, ordered snapshot listing
# ============================================================

def test_latest_schema_retrieval_and_ordered_history(repository, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id_v1 = cleanup.schema_id(f"{system_id}_v1")
    schema_id_v2 = cleanup.schema_id(f"{system_id}_v2")
    schema_id_v3 = cleanup.schema_id(f"{system_id}_v3")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )

    repository.save_schema_snapshot(_small_schema(schema_id_v1, system_id, "a"))
    repository.save_schema_snapshot(_small_schema(schema_id_v2, system_id, "b"))
    repository.save_schema_snapshot(_small_schema(schema_id_v3, system_id, "c"))

    latest = repository.get_latest_schema(system_id, "public")
    assert latest.schema_id == schema_id_v3

    history = repository.list_schema_snapshots(system_id, "public")
    assert [record.schema_id for record in history] == [
        schema_id_v1,
        schema_id_v2,
        schema_id_v3,
    ]
    assert [record.catalog_version for record in history] == [1, 2, 3]


# ============================================================
# 12. Atomic rollback on failed snapshot
# ============================================================

def test_snapshot_save_rolls_back_completely_on_failure(repository, unique_id, cleanup):
    """A save that fails partway must leave NO trace (Task 13)."""
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id = cleanup.schema_id(f"{system_id}_schema")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )

    # Two entities that share the same entity_id: the second insert violates
    # the (schema_id, entity_id) primary key partway through the transaction.
    broken_schema = SourceSchema(
        schema_id=schema_id,
        source_system_id=system_id,
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(
            SourceEntity(entity_id="dup", source_name="a", normalized_name="entity_a"),
            SourceEntity(entity_id="dup", source_name="b", normalized_name="entity_b"),
        ),
    )

    with pytest.raises(Exception):
        repository.save_schema_snapshot(broken_schema)

    with pytest.raises(SchemaSnapshotNotFoundError):
        repository.get_schema_snapshot(schema_id)

    with repository._engine.connect() as connection:  # noqa: SLF001 - test-only introspection
        leftover_entities = connection.execute(
            text(
                "SELECT COUNT(*) FROM erp_catalog.source_entities WHERE schema_id = :id"
            ),
            {"id": schema_id},
        ).scalar()
    assert leftover_entities == 0, "a failed snapshot save left partial entity rows"


# ============================================================
# 13. FK integrity
# ============================================================

def test_mapping_profile_referencing_missing_schema_is_rejected(
    repository, unique_id, cleanup
):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    mapping_id = cleanup.mapping_id(f"{system_id}_mapping")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )

    profile = MappingProfile(
        mapping_id=mapping_id,
        source_system_id=system_id,
        source_entity="fin_invoice",
        target_entity_type="invoice",
        source_schema_id="does_not_exist",
    )

    with pytest.raises(SchemaSnapshotNotFoundError):
        repository.save_mapping_profile(profile)


# ============================================================
# 14 & 15. Mapping profile save/retrieve, TransformationRule preserved
# ============================================================

def test_mapping_profile_save_and_retrieve_with_transformation_rules(
    repository, unique_id, cleanup
):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id = cleanup.schema_id(f"{system_id}_schema")
    mapping_id = cleanup.mapping_id(f"{system_id}_mapping")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )
    repository.save_schema_snapshot(_small_schema(schema_id, system_id))

    profile = MappingProfile(
        mapping_id=mapping_id,
        source_system_id=system_id,
        source_schema_id=schema_id,
        source_entity="fin_invoice",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(
                source_field="invoice_no",
                target_field="invoice_id",
                status=MappingStatus.APPROVED,
            ),
            FieldMapping(
                source_field="total_amount",
                target_field="amount",
                transformations=(
                    TransformationRule(
                        operation=TransformationOperation.CAST,
                        config={"to": "decimal", "scale": 2},
                    ),
                    TransformationRule(
                        operation=TransformationOperation.DEFAULT,
                        config={"value": "0.00"},
                    ),
                ),
                confidence=0.92,
            ),
        ),
        status=MappingStatus.APPROVED,
        approved_by="IT22267290",
    )

    saved = repository.save_mapping_profile(profile)
    retrieved = repository.get_mapping_profile(mapping_id)

    assert saved.to_json_dict() == retrieved.to_json_dict()
    assert len(retrieved.field_mappings) == 2

    cast_mapping = next(
        fm for fm in retrieved.field_mappings if fm.target_field == "amount"
    )
    assert len(cast_mapping.transformations) == 2
    assert cast_mapping.transformations[0].operation.value == "cast"
    assert cast_mapping.transformations[0].config == {"to": "decimal", "scale": 2}
    assert cast_mapping.transformations[1].config == {"value": "0.00"}


def test_mapping_profile_resave_replaces_field_mappings(repository, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    mapping_id = cleanup.mapping_id(f"{system_id}_mapping")

    repository.save_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )

    profile_v1 = MappingProfile(
        mapping_id=mapping_id,
        source_system_id=system_id,
        source_entity="fin_invoice",
        target_entity_type="invoice",
        field_mappings=(FieldMapping(source_field="a", target_field="b"),),
    )
    repository.save_mapping_profile(profile_v1)

    profile_v2 = MappingProfile(
        mapping_id=mapping_id,
        source_system_id=system_id,
        source_entity="fin_invoice",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(source_field="x", target_field="y"),
            FieldMapping(source_field="p", target_field="q"),
        ),
    )
    repository.save_mapping_profile(profile_v2)

    retrieved = repository.get_mapping_profile(mapping_id)
    assert len(retrieved.field_mappings) == 2
    assert {fm.source_field for fm in retrieved.field_mappings} == {"x", "p"}


def test_get_nonexistent_mapping_profile_raises(repository):
    with pytest.raises(MappingProfileNotFoundError):
        repository.get_mapping_profile("__does_not_exist__")


# ============================================================
# 16. Schema comparison against real persisted V1/V2
# ============================================================

def test_schema_comparison_against_persisted_snapshots(service, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id_v1 = cleanup.schema_id(f"{system_id}_v1")
    schema_id_v2 = cleanup.schema_id(f"{system_id}_v2")

    service.register_source_system(
        SourceSystem(source_system_id=system_id, name="X", source_type=SourceType.POSTGRESQL)
    )

    v1 = _small_schema(schema_id_v1, system_id, "a")
    v2 = SourceSchema(
        schema_id=schema_id_v2,
        source_system_id=system_id,
        schema_name="public",
        origin=SchemaOrigin.DISCOVERED,
        entities=(
            SourceEntity(
                entity_id="fin_invoice",
                source_name="fin_invoice",
                normalized_name="fin_invoice",
                primary_key_fields=("invoice_no",),
                fields=(
                    SourceField(
                        source_name="invoice_no",
                        normalized_name="invoice_no",
                        is_primary_key=True,
                        nullable=False,
                    ),
                    # total_amount removed, new field added
                    SourceField(
                        source_name="currency_code",
                        normalized_name="currency_code",
                        normalized_data_type=FieldDataType.STRING,
                    ),
                ),
            ),
        ),
    )

    service.publish_schema(v1)
    service.publish_schema(v2)

    diff = service.compare_versions(schema_id_v1, schema_id_v2)

    assert ("fin_invoice", "total_amount") in diff.removed_fields
    assert ("fin_invoice", "currency_code") in diff.added_fields


# ============================================================
# 17. No secrets written into catalog metadata
# ============================================================

def test_no_secrets_can_reach_catalog_metadata(repository, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))

    with pytest.raises(Exception):
        # The Phase 1 model itself refuses this before it ever reaches the DB.
        SourceSystem(
            source_system_id=system_id,
            name="X",
            source_type=SourceType.POSTGRESQL,
            metadata={"db_password": "hunter2"},
        )


def test_persisted_source_system_serializes_without_secrets(
    repository, unique_id, cleanup
):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    repository.save_source_system(
        SourceSystem(
            source_system_id=system_id,
            name="X",
            source_type=SourceType.POSTGRESQL,
            metadata={"region": "apac"},
        )
    )
    retrieved = repository.get_source_system(system_id)
    serialized = json.dumps(retrieved.to_json_dict()).lower()

    for marker in ("password", "secret", "api_key", "connection_string"):
        assert marker not in serialized


# ============================================================
# 18. Repository never requires the BPI package
# ============================================================

def test_catalog_package_does_not_import_bpi2020():
    import ast
    import pathlib

    package_root = pathlib.Path(__file__).resolve().parents[3] / "src" / "erp_pipeline" / "catalog"
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

    assert offenders == [], f"erp_pipeline.catalog imports bpi2020: {offenders}"


# ============================================================
# Synthetic FinanceERP end-to-end: the Task 23/34 demonstration, as tests
# ============================================================

def test_finance_erp_synthetic_schema_persists_and_reconstructs_exactly(
    service, unique_id, cleanup
):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id = cleanup.schema_id(f"{system_id}_public_v1")

    system = build_finance_erp_source_system(source_system_id=system_id)
    service.register_source_system(system)

    schema = build_finance_erp_schema(schema_id=schema_id, source_system_id=system_id)
    result = service.publish_schema(schema)

    assert result.created is True
    assert result.record.catalog_version == 1

    retrieved = service.get_snapshot(schema_id)
    assert len(retrieved.entities) == 25
    assert sum(len(e.fields) for e in retrieved.entities) == 184
    assert len(retrieved.relationships) == 31

    original_payload = schema.to_json_dict()
    retrieved_payload = retrieved.to_json_dict()
    original_payload.pop("schema_hash")
    retrieved_payload.pop("schema_hash")
    assert original_payload == retrieved_payload

    summary = service.summarize(schema_id)
    assert summary.entity_count == 25
    assert summary.field_count == 184
    assert summary.relationship_count == 31


def test_finance_erp_identical_resave_stays_at_version_one(service, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id = cleanup.schema_id(f"{system_id}_public_v1")

    service.register_source_system(
        build_finance_erp_source_system(source_system_id=system_id)
    )
    schema = build_finance_erp_schema(schema_id=schema_id, source_system_id=system_id)

    first = service.publish_schema(schema)
    second = service.publish_schema(schema)

    assert first.record.catalog_version == 1
    assert second.created is False
    assert second.record.catalog_version == 1


def test_finance_erp_v2_creates_version_two_with_expected_diff(service, unique_id, cleanup):
    system_id = cleanup.source_system_id(_sys_id(unique_id))
    schema_id_v1 = cleanup.schema_id(f"{system_id}_public_v1")
    schema_id_v2 = cleanup.schema_id(f"{system_id}_public_v2")

    service.register_source_system(
        build_finance_erp_source_system(source_system_id=system_id)
    )

    v1 = build_finance_erp_schema(schema_id=schema_id_v1, source_system_id=system_id)
    v2 = build_finance_erp_schema_v2(schema_id=schema_id_v2, source_system_id=system_id)

    service.publish_schema(v1)
    result_v2 = service.publish_schema(v2)

    assert result_v2.created is True
    assert result_v2.record.catalog_version == 2

    diff = service.compare_versions(schema_id_v1, schema_id_v2)
    assert diff.added_fields == (("customer", "loyalty_tier"),)
    assert diff.removed_fields == (("vendor", "version"),)
    assert diff.added_relationships == (("budget", "employee"),)
