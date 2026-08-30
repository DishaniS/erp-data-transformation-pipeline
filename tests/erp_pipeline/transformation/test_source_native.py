"""Phase 2 - source-native transformation for uncovered ERP entities.

The canonical model covers three entities. A real ERP holds dozens. These tests
pin the behaviour that lets the other dozens become AI-ready **without** giving
anyone a way around a mapping decision a human is supposed to make.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.schemas.enums import EntityKind, FieldDataType, SourceType
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import (
    BINARY_FIELDS_KEY,
    BUSINESS_KEY_NAME,
    BUSINESS_KEY_VALUE,
    SOURCE_NATIVE_KEY,
    SourceIdentityUnavailableError,
    SourceNativeTransformer,
    binary_field_names,
    resolve_business_key,
)


def field(name: str, data_type: FieldDataType, primary: bool = False) -> SourceField:
    return SourceField(
        source_name=name,
        normalized_name=name,
        source_data_type="VARCHAR",
        normalized_data_type=data_type,
        is_primary_key=primary,
        nullable=not primary,
    )


def entity(name: str, keys, fields) -> SourceEntity:
    return SourceEntity(
        entity_id=f"src.{name}",
        source_name=name,
        normalized_name=name,
        entity_kind=EntityKind.TABLE,
        primary_key_fields=tuple(keys),
        fields=tuple(fields),
    )


@pytest.fixture
def transformer() -> SourceNativeTransformer:
    return SourceNativeTransformer()


@pytest.fixture
def employees() -> SourceEntity:
    return entity(
        "employees",
        ("employee_id",),
        (
            field("employee_id", FieldDataType.STRING, primary=True),
            field("name", FieldDataType.STRING),
            field("department", FieldDataType.STRING),
            field("job_title", FieldDataType.STRING),
        ),
    )


@pytest.fixture
def employee_record() -> SourceRecord:
    return SourceRecord.from_mapping(
        {
            "employee_id": "EMP002",
            "name": "Nimal Silva",
            "department": "Finance",
            "job_title": "Accountant",
        },
        source_entity="employees",
    )


# ======================================================================
# TEST B - an entity the canonical model does not cover
# ======================================================================


def test_an_uncovered_entity_becomes_a_record(transformer, employees, employee_record):
    record = transformer.transform_record(
        employee_record, employees, "legacy_hr", SourceType.POSTGRESQL
    )

    assert record.record_id == "erp:legacy_hr:employees:emp002"
    assert record.entity_type == "employees"
    assert record.normalized_data["employee_id"] == "EMP002"
    assert record.normalized_data["name"] == "Nimal Silva"


def test_field_names_are_the_sources_own(transformer, employees, employee_record):
    """Nothing is renamed, because there is no canonical concept to rename to."""
    record = transformer.transform_record(
        employee_record, employees, "legacy_hr", SourceType.POSTGRESQL
    )

    assert set(record.normalized_data) == {
        "employee_id",
        "name",
        "department",
        "job_title",
    }


def test_an_uncovered_entity_is_never_labelled_as_a_canonical_one(
    transformer, employees, employee_record
):
    record = transformer.transform_record(
        employee_record, employees, "legacy_hr", SourceType.POSTGRESQL
    )

    assert record.entity_type not in {"invoice", "customer", "purchase_order"}


def test_the_record_is_marked_as_source_native(
    transformer, employees, employee_record
):
    """Nothing downstream should mistake this for curated-vocabulary data."""
    record = transformer.transform_record(
        employee_record, employees, "legacy_hr", SourceType.POSTGRESQL
    )

    assert record.metadata[SOURCE_NATIVE_KEY] is True


def test_it_becomes_an_ordinary_ai_representation(
    transformer, employees, employee_record
):
    """No parallel AI model. The existing builder handles it unchanged."""
    record = transformer.transform_record(
        employee_record, employees, "legacy_hr", SourceType.POSTGRESQL
    )
    representation = canonical_record_to_representation(record)

    assert representation.representation_id.startswith("ai:employees:")
    assert "Nimal Silva" in representation.text_for_ai
    assert "Employee Id: EMP002" in representation.text_for_ai
    assert representation.metadata["canonical_record_id"] == record.record_id


# ======================================================================
# TEST C - an arbitrary custom entity, proving nothing is employee-specific
# ======================================================================


def test_an_arbitrary_custom_entity_works_with_no_new_code(transformer):
    machines = entity(
        "machine_maintenance_records",
        ("machine_code", "service_date"),
        (
            field("machine_code", FieldDataType.STRING, primary=True),
            field("service_date", FieldDataType.DATE, primary=True),
            field("technician", FieldDataType.STRING),
            field("notes", FieldDataType.STRING),
        ),
    )
    record = transformer.transform_record(
        SourceRecord.from_mapping(
            {
                "machine_code": "MX-9",
                "service_date": "2026-03-11",
                "technician": "R. Silva",
                "notes": "belt replaced",
            }
        ),
        machines,
        "plant_erp",
        SourceType.MYSQL,
    )

    assert record.entity_type == "machine_maintenance_records"
    assert record.normalized_data["technician"] == "R. Silva"


@pytest.mark.parametrize(
    "name", ["assets", "warehouses", "product_master", "vendor_ledger", "custom_table_xyz"]
)
def test_any_discovered_entity_name_is_accepted(transformer, name):
    generic = entity(
        name, ("code",), (field("code", FieldDataType.STRING, primary=True),)
    )
    record = transformer.transform_record(
        SourceRecord.from_mapping({"code": "K-1"}), generic, "erp", SourceType.POSTGRESQL
    )

    assert record.entity_type == name


# ======================================================================
# TEST D - composite primary key
# ======================================================================


def test_a_composite_key_produces_one_stable_identity(transformer):
    stock = entity(
        "warehouse_stock",
        ("warehouse_id", "product_id"),
        (
            field("warehouse_id", FieldDataType.STRING, primary=True),
            field("product_id", FieldDataType.STRING, primary=True),
            field("quantity", FieldDataType.INTEGER),
        ),
    )
    values = {"warehouse_id": "WH-1", "product_id": "P-77", "quantity": "250"}

    first = transformer.transform_record(
        SourceRecord.from_mapping(values), stock, "wms", SourceType.POSTGRESQL
    )
    second = transformer.transform_record(
        SourceRecord.from_mapping(dict(values)), stock, "wms", SourceType.POSTGRESQL
    )

    assert first.record_id == second.record_id
    assert first.metadata[BUSINESS_KEY_NAME] == "warehouse_id|product_id"
    assert first.metadata[BUSINESS_KEY_VALUE] == "WH-1|P-77"
    # Declared order, not dictionary order - so the value cannot drift.
    assert resolve_business_key(stock, SourceRecord.from_mapping(values))[1] == "WH-1|P-77"


def test_declared_types_are_still_converted(transformer):
    stock = entity(
        "warehouse_stock",
        ("warehouse_id",),
        (
            field("warehouse_id", FieldDataType.STRING, primary=True),
            field("quantity", FieldDataType.INTEGER),
        ),
    )
    record = transformer.transform_record(
        SourceRecord.from_mapping({"warehouse_id": "WH-1", "quantity": "250"}),
        stock,
        "wms",
        SourceType.POSTGRESQL,
    )

    assert record.normalized_data["quantity"] == 250
    assert isinstance(record.normalized_data["quantity"], int)


# ======================================================================
# TEST E - no reliable identity is refused, never invented
# ======================================================================


def test_an_entity_with_no_key_is_refused(transformer):
    log = entity(
        "event_log",
        (),
        (field("message", FieldDataType.STRING), field("level", FieldDataType.STRING)),
    )

    with pytest.raises(SourceIdentityUnavailableError):
        transformer.transform_record(
            SourceRecord.from_mapping({"message": "x", "level": "INFO"}),
            log,
            "erp",
            SourceType.CSV,
        )


def test_a_row_number_is_refused_as_an_identity(transformer):
    """A CSV declares no key and its extractor supplies a row offset.

    Keying on it would make the same record change identity whenever the file is
    reordered, orphaning every vector already stored against the old id. This is
    the same refusal the canonical path applies to surrogate keys.
    """
    log = entity("event_log", (), (field("message", FieldDataType.STRING),))

    with pytest.raises(SourceIdentityUnavailableError) as caught:
        transformer.transform_record(
            SourceRecord.from_mapping({"message": "x"}, record_key="1"),
            log,
            "erp",
            SourceType.CSV,
        )

    assert "POSITION" in str(caught.value)


def test_a_genuine_business_record_key_is_accepted(transformer):
    log = entity("event_log", (), (field("message", FieldDataType.STRING),))
    record = transformer.transform_record(
        SourceRecord.from_mapping({"message": "x"}, record_key="EVT-9001"),
        log,
        "erp",
        SourceType.CSV,
    )

    assert record.record_id.endswith("evt-9001")


def test_a_batch_reports_unidentifiable_records_rather_than_dropping_them(
    transformer,
):
    log = entity("event_log", (), (field("message", FieldDataType.STRING),))
    result = transformer.transform_records(
        [SourceRecord.from_mapping({"message": "x"}, ordinal=1)],
        log,
        "erp",
        SourceType.CSV,
    )

    assert result.record_count == 0
    assert len(result.rejected) == 1
    assert "record 1" in result.rejected[0]


def test_an_explicit_key_field_outranks_everything_inferred(transformer):
    """A caller's decision is stronger evidence than anything the schema omits."""
    log = entity(
        "event_log",
        (),
        (field("event_code", FieldDataType.STRING), field("message", FieldDataType.STRING)),
    )
    record = transformer.transform_record(
        SourceRecord.from_mapping({"event_code": "EVT-1", "message": "x"}),
        log,
        "erp",
        SourceType.CSV,
        key_fields=["event_code"],
    )

    assert record.record_id.endswith("evt-1")
    assert record.metadata[BUSINESS_KEY_NAME] == "event_code"


