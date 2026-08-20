"""Coverage, validation, schema evolution and Phase 2 persistence.

Steps 24-27, 30, 34, 36. The persistence tests use the real PostgreSQL
catalog and skip - never fake - when it is unreachable.
"""

from __future__ import annotations

import pytest

from erp_pipeline.mapping import (
    DEFAULT_CANONICAL_MODEL,
    CanonicalTargetModel,
    FieldOutcome,
    FindingSeverity,
    MappingService,
    MappingValidationError,
    generate_mapping,
    validate_profile,
)
from erp_pipeline.schemas.enums import (
    EntityKind,
    FieldDataType,
    MappingStatus,
    SchemaOrigin,
    SourceType,
    TransformationOperation,
)
from erp_pipeline.schemas.mapping_models import (
    FieldMapping,
    MappingProfile,
    TransformationRule,
)

from tests.erp_pipeline.mapping.conftest import make_entity, make_field, make_schema

T = FieldDataType


def schema_with(*fields, entity_name: str = "fin_invoice"):
    return make_schema(
        "cov_probe", SourceType.POSTGRESQL, SchemaOrigin.DISCOVERED,
        entities=(make_entity(entity_name, tuple(fields)),),
    )


# ============================================================
# Source coverage (Step 25)
# ============================================================

def test_source_coverage_counts_every_outcome():
    result = generate_mapping(
        schema_with(
            make_field("invoice_no", T.STRING),
            make_field("customer_id", T.STRING),
            make_field("total_amount", T.DECIMAL),
            make_field("legacy_internal_flag_74", T.INTEGER),
            make_field("payload", T.OBJECT),
        )
    )
    coverage = result.coverage

    assert coverage.total_fields == 5
    assert coverage.mapped_fields == 3
    # Neither `legacy_internal_flag_74` nor `payload` matches any canonical
    # name, so both are unmapped rather than offered for review. A type
    # conflict only reaches REVIEW_REQUIRED when the NAME matched something -
    # see test_engine_selection.py, where `customer_id` as an OBJECT does.
    assert coverage.unmapped_fields == 2
    assert coverage.review_required_fields == 0
    assert coverage.coverage_ratio == 0.6


def test_coverage_rates_are_reported():
    result = generate_mapping(
        schema_with(
            make_field("invoice_no", T.STRING),
            make_field("zzz_nothing", T.STRING),
        )
    )

    assert result.coverage.unmapped_rate == 0.5
    assert result.coverage.ambiguity_rate == 0.0


# ============================================================
# Per-entity coverage (Step 26)
# ============================================================

def test_coverage_is_reported_per_entity(postgres_schema):
    coverage = generate_mapping(postgres_schema).coverage

    by_entity = {item.source_entity: item for item in coverage.entities}

    assert set(by_entity) == {"fin_invoice", "fin_customer"}
    assert by_entity["fin_invoice"].target_entity_type == "invoice"
    assert by_entity["fin_customer"].target_entity_type == "customer"
    assert by_entity["fin_invoice"].total_fields == 7
    assert by_entity["fin_customer"].total_fields == 4


def test_per_entity_coverage_ratios_are_independent(postgres_schema):
    coverage = generate_mapping(postgres_schema).coverage
    by_entity = {item.source_entity: item for item in coverage.entities}

    # The invoice table has an unmappable legacy column; the customer table
    # maps cleanly.
    assert by_entity["fin_invoice"].coverage_ratio < 1.0
    assert by_entity["fin_customer"].coverage_ratio == 1.0


# ============================================================
# Required target coverage (Step 24)
# ============================================================

def test_missing_required_targets_are_reported():
    """canonical invoice requires invoice_id, customer_id and amount."""
    result = generate_mapping(
        schema_with(make_field("invoice_no", T.STRING))
    )
    entity_coverage = result.coverage.entities[0]

    assert set(entity_coverage.missing_required_targets) == {
        "customer_id", "amount",
    }
    assert entity_coverage.required_target_coverage_complete is False


