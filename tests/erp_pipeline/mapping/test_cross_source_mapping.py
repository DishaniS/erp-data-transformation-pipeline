"""PHASE 8 CROSS-SOURCE DEMONSTRATION (Steps 38, 47).

Seven technologies describe the same ERP concepts in seven incompatible
vocabularies. Phases 4-7 made them all produce one ``SourceSchema``. This
module proves the payoff: ONE mapping engine, with no source-specific
branching, maps all of them onto the same canonical targets.

    concept          PostgreSQL      MySQL         MongoDB
    customer id      customer_id     customerId    customer.id
    email            email           email_address customer.contact.email
    invoice total    total_amount    total         financial.total

    concept          CSV             OpenAPI       Postman
    customer id      cust_no         customerId    customer_id
    email            email_addr      contact.email emailAddress
    invoice total    total_amt       totalAmount   amount

That the engine cannot tell these apart - and does not need to - is the
research claim.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from erp_pipeline.mapping import (
    FieldOutcome,
    MappingService,
    generate_mapping,
)
from erp_pipeline.mapping.scoring import render_source_field_path
from erp_pipeline.schemas.mapping_models import FieldMapping, MappingProfile

MAPPING_ROOT = (
    pathlib.Path(__file__).resolve().parents[3] / "src" / "erp_pipeline" / "mapping"
)


def selected_targets(result) -> dict[str, str]:
    """``source field path -> qualified canonical target`` for what was chosen.

    Keyed by field path alone, so a schema whose entities BOTH declare the same
    field name would collapse them. ``selected_targets_by_entity`` keeps them
    apart where that matters.
    """
    return {
        decision.source_field: decision.selected.qualified_target
        for decision in result.decisions
        if decision.selected is not None
    }


def selected_targets_by_entity(result) -> dict[tuple[str, str], str]:
    """``(source entity, field path) -> canonical target``.

    A CSV export legitimately carries ``cust_no`` in both its customer and its
    invoice dataset, mapping to different canonical entities. Both are correct
    and both must be visible.
    """
    return {
        (decision.source_entity, decision.source_field):
            decision.selected.qualified_target
        for decision in result.decisions
        if decision.selected is not None
    }


# ============================================================
# One engine, seven sources
# ============================================================

def test_every_source_technology_is_mapped_by_the_same_api(all_source_schemas):
    """No per-source entry point, no per-source configuration."""
    for name, schema in all_source_schemas.items():
        result = generate_mapping(schema)

        assert result.decisions, name
        assert result.canonical_model_identity == "erp_core@1.0", name


def test_the_customer_identifier_converges_from_every_source(all_source_schemas):
    """Six spellings of one concept, one canonical target.

    All six are read in an INVOICE context, so all six land on
    ``invoice.customer_id`` - the invoice's reference to its customer. Note
    MongoDB's ``customer.id``: a nested customer block inside an invoice
    document is that same reference, not a separate customer record, and the
    engine resolves it accordingly. The customer-table case is asserted
    separately below.
    """
    expected = {
        "postgresql": ("fin_invoice", "customer_id", "invoice.customer_id"),
        "mysql": ("invoices", "customerId", "invoice.customer_id"),
        "mongodb": ("invoices", "customer.id", "invoice.customer_id"),
        "csv": ("invoice_export", "cust_no", "invoice.customer_id"),
        "openapi": ("Invoice", "customerId", "invoice.customer_id"),
        "postman": ("Get Invoice_response_200", "customer_id",
                    "invoice.customer_id"),
    }

    for source, (entity, field_path, target) in expected.items():
        targets = selected_targets_by_entity(
            generate_mapping(all_source_schemas[source])
        )
        assert targets.get((entity, field_path)) == target, (
            f"{source}: {entity}.{field_path} mapped to "
            f"{targets.get((entity, field_path))}, expected {target}"
        )


def test_a_customer_side_identifier_targets_the_customer_entity(
    all_source_schemas,
):
    """The same token in a CUSTOMER context resolves to the customer entity -
    which is what makes entity context worth scoring at all."""
    expected = {
        "postgresql": ("fin_customer", "customer_id", "customer.customer_id"),
        "mysql": ("customers", "customerId", "customer.customer_id"),
        "csv": ("customer_export", "cust_no", "customer.customer_id"),
        "openapi": ("Customer", "customerId", "customer.customer_id"),
    }

    for source, (entity, field_path, target) in expected.items():
        targets = selected_targets_by_entity(
            generate_mapping(all_source_schemas[source])
        )
        assert targets.get((entity, field_path)) == target, (
            f"{source}: {entity}.{field_path} mapped to "
            f"{targets.get((entity, field_path))}"
        )


def test_the_email_concept_converges_from_every_source_that_has_one(
    all_source_schemas,
):
    expected = {
        "postgresql": ("fin_customer", "email", "customer.email"),
        "mysql": ("customers", "email_address", "customer.email"),
        "mongodb": ("invoices", "customer.contact.email", "customer.email"),
        "csv": ("customer_export", "email_addr", "customer.email"),
        "openapi": ("Customer", "contact.email", "customer.email"),
        "postman": ("Get Invoice_response_200", "emailAddress",
                    "customer.email"),
    }

    for source, (entity, field_path, target) in expected.items():
        targets = selected_targets_by_entity(
            generate_mapping(all_source_schemas[source])
        )
        assert targets.get((entity, field_path)) == target, (
            f"{source}: {entity}.{field_path} mapped to "
            f"{targets.get((entity, field_path))}"
        )


def test_the_invoice_total_converges_from_every_source(all_source_schemas):
    expected = {
        "postgresql": ("fin_invoice", "total_amount", "invoice.amount"),
        "mysql": ("invoices", "total", "invoice.amount"),
        "mongodb": ("invoices", "financial.total", "invoice.amount"),
        "csv": ("invoice_export", "total_amt", "invoice.amount"),
        "openapi": ("Invoice", "totalAmount", "invoice.amount"),
        "postman": ("Get Invoice_response_200", "amount", "invoice.amount"),
    }

    for source, (entity, field_path, target) in expected.items():
        targets = selected_targets_by_entity(
            generate_mapping(all_source_schemas[source])
        )
        assert targets.get((entity, field_path)) == target, (
            f"{source}: {entity}.{field_path} mapped to "
            f"{targets.get((entity, field_path))}"
        )


def test_every_source_produces_the_phase_1_contract(all_source_schemas):
    for name, schema in all_source_schemas.items():
        result = generate_mapping(schema)

        assert result.profiles, name
        for profile in result.profiles:
            assert isinstance(profile, MappingProfile), name
            assert all(
                isinstance(item, FieldMapping) for item in profile.field_mappings
            ), name


def test_profiles_from_different_sources_target_the_same_canonical_entities(
    all_source_schemas,
):
    """Different vocabularies, one target model."""
    targets_by_source = {
        name: {profile.target_entity_type for profile in generate_mapping(schema).profiles}
        for name, schema in all_source_schemas.items()
    }

    assert "invoice" in targets_by_source["postgresql"]
    assert "invoice" in targets_by_source["mongodb"]
    assert "invoice" in targets_by_source["csv"]
    assert "customer" in targets_by_source["postgresql"]
    assert "customer" in targets_by_source["csv"]


def test_one_consumer_reads_every_source_without_knowing_its_technology(
    all_source_schemas,
):
    rows = [
        (
            name,
            item.source_field,
            profile.target_entity_type,
            item.target_field,
            item.confidence,
        )
        for name, schema in sorted(all_source_schemas.items())
        for profile in generate_mapping(schema).profiles
        for item in profile.field_mappings
    ]

    assert {row[0] for row in rows} == {
        "postgresql", "mysql", "mongodb", "csv", "openapi", "postman",
    }
    assert len(rows) > 20
    assert all(0.0 <= row[4] <= 1.0 for row in rows)


# ============================================================
# Source independence, proved structurally (Step 15)
# ============================================================

def test_the_engine_contains_no_source_specific_branch():
    """The central research claim, checked in the source rather than asserted.

    A branch on ``SourceType`` inside the mapping package would mean the
    common contract had failed and the engine was quietly special-casing
    technologies again.
    """
    offenders: list[str] = []

    for module_path in sorted(MAPPING_ROOT.rglob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            # SourceType.MYSQL, SourceType.MONGODB, ...
            if isinstance(node.value, ast.Name) and node.value.id == "SourceType":
                offenders.append(f"{module_path.name}: SourceType.{node.attr}")

    assert offenders == [], f"source-specific branching found: {offenders}"


def test_the_mapping_package_never_imports_a_source_technology_module():
    """It consumes SourceSchema, not connectors, discovery, ingestion or
    api_specs."""
    forbidden = {"connectors", "discovery", "ingestion", "api_specs", "bpi2020"}
    offenders: list[str] = []

    for module_path in sorted(MAPPING_ROOT.rglob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                if len(parts) >= 2 and parts[0] == "erp_pipeline":
                    if parts[1] in forbidden:
                        offenders.append(f"{module_path.name}: {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if len(parts) >= 2 and parts[0] == "erp_pipeline":
                        if parts[1] in forbidden:
                            offenders.append(f"{module_path.name}: {alias.name}")

    assert offenders == [], f"mapping depends on a source package: {offenders}"


def test_the_engine_only_reads_the_common_contract():
    """Positive control: it does import the Phase 1 source models."""
    engine_source = (MAPPING_ROOT / "engine.py").read_text(encoding="utf-8")

    assert "from erp_pipeline.schemas.source_models import" in engine_source


# ============================================================
# Source-technology quirks are handled generically
# ============================================================

def test_mongodb_nested_paths_map_without_special_handling(mongodb_schema):
    """Three nested paths, three different resolutions, all from the path.

    ``customer.id`` is the invoice's customer reference and stays on the
    invoice; ``customer.contact.email`` has no invoice-side target so the path
    carries it to the customer entity; ``financial.total`` is the invoice
    amount despite the leaf being a bare ``total``.
    """
    targets = selected_targets(generate_mapping(mongodb_schema))

    assert targets["customer.id"] == "invoice.customer_id"
    assert targets["customer.contact.email"] == "customer.email"
    assert targets["financial.total"] == "invoice.amount"


def test_csv_abbreviations_map_through_the_declared_registry(csv_schema):
    """`cust_no` appears in both CSV datasets and correctly maps to a
    different canonical entity in each - the entity context decides."""
    targets = selected_targets_by_entity(generate_mapping(csv_schema))

    assert targets[("customer_export", "cust_no")] == "customer.customer_id"
    assert targets[("invoice_export", "cust_no")] == "invoice.customer_id"
    assert targets[("invoice_export", "total_amt")] == "invoice.amount"
    assert targets[("customer_export", "email_addr")] == "customer.email"


def test_openapi_camel_case_maps_without_special_handling(openapi_schema):
    result = generate_mapping(openapi_schema)
    targets = selected_targets(result)

    assert targets["invoiceId"] == "invoice.invoice_id"
    assert targets["totalAmount"] == "invoice.amount"
    assert targets["issuedOn"] == "invoice.issued_on"


def test_a_postman_inferred_integer_still_widens_to_a_decimal_target(
    postman_schema,
):
    """Postman inferred `amount` as INTEGER from an example; the canonical
    target is DECIMAL, which is a lossless widening."""
    result = generate_mapping(postman_schema)
    decision = result.decision_for("amount")

    assert decision.outcome is FieldOutcome.AUTO_SELECTED
    assert decision.selected.evidence.type_comparison.compatibility.value == "widening"


def test_a_mongodb_unknown_type_does_not_auto_select(mongodb_schema):
    """An UNKNOWN source type is unproven, not optimistically accepted."""
    result = generate_mapping(mongodb_schema)
    decision = result.decision_for("note")

    assert decision.outcome is not FieldOutcome.AUTO_SELECTED


def test_a_mysql_integer_customer_id_against_a_string_target_is_flagged(
    mysql_schema,
):
    """MySQL types its customer id as INTEGER; the canonical target is a
    STRING business key. Convertible, but not silently."""
    result = generate_mapping(mysql_schema)
    decision = result.decision_for("customerId")

    assert decision.selected is not None
    assert decision.selected.evidence.type_comparison.compatibility.value == "lossy"


# ============================================================
# Coverage across sources
# ============================================================

def test_coverage_is_reported_for_every_source(all_source_schemas):
    for name, schema in all_source_schemas.items():
        coverage = generate_mapping(schema).coverage

        assert coverage.total_fields > 0, name
        assert 0.0 <= coverage.coverage_ratio <= 1.0, name
        assert coverage.entities, name


def test_a_service_instance_is_reusable_across_sources(all_source_schemas):
    """One configured service, many schemas - no per-source state."""
    service = MappingService()

    results = {
        name: service.generate(schema) for name, schema in all_source_schemas.items()
    }

    assert len(results) == 6
    assert all(result.profiles for result in results.values())