# ======================================================================
# TEST F - binary is described, never rendered
# ======================================================================


@pytest.fixture
def employees_with_blob() -> SourceEntity:
    return entity(
        "employees",
        ("employee_id",),
        (
            field("employee_id", FieldDataType.STRING, primary=True),
            field("name", FieldDataType.STRING),
            field("birth_certificate", FieldDataType.BINARY),
        ),
    )


def test_binary_values_never_reach_the_ai_text(transformer, employees_with_blob):
    """Base64 in the embedded text would displace the fields that carry meaning."""
    import base64

    jpeg = b"\xff\xd8\xff" + b"\x00" * 400
    record = transformer.transform_record(
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "name": "Nimal Silva",
                "birth_certificate": jpeg,
            }
        ),
        employees_with_blob,
        "legacy_hr",
        SourceType.POSTGRESQL,
    )
    text = canonical_record_to_representation(record).text_for_ai

    assert base64.b64encode(jpeg).decode()[:24] not in text
    assert "birth_certificate" not in record.normalized_data
    # The fields that do carry meaning are still there.
    assert "EMP002" in text
    assert "Nimal Silva" in text


def test_the_binary_field_is_recorded_for_phase_3(transformer, employees_with_blob):
    """Excluded from the text, but not forgotten - Phase 3 needs to find it."""
    record = transformer.transform_record(
        SourceRecord.from_mapping(
            {"employee_id": "EMP002", "name": "N", "birth_certificate": b"\xff\xd8\xff"}
        ),
        employees_with_blob,
        "legacy_hr",
        SourceType.POSTGRESQL,
    )

    assert record.metadata[BINARY_FIELDS_KEY] == ["birth_certificate"]