def test_a_complete_mapping_reports_full_required_coverage():
    result = generate_mapping(
        schema_with(
            make_field("invoice_no", T.STRING),
            make_field("customer_id", T.STRING),
            make_field("total_amount", T.DECIMAL),
        )
    )

    assert result.coverage.all_required_targets_covered is True


def test_a_profile_missing_a_required_target_is_not_transformation_ready():
    """Step 24: high source coverage does not make a profile usable."""
    result = generate_mapping(
        schema_with(
            make_field("invoice_no", T.STRING),
            make_field("currency_code", T.STRING),
        )
    )

    assert result.coverage.coverage_ratio == 1.0        # every source field mapped
    assert result.coverage.all_required_targets_covered is False
    assert not result.validation.is_valid                # and yet: not ready


# ============================================================
# Validation (Step 30)
# ============================================================

def test_validation_reports_a_missing_required_target():
    result = generate_mapping(schema_with(make_field("invoice_no", T.STRING)))

    codes = {finding.code for finding in result.validation.errors}
    assert "missing_required_target" in codes


def test_validation_warns_about_a_lossy_conversion():
    result = generate_mapping(
        schema_with(
            make_field("invoice_no", T.STRING),
            make_field("customer_id", T.INTEGER),   # -> STRING target
            make_field("total_amount", T.DECIMAL),
        )
    )

    codes = {finding.code for finding in result.validation.warnings}
    assert "lossy_type_conversion" in codes


def test_validation_catches_an_unknown_source_field():
    schema = schema_with(make_field("invoice_no", T.STRING))
    profile = MappingProfile(
        mapping_id="probe.manual.invoice.aaaaaaaaaaaa",
        source_system_id="cov_probe",
        source_entity="fin_invoice",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(source_field="ghost_column", target_field="invoice_id"),
        ),
    )

    report = validate_profile(profile, schema, DEFAULT_CANONICAL_MODEL)

    assert "unknown_source_field" in {finding.code for finding in report.errors}


def test_validation_catches_an_unknown_target_field():
    schema = schema_with(make_field("invoice_no", T.STRING))
    profile = MappingProfile(
        mapping_id="probe.manual.invoice.bbbbbbbbbbbb",
        source_system_id="cov_probe",
        source_entity="fin_invoice",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(source_field="invoice_no", target_field="not_a_field"),
        ),
    )

    report = validate_profile(profile, schema, DEFAULT_CANONICAL_MODEL)

    assert "unknown_target_field" in {finding.code for finding in report.errors}


def test_validation_catches_an_unknown_target_entity():
    schema = schema_with(make_field("invoice_no", T.STRING))
    profile = MappingProfile(
        mapping_id="probe.manual.ghost.cccccccccccc",
        source_system_id="cov_probe",
        source_entity="fin_invoice",
        target_entity_type="ghost_entity",
        field_mappings=(
            FieldMapping(source_field="invoice_no", target_field="invoice_id"),
        ),
    )

    report = validate_profile(profile, schema, DEFAULT_CANONICAL_MODEL)

    assert "unknown_target_entity" in {finding.code for finding in report.errors}


def test_validation_catches_an_impossible_type():
    schema = schema_with(make_field("payload", T.OBJECT))
    profile = MappingProfile(
        mapping_id="probe.manual.invoice.dddddddddddd",
        source_system_id="cov_probe",
        source_entity="fin_invoice",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(source_field="payload", target_field="amount"),
        ),
    )

    report = validate_profile(profile, schema, DEFAULT_CANONICAL_MODEL)

    assert "incompatible_type" in {finding.code for finding in report.errors}


def test_validation_warns_about_a_target_collision():
    schema = schema_with(
        make_field("cust_no", T.STRING),
        make_field("customer_number", T.STRING),
    )
    profile = MappingProfile(
        mapping_id="probe.manual.invoice.eeeeeeeeeeee",
        source_system_id="cov_probe",
        source_entity="fin_invoice",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(source_field="cust_no", target_field="customer_id"),
            FieldMapping(source_field="customer_number",
                         target_field="customer_id"),
        ),
    )

    report = validate_profile(profile, schema, DEFAULT_CANONICAL_MODEL)

    assert "target_collision" in {finding.code for finding in report.warnings}


