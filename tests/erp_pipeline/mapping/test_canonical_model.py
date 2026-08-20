"""The canonical target model, and the honesty of its grounding claim.

Phase 8 had to declare a target vocabulary because the repository
deliberately had none. The risk in doing that is passing invented names off as
established ones, so these tests make the distinction machine-checked rather
than merely documented.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from erp_pipeline.mapping import (
    DEFAULT_CANONICAL_MODEL,
    REPOSITORY_INVOICE_FIELDS,
    CanonicalEntity,
    CanonicalField,
    CanonicalTargetModel,
    FieldProvenance,
)
from erp_pipeline.schemas.enums import FieldDataType

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


# ============================================================
# The grounding claim (Step 4)
# ============================================================

def test_the_invoice_entity_reuses_the_repositorys_own_field_names():
    """The strongest form of the claim: the repository's canonical invoice
    example is reproduced field for field, with nothing renamed."""
    invoice = DEFAULT_CANONICAL_MODEL.entity("invoice")

    grounded = {
        field.name
        for field in invoice.fields
        if field.provenance is FieldProvenance.REPOSITORY
    }

    assert grounded == REPOSITORY_INVOICE_FIELDS


def test_the_grounded_names_really_do_appear_in_the_repository():
    """Verified against the actual files, not against a constant in this
    package - a constant could drift from reality; the files cannot."""
    canonical_doc = (REPO_ROOT / "docs" / "canonical_erp_model.md").read_text(
        encoding="utf-8"
    )
    phase1_demo = (
        REPO_ROOT / "tests" / "erp_pipeline" / "test_cross_source_canonicalization.py"
    ).read_text(encoding="utf-8")

    combined = canonical_doc + phase1_demo

    for name in REPOSITORY_INVOICE_FIELDS:
        assert f'"{name}"' in combined, (
            f"{name!r} is claimed to be existing repository vocabulary but "
            "appears in neither the canonical model doc nor the Phase 1 "
            "cross-source demonstration."
        )


def test_the_invoice_entity_type_is_the_one_the_repository_uses():
    phase1_demo = (
        REPO_ROOT / "tests" / "erp_pipeline" / "test_cross_source_canonicalization.py"
    ).read_text(encoding="utf-8")

    assert 'entity_type="invoice"' in phase1_demo
    assert DEFAULT_CANONICAL_MODEL.entity("invoice") is not None


def test_every_extension_states_why_it_was_added():
    """An addition with no justification is an invented ontology."""
    for field in DEFAULT_CANONICAL_MODEL.extension_fields:
        assert field.reason, f"{field.qualified_name} is an extension with no reason"
        assert len(field.reason) > 30, (
            f"{field.qualified_name} has a reason too terse to be meaningful"
        )


def test_extensions_are_a_minority_of_the_invoice_contract():
    """Phase 8 extended the invoice entity by one field, not by twenty."""
    invoice = DEFAULT_CANONICAL_MODEL.entity("invoice")
    extensions = [
        field for field in invoice.fields
        if field.provenance is FieldProvenance.PHASE_8_EXTENSION
    ]

    assert len(extensions) == 1
    assert extensions[0].name == "issued_on"


def test_the_model_stays_small():
    """A guard against the ontology quietly growing. If a future phase needs
    more targets that is fine - but it should be a deliberate, reviewed change
    that updates this number."""
    assert len(DEFAULT_CANONICAL_MODEL.entity_types) == 3
    assert len(tuple(DEFAULT_CANONICAL_MODEL.iter_fields())) == 14


def test_an_extension_without_a_reason_is_refused():
    with pytest.raises(ValueError, match="must state why"):
        CanonicalField(
            entity_type="thing",
            name="mystery",
            data_type=FieldDataType.STRING,
            provenance=FieldProvenance.PHASE_8_EXTENSION,
        )


def test_a_repository_field_needs_no_reason():
    field = CanonicalField(
        entity_type="invoice", name="amount", data_type=FieldDataType.DECIMAL,
        provenance=FieldProvenance.REPOSITORY,
    )

    assert field.reason is None


# ============================================================
# Structure
# ============================================================

def test_qualified_names_are_entity_dot_field():
    field = DEFAULT_CANONICAL_MODEL.field("invoice", "amount")

    assert field.qualified_name == "invoice.amount"


def test_lookup_by_qualified_name():
    field = DEFAULT_CANONICAL_MODEL.field_by_qualified_name("customer.email")

    assert field is not None
    assert field.entity_type == "customer"
    assert field.name == "email"


def test_lookup_of_an_unknown_target_returns_none():
    assert DEFAULT_CANONICAL_MODEL.field_by_qualified_name("nope.nothing") is None
    assert DEFAULT_CANONICAL_MODEL.field_by_qualified_name("bare_name") is None


def test_required_and_identifier_fields_are_declared():
    invoice = DEFAULT_CANONICAL_MODEL.entity("invoice")
    customer = DEFAULT_CANONICAL_MODEL.entity("customer")

    assert {f.name for f in invoice.required_fields} == {
        "invoice_id", "customer_id", "amount",
    }
    assert invoice.identifier_field.name == "invoice_id"
    assert {f.name for f in customer.required_fields} == {"customer_id", "name"}


def test_field_iteration_is_deterministic():
    first = [field.qualified_name for field in DEFAULT_CANONICAL_MODEL.iter_fields()]
    second = [field.qualified_name for field in DEFAULT_CANONICAL_MODEL.iter_fields()]

    assert first == second


def test_duplicate_fields_are_refused():
    with pytest.raises(ValueError, match="more than once"):
        CanonicalEntity(
            entity_type="thing",
            provenance=FieldProvenance.REPOSITORY,
            fields=(
                CanonicalField(entity_type="thing", name="a",
                               data_type=FieldDataType.STRING,
                               provenance=FieldProvenance.REPOSITORY),
                CanonicalField(entity_type="thing", name="a",
                               data_type=FieldDataType.INTEGER,
                               provenance=FieldProvenance.REPOSITORY),
            ),
        )


def test_a_field_listed_under_the_wrong_entity_is_refused():
    with pytest.raises(ValueError, match="declares entity"):
        CanonicalEntity(
            entity_type="thing",
            provenance=FieldProvenance.REPOSITORY,
            fields=(
                CanonicalField(entity_type="other", name="a",
                               data_type=FieldDataType.STRING,
                               provenance=FieldProvenance.REPOSITORY),
            ),
        )


# ============================================================
# Versioning and configurability (Steps 29, 37)
# ============================================================

def test_the_model_declares_a_versioned_identity():
    assert DEFAULT_CANONICAL_MODEL.identity == "erp_core@1.0"


def test_a_model_can_be_built_from_a_plain_dictionary():
    """This is what makes the vocabulary configurable rather than hard-coded:
    a research run can supply its own without editing the package."""
    payload = {
        "model_id": "custom_erp",
        "version": "2.1",
        "entities": [
            {
                "entity_type": "shipment",
                "provenance": "phase_8_extension",
                "reason": "a custom research vocabulary",
                "aliases": ["shipments"],
                "fields": [
                    {
                        "name": "shipment_id",
                        "data_type": "string",
                        "required": True,
                        "is_identifier": True,
                        "aliases": ["ship_no"],
                        "provenance": "phase_8_extension",
                        "reason": "declared by the research configuration",
                    }
                ],
            }
        ],
    }

    model = CanonicalTargetModel.from_dict(payload)

    assert model.identity == "custom_erp@2.1"
    assert model.field("shipment", "shipment_id").aliases == ("ship_no",)


def test_a_model_round_trips_through_its_dictionary_form():
    rebuilt = CanonicalTargetModel.from_dict(DEFAULT_CANONICAL_MODEL.to_dict())

    assert rebuilt.identity == DEFAULT_CANONICAL_MODEL.identity
    assert [f.qualified_name for f in rebuilt.iter_fields()] == [
        f.qualified_name for f in DEFAULT_CANONICAL_MODEL.iter_fields()
    ]


def test_the_model_serializes_to_json_safe_output():
    payload = json.dumps(DEFAULT_CANONICAL_MODEL.to_dict())

    assert "erp_core" in payload
    assert "phase_8_extension" in payload
    assert "repository" in payload


# ============================================================
# Alias hygiene
# ============================================================

def test_no_field_lists_its_own_name_as_an_alias():
    """Harmless but noisy, and a sign of a copy-paste error in the registry."""
    for field in DEFAULT_CANONICAL_MODEL.iter_fields():
        assert field.name not in field.aliases, field.qualified_name


def test_aliases_are_plausible_field_spellings():
    """No whitespace, no punctuation beyond dots and underscores - anything
    else means an alias was written as prose."""
    pattern = re.compile(r"^[A-Za-z0-9_.]+$")

    for field in DEFAULT_CANONICAL_MODEL.iter_fields():
        for alias in field.aliases:
            assert pattern.match(alias), f"{field.qualified_name}: {alias!r}"


def test_every_entity_has_an_identifier():
    for entity in DEFAULT_CANONICAL_MODEL.entities:
        assert entity.identifier_field is not None, entity.entity_type