def test_binary_fields_are_read_from_the_schema_not_sniffed(employees_with_blob):
    assert binary_field_names(employees_with_blob) == ("birth_certificate",)


def test_a_batch_reports_which_binary_fields_it_left_unopened(
    transformer, employees_with_blob
):
    result = transformer.transform_records(
        [
            SourceRecord.from_mapping(
                {"employee_id": "EMP002", "name": "N", "birth_certificate": b"\xff\xd8"}
            )
        ],
        employees_with_blob,
        "legacy_hr",
        SourceType.POSTGRESQL,
    )

    assert result.binary_fields_omitted == ("birth_certificate",)


# ======================================================================
# TEST H - repeated processing is stable
# ======================================================================


def test_processing_the_same_record_twice_is_stable(
    transformer, employees, employee_record
):
    first = transformer.transform_record(
        employee_record, employees, "legacy_hr", SourceType.POSTGRESQL
    )
    second = transformer.transform_record(
        employee_record, employees, "legacy_hr", SourceType.POSTGRESQL
    )

    assert first.record_id == second.record_id
    assert first.content_hash == second.content_hash
    assert (
        canonical_record_to_representation(first).representation_id
        == canonical_record_to_representation(second).representation_id
    )


def test_changed_content_changes_the_hash_but_not_the_identity(
    transformer, employees, employee_record
):
    """A promotion is the same employee with different data."""
    first = transformer.transform_record(
        employee_record, employees, "legacy_hr", SourceType.POSTGRESQL
    )
    promoted = transformer.transform_record(
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "name": "Nimal Silva",
                "department": "Finance",
                "job_title": "Senior Accountant",
            }
        ),
        employees,
        "legacy_hr",
        SourceType.POSTGRESQL,
    )

    assert first.record_id == promoted.record_id
    assert first.content_hash != promoted.content_hash


# ======================================================================
# Provenance
# ======================================================================


def test_provenance_is_preserved(transformer, employees, employee_record):
    record = transformer.transform_record(
        employee_record,
        employees,
        "legacy_hr",
        SourceType.POSTGRESQL,
        schema_id="legacy_hr.employees.v1",
    )

    assert record.source.source_system_id == "legacy_hr"
    assert record.source.source_entity == "employees"
    assert record.source.source_record_key == "EMP002"
    assert record.source.source_type is SourceType.POSTGRESQL
    assert record.provenance.schema_id == "legacy_hr.employees.v1"
    assert record.provenance.ingestion_method == "source_native_transformation"
    assert record.content_hash


def test_the_business_key_is_preserved_for_phase_4(
    transformer, employees, employee_record
):
    """Phase 4 will expose this as a retrieval filter. Phase 2 must not lose it."""
    record = transformer.transform_record(
        employee_record, employees, "legacy_hr", SourceType.POSTGRESQL
    )

    assert record.metadata[BUSINESS_KEY_NAME] == "employee_id"
    assert record.metadata[BUSINESS_KEY_VALUE] == "EMP002"


def test_nulls_are_preserved_rather_than_invented(transformer, employees):
    record = transformer.transform_record(
        SourceRecord.from_mapping(
            {
                "employee_id": "EMP002",
                "name": "N",
                "department": None,
                "job_title": "Accountant",
            }
        ),
        employees,
        "legacy_hr",
        SourceType.POSTGRESQL,
    )

    assert record.normalized_data["department"] is None