def test_validation_warns_when_one_source_feeds_several_targets():
    schema = schema_with(make_field("reference", T.STRING))
    profile = MappingProfile(
        mapping_id="probe.manual.invoice.ffffffffffff",
        source_system_id="cov_probe",
        source_entity="fin_invoice",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(source_field="reference", target_field="invoice_id"),
            FieldMapping(source_field="reference", target_field="customer_id"),
        ),
    )

    report = validate_profile(profile, schema, DEFAULT_CANONICAL_MODEL)

    assert "source_field_mapped_multiple_times" in {
        finding.code for finding in report.warnings
    }


def test_a_declared_transformation_is_inspected_but_never_run():
    """Step 31: the rule's presence is validated structurally; nothing
    dispatches on it."""
    schema = schema_with(
        make_field("invoice_no", T.STRING),
        make_field("customer_id", T.STRING),
        make_field("issue_date", T.STRING),
    )
    profile = MappingProfile(
        mapping_id="probe.manual.invoice.111111111111",
        source_system_id="cov_probe",
        source_entity="fin_invoice",
        target_entity_type="invoice",
        field_mappings=(
            FieldMapping(source_field="invoice_no", target_field="invoice_id"),
            FieldMapping(source_field="customer_id", target_field="customer_id"),
            FieldMapping(
                source_field="issue_date", target_field="issued_on",
                transformations=(
                    TransformationRule(
                        operation=TransformationOperation.DATE_PARSE,
                        config={"format": "%d/%m/%Y"},
                    ),
                ),
            ),
        ),
    )

    report = validate_profile(profile, schema, DEFAULT_CANONICAL_MODEL)

    assert "unknown_transformation_operation" not in {
        finding.code for finding in report.findings
    }
    # The rule survived validation untouched and unexecuted.
    assert profile.field_mappings[2].transformations[0].config == {
        "format": "%d/%m/%Y"
    }


def test_strict_mode_raises_on_a_validation_error():
    service = MappingService()

    with pytest.raises(MappingValidationError) as excinfo:
        service.generate(
            schema_with(make_field("invoice_no", T.STRING)), strict=True
        )

    assert excinfo.value.findings


def test_validation_is_non_fatal_by_default():
    """A reviewer wants the whole list, not one error at a time."""
    result = generate_mapping(schema_with(make_field("invoice_no", T.STRING)))

    assert result.validation is not None
    assert not result.validation.is_valid
    assert result.profiles  # still produced


# ============================================================
# Source schema evolution (Step 36)
# ============================================================

def test_adding_a_field_preserves_the_existing_mappings():
    v1 = schema_with(
        make_field("customer_id", T.STRING),
        make_field("customer_name", T.STRING),
        entity_name="fin_customer",
    )
    v2 = schema_with(
        make_field("customer_id", T.STRING),
        make_field("customer_name", T.STRING),
        make_field("email", T.STRING),
        entity_name="fin_customer",
    )

    first = generate_mapping(v1)
    second = generate_mapping(v2)

    def targets(result):
        return {
            decision.source_field: decision.selected.qualified_target
            for decision in result.decisions
            if decision.selected
        }

    before = targets(first)
    after = targets(second)

    # Every prior mapping is unchanged...
    for source_field, target in before.items():
        assert after[source_field] == target

    # ...and the new field gained its own.
    assert after["email"] == "customer.email"


def test_adding_a_field_changes_the_reported_coverage():
    v1 = schema_with(
        make_field("customer_id", T.STRING),
        make_field("customer_name", T.STRING),
        entity_name="fin_customer",
    )
    v2 = schema_with(
        make_field("customer_id", T.STRING),
        make_field("customer_name", T.STRING),
        make_field("zzz_unmappable_column", T.STRING),
        entity_name="fin_customer",
    )

    assert generate_mapping(v1).coverage.total_fields == 2
    assert generate_mapping(v2).coverage.total_fields == 3
    assert generate_mapping(v2).coverage.coverage_ratio < (
        generate_mapping(v1).coverage.coverage_ratio
    )


