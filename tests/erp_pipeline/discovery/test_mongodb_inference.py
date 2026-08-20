"""Observed-structure inference rules, exercised without any MongoDB server.

Everything here tests ``discovery.mongodb_inference``: BSON type observation,
nested paths, arrays, mixed types, presence statistics and the conservative
requiredness policy. Document sampling and collection discovery are tested in
``test_mongodb_discovery.py``; a real server is used in
``test_live_mongodb_inference.py``.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest

from erp_pipeline.discovery.models import FieldObservation, MongoInferenceOptions
from erp_pipeline.discovery.mongodb_inference import (
    ARRAY_ELEMENT_SEGMENT,
    DocumentStructureInference,
    bson_type_alias,
    build_source_fields,
    normalize_bson_alias,
    render_path,
    render_source_data_type,
    resolve_normalized_type,
)
from erp_pipeline.schemas.enums import FieldDataType
from erp_pipeline.schemas.source_models import SourceField


def infer(documents, options: MongoInferenceOptions | None = None):
    inference = DocumentStructureInference(options)
    inference.observe_all(documents)
    return inference


def observations_by_path(documents, options: MongoInferenceOptions | None = None):
    return {
        observation.path: observation
        for observation in infer(documents, options).observations()
    }


def fields_by_name(documents, options: MongoInferenceOptions | None = None):
    inference = infer(documents, options)
    built = build_source_fields(inference.observations(), options)
    return {field.normalized_name: field for field in built.fields}


# ============================================================
# The brief's worked example
# ============================================================

EXAMPLE_DOCUMENTS = (
    {"invoice": "INV1", "customer": {"id": 22}, "amount": 5000},
    {
        "invoice": "INV2",
        "customer": {"id": 25, "name": "ABC"},
        "amount": 9000,
        "approved": True,
    },
)


def test_worked_example_produces_the_expected_observed_structure():
    observed = observations_by_path(EXAMPLE_DOCUMENTS)

    assert set(observed) == {
        "invoice", "customer", "customer.id", "customer.name", "amount", "approved",
    }
    assert observed["invoice"].presence_ratio == 1.0
    assert observed["customer.id"].presence_ratio == 1.0
    assert observed["customer.name"].presence_ratio == 0.5
    assert observed["amount"].presence_ratio == 1.0
    assert observed["approved"].presence_ratio == 0.5


def test_worked_example_normalized_types():
    fields = fields_by_name(EXAMPLE_DOCUMENTS)

    assert fields["invoice"].normalized_data_type is FieldDataType.STRING
    assert fields["customer.id"].normalized_data_type is FieldDataType.INTEGER
    assert fields["customer.name"].normalized_data_type is FieldDataType.STRING
    assert fields["amount"].normalized_data_type is FieldDataType.INTEGER
    assert fields["approved"].normalized_data_type is FieldDataType.BOOLEAN


# ============================================================
# BSON type observation (Step 8)
# ============================================================

class FakeObjectId:
    """Stands in for ``bson.ObjectId``, which is recognized by class name."""

    __name__ = "ObjectId"


for _fake_name in ("ObjectId", "Decimal128", "Binary", "Int64", "Timestamp", "Code", "MinKey"):
    globals()[f"Fake{_fake_name}"] = type(_fake_name, (), {})


@pytest.mark.parametrize(
    "value,expected_alias",
    [
        (None, "null"),
        ("text", "string"),
        (True, "bool"),
        (False, "bool"),
        (42, "int"),
        (2 ** 40, "long"),
        (-(2 ** 40), "long"),
        (4.5, "double"),
        (Decimal("1.25"), "decimal"),
        (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), "date"),
        (dt.date(2026, 1, 1), "date"),
        (b"\x00\x01", "binData"),
        (bytearray(b"\x00"), "binData"),
        ({"nested": 1}, "object"),
        ([1, 2], "array"),
        ((1, 2), "array"),
        (re.compile("^a"), "regex"),
        (object(), "unknown"),
    ],
)
def test_bson_type_alias_recognizes_common_values(value, expected_alias):
    assert bson_type_alias(value) == expected_alias


@pytest.mark.parametrize(
    "class_name,expected_alias",
    [
        ("ObjectId", "objectId"),
        ("Decimal128", "decimal"),
        ("Binary", "binData"),
        ("Int64", "long"),
        ("Timestamp", "timestamp"),
        ("Code", "javascript"),
        ("MinKey", "minKey"),
    ],
)
def test_bson_driver_types_are_recognized_by_class_name(class_name, expected_alias):
    """Recognized without importing ``bson``, and BEFORE the isinstance table -
    ``Int64`` is an ``int``, ``Binary`` is ``bytes`` and ``Code`` is a ``str``,
    so an isinstance-first order would lose the precise BSON type."""
    instance = globals()[f"Fake{class_name}"]()
    assert bson_type_alias(instance) == expected_alias


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("string", FieldDataType.STRING),
        ("objectId", FieldDataType.STRING),
        ("int", FieldDataType.INTEGER),
        ("long", FieldDataType.INTEGER),
        ("double", FieldDataType.DECIMAL),
        ("decimal", FieldDataType.DECIMAL),
        ("bool", FieldDataType.BOOLEAN),
        ("date", FieldDataType.DATETIME),
        ("timestamp", FieldDataType.DATETIME),
        ("binData", FieldDataType.BINARY),
        ("object", FieldDataType.OBJECT),
        ("array", FieldDataType.ARRAY),
        ("javascript", FieldDataType.UNKNOWN),
        ("unknown", FieldDataType.UNKNOWN),
        ("not_a_bson_type", FieldDataType.UNKNOWN),
    ],
)
def test_bson_aliases_map_onto_the_existing_field_data_type_enum(alias, expected):
    assert normalize_bson_alias(alias) is expected


def test_objectid_preserves_its_source_type_while_normalizing_to_string():
    field = fields_by_name([{"ref": FakeObjectId()}])["ref"]

    assert field.source_data_type == "objectId"
    assert field.normalized_data_type is FieldDataType.STRING


def test_decimal128_datetime_and_binary_are_supported():
    documents = [
        {
            "total": FakeDecimal128(),
            "issued_at": dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc),
            "attachment": b"\x00\x01\x02",
        }
    ]
    fields = fields_by_name(documents)

    assert fields["total"].normalized_data_type is FieldDataType.DECIMAL
    assert fields["total"].source_data_type == "decimal"
    assert fields["issued_at"].normalized_data_type is FieldDataType.DATETIME
    assert fields["attachment"].normalized_data_type is FieldDataType.BINARY
    assert fields["attachment"].source_data_type == "binData"


def test_int64_is_an_integer_and_keeps_its_long_source_type():
    field = fields_by_name([{"counter": FakeInt64()}])["counter"]

    assert field.source_data_type == "long"
    assert field.normalized_data_type is FieldDataType.INTEGER


def test_unknown_bson_type_is_unknown_not_guessed():
    field = fields_by_name([{"odd": object()}])["odd"]

    assert field.normalized_data_type is FieldDataType.UNKNOWN
    assert field.source_data_type == "unknown"


# ============================================================
# Nested objects (Step 6)
# ============================================================

def test_multiple_nesting_levels_are_preserved_as_paths():
    documents = [{"customer": {"id": 22, "contact": {"email": "a@example.com"}}}]

    observed = observations_by_path(documents)

    assert set(observed) == {
        "customer", "customer.id", "customer.contact", "customer.contact.email",
    }


def test_nested_fields_use_nested_path_rather_than_an_ambiguous_flat_name():
    fields = fields_by_name([{"customer": {"contact": {"email": "a@example.com"}}}])

    email = fields["customer.contact.email"]
    assert email.source_name == "email"
    assert email.nested_path == ("customer", "contact")
    assert email.access_path == ("customer", "contact", "email")


def test_parent_of_a_nested_document_is_itself_an_object_field():
    fields = fields_by_name([{"customer": {"id": 1}}])

    assert fields["customer"].normalized_data_type is FieldDataType.OBJECT
    assert fields["customer"].source_data_type == "object"


def test_max_depth_stops_expansion_without_crashing():
    documents = [{"a": {"b": {"c": {"d": 1}}}}]

    inference = infer(documents, MongoInferenceOptions(max_depth=2))
    observed = {item.path: item for item in inference.observations()}

    assert set(observed) == {"a", "a.b"}
    assert observed["a.b"].truncated_due_to_depth is True
    assert observed["a"].truncated_due_to_depth is False
    assert inference.depth_limit_reached is True
    assert inference.partial is True


def test_depth_truncated_parent_stays_an_object_and_is_flagged():
    options = MongoInferenceOptions(max_depth=1)
    fields = fields_by_name([{"a": {"b": 1}}], options)

    assert fields["a"].normalized_data_type is FieldDataType.OBJECT
    assert fields["a"].metadata["truncated_due_to_depth"] is True


# ============================================================
# Arrays (Step 7)
# ============================================================

def test_array_of_primitives():
    field = fields_by_name([{"tags": ["urgent", "approved"]}])["tags"]

    assert field.is_array is True
    assert field.normalized_data_type is FieldDataType.ARRAY
    assert field.source_data_type == "array<string>"


def test_array_of_objects_exposes_element_fields():
    documents = [{"items": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]}]

    observed = observations_by_path(documents)
    assert set(observed) == {"items", "items[].sku", "items[].qty"}

    fields = fields_by_name(documents)
    items = fields["items"]
    assert items.is_array is True
    assert items.source_data_type == "array<object>"

    sku = fields["items_.sku"]
    assert sku.source_name == "sku"
    assert sku.nested_path == ("items", ARRAY_ELEMENT_SEGMENT)
    assert sku.normalized_data_type is FieldDataType.STRING
    assert sku.metadata["field_path"] == "items[].sku"

    assert fields["items_.qty"].normalized_data_type is FieldDataType.INTEGER


def test_mixed_array_is_reported_conservatively():
    field = fields_by_name([{"values": [1, "A", True]}])["values"]

    assert field.is_array is True
    assert field.source_data_type == "array<mixed<bool|int|string>>"
    assert field.metadata["array_element_bson_type_distribution"] == {
        "bool": 1, "int": 1, "string": 1,
    }


def test_empty_array_is_still_an_array():
    field = fields_by_name([{"tags": []}])["tags"]

    assert field.is_array is True
    assert field.source_data_type == "array<empty>"


def test_array_of_arrays_records_the_element_type_without_descending():
    field = fields_by_name([{"matrix": [[1, 2], [3]]}])["matrix"]

    assert field.source_data_type == "array<array>"
    assert "matrix_" not in fields_by_name([{"matrix": [[1, 2]]}])


def test_array_element_cap_bounds_inference_cost():
    documents = [{"items": [{"sku": f"S{index}"} for index in range(100)]}]
    options = MongoInferenceOptions(max_array_elements_per_document=10)

    observed = observations_by_path(documents, options)

    assert observed["items"].element_type_counts == {"object": 10}
    assert observed["items"].array_elements_truncated is True
    assert observed["items[].sku"].value_count == 10


def test_array_element_cap_is_reported_on_the_field():
    documents = [{"tags": ["a", "b", "c"]}]
    options = MongoInferenceOptions(max_array_elements_per_document=2)

    field = fields_by_name(documents, options)["tags"]

    assert field.metadata["array_elements_truncated"] is True
    assert field.metadata["max_array_elements_per_document"] == 2


def test_presence_of_an_array_element_field_counts_documents_not_elements():
    """One document with three matching elements is still ONE document."""
    documents = [
        {"items": [{"sku": "A"}, {"sku": "B"}, {"sku": "C"}]},
        {"items": [{"other": 1}]},
    ]

    observed = observations_by_path(documents)["items[].sku"]

    assert observed.documents_sampled == 2
    assert observed.present_count == 1
    assert observed.value_count == 3
    assert observed.presence_ratio == 0.5


# ============================================================
# Mixed types and the type distribution (Steps 9, 28)
# ============================================================

def test_mixed_integer_and_string_is_unknown_not_silently_chosen():
    documents = [{"value": 10}, {"value": "10"}, {"value": 20.5}]

    field = fields_by_name(documents)["value"]

    assert field.normalized_data_type is FieldDataType.UNKNOWN
    assert field.source_data_type == "mixed<double|int|string>"
    assert field.metadata["mixed_types"] is True
    assert field.metadata["bson_type_distribution"] == {
        "double": 1, "int": 1, "string": 1,
    }
    assert field.metadata["observed"]["values_observed"] == 3


def test_integer_and_decimal_widen_to_decimal():
    documents = [{"amount": 5000}, {"amount": 20.5}]

    field = fields_by_name(documents)["amount"]

    assert field.normalized_data_type is FieldDataType.DECIMAL
    assert field.source_data_type == "mixed<double|int>"


def test_int_and_long_agree_on_integer():
    assert resolve_normalized_type({"int": 3, "long": 1}) is FieldDataType.INTEGER


def test_object_and_array_together_are_unknown():
    assert resolve_normalized_type({"object": 2, "array": 1}) is FieldDataType.UNKNOWN


def test_only_nulls_observed_yields_unknown():
    assert resolve_normalized_type({"null": 5}) is FieldDataType.UNKNOWN

    field = fields_by_name([{"note": None}])["note"]
    assert field.normalized_data_type is FieldDataType.UNKNOWN
    assert field.source_data_type == "null"


def test_nulls_do_not_dilute_an_otherwise_single_type():
    documents = [{"note": "text"}, {"note": None}]

    field = fields_by_name(documents)["note"]

    assert field.normalized_data_type is FieldDataType.STRING
    assert "mixed_types" not in field.metadata


def test_source_data_type_rendering_is_independent_of_counts():
    """The rendering feeds the structural hash, so it must depend only on
    WHICH types were seen - never on how many."""
    assert render_source_data_type({"int": 1, "string": 99}) == render_source_data_type(
        {"int": 99, "string": 1}
    )


# ============================================================
# Presence, nulls, requiredness (Steps 10, 11)
# ============================================================

def test_presence_statistics_are_counted_exactly():
    documents = [{"a": 1}] * 64 + [{"b": 1}] * 36

    observed = observations_by_path(documents)["a"]

    assert observed.documents_sampled == 100
    assert observed.present_count == 64
    assert observed.missing_count == 36
    assert observed.presence_ratio == 0.64


def test_null_statistics_are_counted_separately_from_absence():
    documents = [{"note": "x"}, {"note": None}, {}]

    observed = observations_by_path(documents)["note"]

    assert observed.present_count == 2
    assert observed.missing_count == 1
    assert observed.null_count == 1
    assert observed.null_ratio == 0.5


def test_required_only_when_always_present_and_never_null():
    documents = [{"a": 1, "b": 1, "c": None}, {"a": 2, "c": 3}]

    fields = fields_by_name(documents)

    assert fields["a"].required is True
    assert fields["a"].nullable is False
    # present in only one document
    assert fields["b"].required is False
    assert fields["b"].nullable is True
    # always present, but null once
    assert fields["c"].required is False
    assert fields["c"].nullable is True


def test_requiredness_is_recorded_as_an_observation_not_a_constraint():
    field = fields_by_name([{"a": 1}])["a"]

    assert field.metadata["schema_claim"] == "observed"
    assert field.metadata["inference_method"] == "bounded_document_sample"
    assert field.metadata["observed"]["documents_sampled"] == 1


def test_one_document_makes_every_observed_field_required():
    """Honest, and exactly why the sample size travels with the claim."""
    fields = fields_by_name([{"a": 1, "b": 2}])

    assert all(field.required for field in fields.values())
    assert all(
        field.metadata["observed"]["documents_sampled"] == 1
        for field in fields.values()
    )


def test_no_documents_produces_no_fields():
    inference = infer([])

    assert inference.observations() == ()
    assert build_source_fields(inference.observations()).fields == ()


def test_tracking_flags_switch_off_their_metadata():
    options = MongoInferenceOptions(
        track_presence=False, track_nulls=False, track_type_distribution=False
    )
    field = fields_by_name([{"a": 1}], options)["a"]

    assert "observed" not in field.metadata
    assert "bson_type_distribution" not in field.metadata


# ============================================================
# _id handling (Step 12)
# ============================================================

def test_id_is_the_primary_key_when_present_in_every_document():
    documents = [{"_id": FakeObjectId(), "a": 1}, {"_id": FakeObjectId(), "a": 2}]

    inference = infer(documents)
    built = build_source_fields(inference.observations())
    identifier = built.fields[0]

    assert identifier.source_name == "_id"
    assert identifier.normalized_name == "id"
    assert identifier.is_primary_key is True
    assert identifier.is_unique is True
    assert identifier.nullable is False
    assert identifier.required is True
    assert identifier.source_data_type == "objectId"
    assert built.primary_key_fields == ("id",)


def test_id_is_listed_first_regardless_of_document_key_order():
    documents = [{"zzz": 1, "_id": "x", "aaa": 2}]

    built = build_source_fields(infer(documents).observations())

    assert built.fields[0].source_name == "_id"


def test_a_document_id_value_is_never_exposed():
    documents = [{"_id": "SENTINEL-ID-0001"}]

    built = build_source_fields(infer(documents).observations())

    assert "SENTINEL-ID-0001" not in repr(built.fields[0])


def test_id_absent_from_some_documents_is_not_asserted_as_a_primary_key():
    built = build_source_fields(infer([{"_id": "a"}, {"other": 1}]).observations())

    identifier = next(f for f in built.fields if f.source_name == "_id")
    assert identifier.is_primary_key is False
    assert built.primary_key_fields == ()


def test_id_wins_the_normalized_name_against_a_literal_id_field():
    documents = [{"_id": "a", "id": 7}, {"_id": "b", "id": 8}]

    built = build_source_fields(infer(documents).observations())
    names = [field.normalized_name for field in built.fields]

    assert names[0] == "id"
    assert built.fields[0].source_name == "_id"
    assert "id.2" in names
    assert len(set(names)) == len(names)


# ============================================================
# Field explosion safety (Step 18)
# ============================================================

def test_field_limit_stops_new_paths_and_reports_the_result_as_partial():
    documents = [{f"key_{index}": index for index in range(50)}]
    options = MongoInferenceOptions(max_fields_per_collection=10)

    inference = infer(documents, options)

    assert len(inference.observations()) == 10
    assert inference.field_limit_reached is True
    assert inference.dropped_path_count == 40
    assert inference.partial is True


def test_field_limit_cutoff_does_not_depend_on_document_key_order():
    """Keys are visited in sorted order, so the same collection always keeps
    the same paths when the budget bites."""
    forward = [{"a": 1, "b": 2, "c": 3, "d": 4}]
    reversed_keys = [{"d": 4, "c": 3, "b": 2, "a": 1}]
    options = MongoInferenceOptions(max_fields_per_collection=2)

    assert [o.path for o in infer(forward, options).observations()] == ["a", "b"]
    assert [o.path for o in infer(reversed_keys, options).observations()] == ["a", "b"]


def test_paths_already_recorded_keep_accumulating_after_the_limit_is_hit():
    documents = [{"a": 1, "b": 2}, {"a": 3, "b": 4, "c": 5}]
    options = MongoInferenceOptions(max_fields_per_collection=2)

    observed = observations_by_path(documents, options)

    assert set(observed) == {"a", "b"}
    assert observed["a"].present_count == 2


# ============================================================
# Determinism (Step 15)
# ============================================================

def test_field_ordering_is_deterministic_regardless_of_key_order():
    first = infer([{"b": 1, "a": 2, "c": 3}]).observations()
    second = infer([{"c": 3, "a": 2, "b": 1}]).observations()

    assert [o.path for o in first] == [o.path for o in second]


def test_repeated_inference_over_the_same_documents_is_identical():
    documents = list(EXAMPLE_DOCUMENTS)

    first = build_source_fields(infer(documents).observations()).fields
    second = build_source_fields(infer(documents).observations()).fields

    assert [f.to_json_dict() for f in first] == [f.to_json_dict() for f in second]


# ============================================================
# Robustness
# ============================================================

def test_a_non_document_is_counted_but_contributes_no_structure():
    inference = infer([{"a": 1}, "not a document", None])

    assert inference.documents_sampled == 3
    assert [o.path for o in inference.observations()] == ["a"]


def test_a_blank_field_name_is_skipped_with_a_note_rather_than_crashing():
    built = build_source_fields(infer([{"": 1, "ok": 2}]).observations())

    assert [field.normalized_name for field in built.fields] == ["ok"]
    assert any("blank" in note for note in built.notes)


def test_an_unnameable_field_gets_a_deterministic_fallback_name():
    """``@@@`` normalizes to nothing at all. The path still has to survive,
    with the same name on every run."""
    first = build_source_fields(infer([{"@@@": 1}]).observations())
    second = build_source_fields(infer([{"@@@": 1}]).observations())

    assert first.fields[0].normalized_name.startswith("field.")
    assert first.fields[0].normalized_name == second.fields[0].normalized_name
    assert first.fields[0].source_name == "@@@"
    assert any("normalized name" in note for note in first.notes)


def test_case_differing_keys_both_survive_with_distinct_names():
    built = build_source_fields(infer([{"Amount": 1, "amount": 2}]).observations())

    names = [field.normalized_name for field in built.fields]
    assert len(set(names)) == 2
    assert {field.source_name for field in built.fields} == {"Amount", "amount"}


def test_every_built_field_is_a_phase_1_source_field():
    built = build_source_fields(infer(EXAMPLE_DOCUMENTS).observations())

    assert all(isinstance(field, SourceField) for field in built.fields)
    assert [field.ordinal for field in built.fields] == list(range(len(built.fields)))


def test_render_path_round_trips_the_array_marker():
    assert render_path(("items", ARRAY_ELEMENT_SEGMENT, "sku")) == "items[].sku"
    assert render_path(("customer", "id")) == "customer.id"
    assert render_path(("plain",)) == "plain"


def test_field_observation_ratios_are_safe_for_an_empty_sample():
    observation = FieldObservation(
        path="a", segments=("a",), documents_sampled=0,
        present_count=0, null_count=0, value_count=0,
    )

    assert observation.presence_ratio == 0.0
    assert observation.null_ratio == 0.0
    assert observation.observed_always_present is False
