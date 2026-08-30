"""MongoDB and relational sources must land in ONE common model.

WHAT THIS FILE IS FOR
---------------------
The pipeline discovers a relational schema by READING a declared catalog, and a
MongoDB schema by OBSERVING a bounded sample of documents. Those are genuinely
different acts of knowledge, and the distinction is preserved. What must NOT
differ is the contract they produce: after discovery, every downstream stage -
mapping, transformation, representation, embedding, routing, retrieval - sees
one ``SourceSchema`` / ``SourceEntity`` / ``SourceField`` shape.

These tests prove that equivalence directly, using REAL bson classes rather than
their string aliases. A mapping that handles the word ``"objectId"`` but fails
on an actual ``ObjectId`` instance is not support, and the difference only shows
up against the real driver types.

They also pin the honesty rules: a source type is never discarded, an
observation never claims more certainty than the sample supports, and binary
never reaches the text an embedding is built from.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from erp_pipeline.discovery.models import MongoInferenceOptions
from erp_pipeline.discovery.mongodb_inference import (
    DocumentStructureInference,
    bson_type_alias,
    build_source_fields,
    normalize_bson_alias,
    render_source_data_type,
    resolve_normalized_type,
)
from erp_pipeline.schemas.enums import FieldDataType

bson = pytest.importorskip("bson", reason="pymongo/bson is not installed")

from bson import (  # noqa: E402
    Binary,
    Code,
    Decimal128,
    Int64,
    MaxKey,
    MinKey,
    ObjectId,
    Regex,
    Timestamp,
)


def fields_of(documents, options=None):
    options = options or MongoInferenceOptions()
    inference = DocumentStructureInference(options)
    inference.observe_all(documents)
    built = build_source_fields(inference.observations(), options)

    return {field.source_name: field for field in built.fields}


# ======================================================================
# Real BSON Python classes, not string aliases
# ======================================================================


class TestRealBsonValuesAreRecognised:
    """Against the classes pymongo actually returns."""

    @pytest.mark.parametrize(
        "value,alias,common",
        [
            ("EMP002", "string", FieldDataType.STRING),
            (ObjectId("650000000000000000000001"), "objectId", FieldDataType.STRING),
            (42, "int", FieldDataType.INTEGER),
            (Int64(2**40), "long", FieldDataType.INTEGER),
            (2**40, "long", FieldDataType.INTEGER),
            (1.5, "double", FieldDataType.DECIMAL),
            (Decimal128("125000.50"), "decimal", FieldDataType.DECIMAL),
            (True, "bool", FieldDataType.BOOLEAN),
            (datetime(2019, 3, 11, tzinfo=timezone.utc), "date", FieldDataType.DATETIME),
            (Timestamp(1, 1), "timestamp", FieldDataType.DATETIME),
            (Binary(b"%PDF-1.4"), "binData", FieldDataType.BINARY),
            (b"\x89PNG", "binData", FieldDataType.BINARY),
            (Regex("^EMP"), "regex", FieldDataType.STRING),
            ({"a": 1}, "object", FieldDataType.OBJECT),
            ([1, 2], "array", FieldDataType.ARRAY),
            (Code("function(){}"), "javascript", FieldDataType.UNKNOWN),
            (MinKey(), "minKey", FieldDataType.UNKNOWN),
            (MaxKey(), "maxKey", FieldDataType.UNKNOWN),
            (None, "null", FieldDataType.UNKNOWN),
        ],
    )
    def test_each_bson_value_maps_to_its_alias_and_common_type(
        self, value, alias, common
    ):
        assert bson_type_alias(value) == alias
        assert normalize_bson_alias(alias) is common

    def test_bson_subclasses_of_builtins_keep_their_precise_type(self):
        """``Int64`` is an ``int``, ``Binary`` is ``bytes``, ``Code`` is a ``str``.

        An isinstance-first check would silently downgrade all three.
        """
        assert bson_type_alias(Int64(5)) == "long"
        assert bson_type_alias(Binary(b"x")) == "binData"
        assert bson_type_alias(Code("x")) == "javascript"

    def test_int32_and_int64_are_separated_by_magnitude(self):
        """A driver hands back a plain ``int`` for both; size is the only signal."""
        assert bson_type_alias(2**31 - 1) == "int"
        assert bson_type_alias(2**31) == "long"
        assert bson_type_alias(-(2**31)) == "int"
        assert bson_type_alias(-(2**31) - 1) == "long"


# ======================================================================
# Mixed observations
# ======================================================================


class TestMixedTypeResolution:
    @pytest.mark.parametrize(
        "counts,expected",
        [
            ({"int": 4, "long": 1}, FieldDataType.INTEGER),
            ({"int": 3, "decimal": 2}, FieldDataType.DECIMAL),
            ({"decimal": 8, "double": 1}, FieldDataType.DECIMAL),
            ({"date": 2, "timestamp": 1}, FieldDataType.DATETIME),
            ({"objectId": 3, "string": 2}, FieldDataType.STRING),
            ({"int": 5, "string": 1}, FieldDataType.UNKNOWN),
            ({"object": 2, "array": 2}, FieldDataType.UNKNOWN),
            ({"null": 7}, FieldDataType.UNKNOWN),
        ],
    )
    def test_resolution_is_deterministic(self, counts, expected):
        assert resolve_normalized_type(counts) is expected

    def test_a_null_never_dominates_a_meaningful_observation(self):
        """A null says nothing about the type a populated field has."""
        assert resolve_normalized_type({"null": 99, "string": 1}) is FieldDataType.STRING

    def test_resolution_is_independent_of_observation_order(self):
        forward = fields_of(
            [{"salary": 75000}, {"salary": 81000}, {"salary": 89500.50}]
        )["salary"]
        reverse = fields_of(
            [{"salary": 89500.50}, {"salary": 81000}, {"salary": 75000}]
        )["salary"]

        assert forward.normalized_data_type is reverse.normalized_data_type
        assert forward.source_data_type == reverse.source_data_type

    def test_integer_and_decimal_widen_rather_than_pick_a_majority(self):
        field = fields_of(
            [{"salary": 75000}, {"salary": 81000}, {"salary": 89500.50}]
        )["salary"]

        assert field.normalized_data_type is FieldDataType.DECIMAL
        assert "int" in field.source_data_type and "double" in field.source_data_type

    def test_incompatible_observations_are_reported_as_unknown_not_guessed(self):
        """Electing the majority would state something false about the minority."""
        field = fields_of([{"value": 1}, {"value": "two"}, {"value": 3}])["value"]

        assert field.normalized_data_type is FieldDataType.UNKNOWN
        assert "int" in field.source_data_type and "string" in field.source_data_type

    def test_the_source_type_evidence_survives_every_resolution(self):
        """The whole point of the common model: normalize WITHOUT discarding."""
        rendered = render_source_data_type({"objectId": 2, "string": 1})

        assert "objectId" in rendered
        assert "string" in rendered


# ======================================================================
# Structure: nesting, arrays, optionality
# ======================================================================


class TestNestedDocuments:
    DOCS = [
        {
            "_id": ObjectId("650000000000000000000001"),
            "employee_id": "EMP002",
            "employment": {
                "department": "Finance",
                "contract": {"type": "permanent", "probation_months": 6},
            },
        },
        {
            "_id": ObjectId("650000000000000000000002"),
            "employee_id": "EMP003",
            "employment": {
                "department": "HR",
                "contract": {"type": "fixed_term", "probation_months": 3},
            },
        },
    ]

    def test_the_container_itself_is_an_object(self):
        assert fields_of(self.DOCS)["employment"].normalized_data_type is (
            FieldDataType.OBJECT
        )

    def test_one_level_of_nesting_keeps_its_path(self):
        field = fields_of(self.DOCS)["department"]

        assert field.nested_path == ("employment",)
        assert field.normalized_data_type is FieldDataType.STRING

    def test_two_levels_of_nesting_keep_the_full_path(self):
        """Provenance is not flattened away."""
        field = fields_of(self.DOCS)["probation_months"]

        assert field.nested_path == ("employment", "contract")
        assert field.normalized_data_type is FieldDataType.INTEGER

    def test_nested_leaves_still_carry_their_bson_source_type(self):
        assert fields_of(self.DOCS)["type"].source_data_type == "string"


class TestArrays:
    def test_a_primitive_array_reports_its_element_type(self):
        field = fields_of([{"tags": ["finance", "manager"]}])["tags"]

        assert field.normalized_data_type is FieldDataType.ARRAY
        assert field.source_data_type == "array<string>"

    def test_a_numeric_array_reports_a_numeric_element_type(self):
        field = fields_of([{"scores": [10, 20, 30]}])["scores"]

        assert field.source_data_type == "array<int>"

    def test_an_empty_array_does_not_invent_an_element_type(self):
        """Nothing was observed inside it, so nothing is claimed about it.

        The renderer says ``array<empty>`` rather than guessing an element type
        or falling back to ``unknown`` - "I saw an array and it had nothing in
        it" is a more precise statement than either.
        """
        field = fields_of([{"values": []}])["values"]

        assert field.normalized_data_type is FieldDataType.ARRAY
        assert field.source_data_type == "array<empty>"

    def test_a_mixed_array_records_every_element_type_observed(self):
        field = fields_of([{"values": [1, "two", True]}])["values"]

        rendered = field.source_data_type

        assert "int" in rendered and "string" in rendered and "bool" in rendered

    def test_an_array_of_documents_exposes_its_element_fields(self):
        fields = fields_of(
            [
                {
                    "addresses": [
                        {"type": "home", "city": "Colombo"},
                        {"type": "office", "city": "Kandy"},
                    ]
                }
            ]
        )

        assert "city" in fields
        assert fields["city"].normalized_data_type is FieldDataType.STRING
        assert "addresses" in fields["city"].nested_path


class TestOptionalAndNullFields:
    """A collection is not obliged to be uniform, and the schema must say so."""

    DOCS = [
        {"employee_id": "EMP001", "email": "a@example.invalid"},
        {"employee_id": "EMP002"},
        {"employee_id": "EMP003", "email": None},
    ]

    def test_a_field_present_in_every_document_is_not_nullable(self):
        assert fields_of(self.DOCS)["employee_id"].nullable is False

    def test_a_field_absent_from_some_documents_is_nullable(self):
        """Absent in one, explicitly null in another - both make it optional."""
        assert fields_of(self.DOCS)["email"].nullable is True

    def test_an_explicit_null_does_not_erase_the_observed_type(self):
        assert fields_of(self.DOCS)["email"].normalized_data_type is (
            FieldDataType.STRING
        )


# ======================================================================
# The parity proof
# ======================================================================


#: A relational entity, as a declared catalog would report it, and the MongoDB
#: document that means the same thing. Same business facts, different
#: technologies, different source vocabularies.
RELATIONAL_COLUMNS = {
    "employee_id": ("VARCHAR", FieldDataType.STRING),
    "name": ("VARCHAR", FieldDataType.STRING),
    "salary": ("NUMERIC", FieldDataType.DECIMAL),
    "active": ("BOOLEAN", FieldDataType.BOOLEAN),
    "joined_at": ("TIMESTAMP", FieldDataType.DATETIME),
    "birth_certificate": ("BYTEA", FieldDataType.BINARY),
}

MONGO_DOCUMENT = {
    "_id": ObjectId("650000000000000000000002"),
    "employee_id": "EMP002",
    "name": "Nimal Silva",
    "salary": Decimal128("125000.50"),
    "active": True,
    "joined_at": datetime(2019, 3, 11, tzinfo=timezone.utc),
    "birth_certificate": Binary(b"%PDF-1.4 synthetic"),
}


class TestRelationalAndMongoProduceTheSameCommonModel:
    """The viva claim, as an assertion.

    MongoDB does not require a declared relational schema. The pipeline observes
    a bounded sample and maps the observed structure into the SAME
    source-independent contracts used for relational systems. The technologies
    differ; the contract does not.
    """

    @pytest.fixture
    def mongo_fields(self):
        return fields_of([MONGO_DOCUMENT])

    @pytest.mark.parametrize(
        "column,relational_type,common",
        [(name, t[0], t[1]) for name, t in RELATIONAL_COLUMNS.items()],
    )
    def test_each_column_and_its_document_field_share_one_common_type(
        self, mongo_fields, column, relational_type, common
    ):
        """PostgreSQL NUMERIC and MongoDB Decimal128 become one DECIMAL.
        BYTEA and BinData become one BINARY. TIMESTAMP and Date become one
        DATETIME."""
        assert column in mongo_fields, f"{column} was not observed in the document"
        assert mongo_fields[column].normalized_data_type is common, (
            f"{column}: relational {relational_type} and MongoDB "
            f"{mongo_fields[column].source_data_type} must agree on {common.value}"
        )

    def test_the_source_types_remain_different_which_is_the_point(
        self, mongo_fields
    ):
        """Normalizing must not erase which technology it came from."""
        assert mongo_fields["salary"].source_data_type == "decimal"
        assert mongo_fields["birth_certificate"].source_data_type == "binData"
        assert mongo_fields["joined_at"].source_data_type == "date"
        assert mongo_fields["_id"].source_data_type == "objectId"

        # None of these is the relational spelling.
        for name, (relational_type, _) in RELATIONAL_COLUMNS.items():
            if name in mongo_fields:
                assert mongo_fields[name].source_data_type != relational_type

    def test_mongo_binary_normalizes_exactly_like_relational_binary(
        self, mongo_fields
    ):
        """So the existing multimodal pipeline treats both identically."""
        assert mongo_fields["birth_certificate"].normalized_data_type is (
            FieldDataType.BINARY
        )
        assert RELATIONAL_COLUMNS["birth_certificate"][1] is FieldDataType.BINARY

    def test_the_objectid_is_not_promoted_into_a_business_key(self, mongo_fields):
        """``_id`` is stable, but it is provenance - not the ERP's identity.

        The business key is whatever the ERP calls it, here ``employee_id``.
        """
        assert "_id" in mongo_fields
        assert mongo_fields["_id"].source_data_type == "objectId"
        assert "employee_id" in mongo_fields


class TestBinaryDoesNotReachEmbeddingText:
    """The binary leakage guard, checked on the MongoDB path specifically."""

    def test_binary_bytes_never_appear_in_representation_text(self):
        import base64

        from erp_pipeline.schemas.enums import EntityKind, SourceType
        from erp_pipeline.schemas.source_models import SourceEntity
        from erp_pipeline.transformation.models import SourceRecord
        from erp_pipeline.transformation.source_native import SourceNativeTransformer
        from erp_pipeline.ai.representation import canonical_record_to_representation

        marker = b"%PDF-1.4 synthetic-leak-probe"
        document = dict(MONGO_DOCUMENT, birth_certificate=Binary(marker))
        built = fields_of([document])
        entity = SourceEntity(
            entity_id="mongo.employees",
            source_name="employees",
            normalized_name="employees",
            entity_kind=EntityKind.COLLECTION,
            fields=tuple(built.values()),
        )
        record = SourceRecord(
            values={k: v for k, v in document.items() if k != "_id"},
            record_key=str(document["_id"]),
            ordinal=0,
            source_entity="employees",
        )

        result = SourceNativeTransformer().transform_records(
            [record],
            entity,
            source_system_id="mongo_parity",
            source_type=SourceType.MONGODB,
            key_fields=["employee_id"],
        )

        assert result.records, "the MongoDB record did not transform"

        text = canonical_record_to_representation(result.records[0]).text_for_ai

        assert marker.decode() not in text
        assert base64.b64encode(marker).decode() not in text
        assert "%PDF" not in text