def test_a_changed_source_schema_yields_a_new_profile_identity():
    """The profile identity includes the source schema hash, so a mapping
    against V2 is distinguishable from one against V1."""
    v1 = make_schema(
        "evo", SourceType.POSTGRESQL, SchemaOrigin.DISCOVERED,
        entities=(make_entity("fin_customer", (
            make_field("customer_id", T.STRING),
            make_field("customer_name", T.STRING),
        )),),
    )
    v2 = make_schema(
        "evo", SourceType.POSTGRESQL, SchemaOrigin.DISCOVERED,
        entities=(make_entity("fin_customer", (
            make_field("customer_id", T.STRING),
            make_field("customer_name", T.STRING),
            make_field("email", T.STRING),
        )),),
    )
    # Distinct structural hashes, as the catalog would assign.
    object.__setattr__(v2, "schema_hash", "1" * 64)

    first = generate_mapping(v1).profiles[0]
    second = generate_mapping(v2).profiles[0]

    assert first.mapping_id != second.mapping_id


# ============================================================
# Target model evolution (Step 37)
# ============================================================

def test_a_different_target_model_version_yields_a_different_identity():
    schema = schema_with(
        make_field("customer_id", T.STRING),
        make_field("customer_name", T.STRING),
        entity_name="fin_customer",
    )

    v2_model = CanonicalTargetModel(
        model_id=DEFAULT_CANONICAL_MODEL.model_id,
        version="2.0",
        entities=DEFAULT_CANONICAL_MODEL.entities,
    )

    default_profile = generate_mapping(schema).profiles[0]
    v2_profile = generate_mapping(schema, canonical_model=v2_model).profiles[0]

    assert default_profile.mapping_id != v2_profile.mapping_id
    assert v2_profile.metadata["canonical_model_version"] == "2.0"


def test_a_mapping_records_which_target_model_it_saw():
    schema = schema_with(make_field("invoice_no", T.STRING))
    profile = generate_mapping(schema).profiles[0]

    assert profile.metadata["canonical_model_identity"] == "erp_core@1.0"


# ============================================================
# Phase 2 persistence (Step 34)
# ============================================================

@pytest.fixture()
def catalog(pipeline_connector):
    from erp_pipeline.catalog.repository import CatalogRepository
    from erp_pipeline.catalog.schema import bootstrap_catalog
    from erp_pipeline.catalog.service import SchemaCatalogService

    from erp_pipeline.schemas.source_models import SourceSystem

    engine = pipeline_connector._sqlalchemy_engine  # noqa: SLF001 - test setup
    bootstrap_catalog(engine)
    service = SchemaCatalogService(CatalogRepository(engine))

    # Phase 2 requires a registered source system before a mapping profile
    # referencing it can be saved. That is the catalog's own referential rule,
    # and Phase 8 does not work around it.
    for source_system_id in ("cov_probe", "persist_probe"):
        service.register_source_system(
            SourceSystem(
                source_system_id=source_system_id,
                name=f"Phase 8 mapping probe ({source_system_id})",
                source_type=SourceType.POSTGRESQL,
                environment="research",
            )
        )

    yield service

    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                DELETE FROM erp_catalog.field_mappings WHERE mapping_id IN (
                    SELECT mapping_id FROM erp_catalog.mapping_profiles
                    WHERE source_system_id LIKE 'cov_probe%'
                       OR source_system_id LIKE 'persist_probe%')
                """
            )
        )
        connection.execute(
            text(
                "DELETE FROM erp_catalog.mapping_profiles "
                "WHERE source_system_id LIKE 'cov_probe%' "
                "   OR source_system_id LIKE 'persist_probe%'"
            )
        )
        for table in ("source_fields", "source_entities", "source_relationships"):
            connection.execute(
                text(
                    f"""
                    DELETE FROM erp_catalog.{table} WHERE schema_id IN (
                        SELECT schema_id FROM erp_catalog.schema_snapshots
                        WHERE source_system_id IN ('cov_probe', 'persist_probe'))
                    """
                )
            )
        connection.execute(
            text(
                "DELETE FROM erp_catalog.schema_snapshots "
                "WHERE source_system_id IN ('cov_probe', 'persist_probe')"
            )
        )
        connection.execute(
            text(
                "DELETE FROM erp_catalog.source_systems "
                "WHERE source_system_id IN ('cov_probe', 'persist_probe')"
            )
        )


@pytest.fixture()
def persisted_schema():
    return make_schema(
        "persist_probe", SourceType.POSTGRESQL, SchemaOrigin.DISCOVERED,
        entities=(make_entity("fin_invoice", (
            make_field("invoice_no", T.STRING),
            make_field("customer_ref", T.STRING),
            make_field("total_amount", T.DECIMAL),
            make_field("issue_date", T.DATE),
        )),),
    )


@pytest.fixture()
def published_schema(catalog, persisted_schema):
    """Publish the source schema before mapping against it.

    Phase 2 refuses a mapping profile whose ``source_schema_id`` names a
    snapshot it has never seen - correctly, since a mapping bound to a
    non-existent schema could never be applied. It also mirrors the real
    pipeline order: discovery publishes the schema, then mapping references
    that exact snapshot.
    """
    catalog.publish_schema(persisted_schema)
    return persisted_schema


def test_a_generated_profile_saves_through_the_existing_catalog(
    catalog, published_schema
):
    """No second mapping store - the Phase 2 repository, unchanged."""
    service = MappingService()
    result = service.generate(published_schema)

    saved = service.publish(result, catalog)

    assert len(saved) == 1
    assert saved[0].mapping_id == result.profiles[0].mapping_id


def test_a_profile_round_trips_through_the_catalog(catalog, published_schema):
    service = MappingService()
    result = service.generate(published_schema)
    service.publish(result, catalog)

    reloaded = MappingService.load(catalog, result.profiles[0].mapping_id)
    original = result.profiles[0]

    assert reloaded.mapping_id == original.mapping_id
    assert reloaded.source_entity == original.source_entity
    assert reloaded.target_entity_type == original.target_entity_type
    assert reloaded.source_schema_id == original.source_schema_id
    assert len(reloaded.field_mappings) == len(original.field_mappings)


def test_field_mappings_round_trip_with_their_evidence(catalog, published_schema):
    """A profile reloaded from the catalog still explains itself."""
    service = MappingService()
    result = service.generate(published_schema)
    service.publish(result, catalog)

    reloaded = MappingService.load(catalog, result.profiles[0].mapping_id)

    for original, restored in zip(
        result.profiles[0].field_mappings, reloaded.field_mappings
    ):
        assert restored.source_field == original.source_field
        assert restored.target_field == original.target_field
        assert restored.source_type is original.source_type
        assert restored.target_type is original.target_type
        assert restored.confidence == original.confidence
        assert restored.status is original.status
        assert restored.metadata["evidence"] == original.metadata["evidence"]


def test_republishing_an_unchanged_mapping_is_idempotent(catalog, published_schema):
    service = MappingService()

    first = service.generate(published_schema)
    service.publish(first, catalog)

    second = service.generate(published_schema)
    service.publish(second, catalog)

    assert first.profiles[0].mapping_id == second.profiles[0].mapping_id

    reloaded = MappingService.load(catalog, first.profiles[0].mapping_id)
    assert len(reloaded.field_mappings) == len(first.profiles[0].field_mappings)


def test_the_target_model_identity_survives_persistence(catalog, published_schema):
    service = MappingService()
    result = service.generate(published_schema)
    service.publish(result, catalog)

    reloaded = MappingService.load(catalog, result.profiles[0].mapping_id)

    assert reloaded.metadata["canonical_model_identity"] == "erp_core@1.0"
    assert reloaded.metadata["mapping_engine_version"]
    assert reloaded.metadata["applied_to_data"] is False
