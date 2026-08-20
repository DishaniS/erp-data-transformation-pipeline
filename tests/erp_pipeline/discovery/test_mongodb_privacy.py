"""Step 16: no sampled document value may ever reach the inference output.

MongoDB inference is the one part of this framework that reads DATA rather
than metadata, which makes it the one part that could leak business content
into a published catalog. These tests seed documents whose every value is a
unique sentinel and then assert that none of those sentinels appears anywhere
in the serialized schema, the supplemental summary, the field metadata, the
warnings, or an error message.

The sentinels are deliberately chosen to be things that would be genuinely
damaging to leak - an email address, a password, a national ID, an invoice
number - because that is the actual risk being tested, not a hypothetical one.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from erp_pipeline.discovery.models import MongoInferenceOptions
from erp_pipeline.discovery.mongodb import MongoDBSchemaInference
from erp_pipeline.discovery.mongodb_inference import (
    DocumentStructureInference,
    build_source_fields,
)
from erp_pipeline.discovery.service import MongoDBInferenceService

from tests.erp_pipeline.discovery.mongo_fakes import (
    FakeMongoCollection,
    FakeMongoConnector,
    FakeMongoDatabase,
    mongo_connector,
)

#: Every value in the seeded documents. None may appear in any output.
SENTINELS = (
    "john.doe@sentinel-example.invalid",
    "SENTINEL-PASSWORD-9f3a",
    "SENTINEL-INVOICE-100",
    "SENTINEL-CUSTOMER-NAME",
    "SENTINEL-IBAN-GB00SENT0000",
    "SENTINEL-TAG-URGENT",
    "SENTINEL-SKU-ABC",
    "SENTINEL-NOTE-CONFIDENTIAL",
    "SENTINEL-ID-0001",
)

SENTINEL_DOCUMENTS = (
    {
        "_id": "SENTINEL-ID-0001",
        "invoice": "SENTINEL-INVOICE-100",
        "customer": {
            "name": "SENTINEL-CUSTOMER-NAME",
            "email": "john.doe@sentinel-example.invalid",
            "iban": "SENTINEL-IBAN-GB00SENT0000",
        },
        "credentials": {"password": "SENTINEL-PASSWORD-9f3a"},
        "tags": ["SENTINEL-TAG-URGENT"],
        "items": [{"sku": "SENTINEL-SKU-ABC", "qty": 987654321}],
        "note": "SENTINEL-NOTE-CONFIDENTIAL",
        "amount": 123456.789,
        "issued_at": dt.datetime(2026, 5, 17, 4, 5, 6, tzinfo=dt.timezone.utc),
    },
    {
        "_id": "SENTINEL-ID-0002",
        "invoice": "SENTINEL-INVOICE-200",
        "amount": 987654.321,
    },
)


def _assert_clean(payload: str) -> None:
    for sentinel in SENTINELS:
        assert sentinel not in payload, f"leaked sampled value: {sentinel!r}"

    # Numeric values are just as sensitive as text ones.
    for number in ("987654321", "123456.789", "987654.321"):
        assert number not in payload, f"leaked sampled number: {number}"

    # A timestamp drawn from a document would identify a real record.
    assert "2026-05-17" not in payload


def test_no_sampled_value_appears_in_the_serialized_schema():
    connector = mongo_connector({"invoices": SENTINEL_DOCUMENTS})

    schema = MongoDBSchemaInference(connector).infer()

    _assert_clean(json.dumps(schema.to_json_dict(), default=str))


def test_no_sampled_value_appears_in_the_supplemental_summary():
    connector = mongo_connector({"invoices": SENTINEL_DOCUMENTS})

    result = MongoDBInferenceService().infer(connector)

    _assert_clean(json.dumps(result.to_dict(), default=str))


def test_no_sampled_value_appears_in_field_metadata():
    connector = mongo_connector({"invoices": SENTINEL_DOCUMENTS})

    schema = MongoDBSchemaInference(connector).infer()

    for entity in schema.entities:
        for field in entity.fields:
            _assert_clean(json.dumps(dict(field.metadata), default=str))


def test_no_sampled_value_appears_in_raw_observations():
    inference = DocumentStructureInference()
    inference.observe_all(SENTINEL_DOCUMENTS)

    _assert_clean(
        json.dumps([o.to_dict() for o in inference.observations()], default=str)
    )


def test_no_sampled_value_appears_in_warnings():
    collection = FakeMongoCollection(SENTINEL_DOCUMENTS, fail_sorted_find=True)
    inference = MongoDBSchemaInference(
        FakeMongoConnector(FakeMongoDatabase({"invoices": collection})),
        MongoInferenceOptions(max_fields_per_collection=2, max_depth=1),
    )

    inference.infer()

    assert inference.warnings
    _assert_clean(" ".join(inference.warnings))


def test_field_names_are_kept_but_their_values_are_not():
    """The distinction the whole phase rests on: a field CALLED "password" is
    structure and must be reported; the password itself must not be."""
    connector = mongo_connector({"invoices": SENTINEL_DOCUMENTS})

    schema = MongoDBSchemaInference(connector).infer()
    entity = schema.entities[0]

    assert entity.field_by_normalized_name("credentials.password") is not None
    assert entity.field_by_normalized_name("customer.email") is not None
    _assert_clean(json.dumps(schema.to_json_dict(), default=str))


def test_the_observation_model_has_no_field_able_to_hold_a_value():
    """Structural guarantee, not just an assertion about one run: every
    ``FieldObservation`` attribute is a count, a ratio, a path or a flag."""
    from erp_pipeline.discovery.models import FieldObservation

    inference = DocumentStructureInference()
    inference.observe_all(SENTINEL_DOCUMENTS)

    for observation in inference.observations():
        assert isinstance(observation, FieldObservation)
        for name, value in vars(observation).items():
            if name in ("path", "segments"):
                continue  # field names, which ARE the structure
            if name in ("type_counts", "element_type_counts"):
                assert all(isinstance(count, int) for count in value.values())
                continue
            assert isinstance(value, (int, bool)), f"{name} can hold {type(value)}"


def test_an_error_message_never_carries_a_password_or_a_value():
    connector = mongo_connector({"invoices": SENTINEL_DOCUMENTS})
    # The connector holds a password; a raised error must not echo it.
    connector._settings.password = "SENTINEL-PASSWORD-9f3a"  # noqa: SLF001

    failing = FakeMongoConnector(
        FakeMongoDatabase(failing_methods=["list_collections"]),
    )
    failing._settings.password = "SENTINEL-PASSWORD-9f3a"  # noqa: SLF001

    with pytest.raises(Exception) as excinfo:
        MongoDBSchemaInference(failing).infer()

    _assert_clean(str(excinfo.value))


def test_document_ids_are_not_exposed_even_though_id_is_described():
    connector = mongo_connector({"invoices": SENTINEL_DOCUMENTS})

    schema = MongoDBSchemaInference(connector).infer()
    identifier = schema.entities[0].fields[0]

    assert identifier.source_name == "_id"
    assert identifier.is_primary_key is True
    assert "SENTINEL-ID-0001" not in json.dumps(dict(identifier.metadata), default=str)


def test_field_metadata_survives_the_phase_1_credential_denylist():
    """A document field named ``password`` must not trip the contract's
    metadata secret-key check - the name goes in a VALUE slot, never a key."""
    connector = mongo_connector(
        {"secrets": [{"_id": "1", "password": "x", "api_key": "y", "token": "z"}]}
    )

    entity = MongoDBSchemaInference(connector).infer().entities[0]

    assert {field.source_name for field in entity.fields} == {
        "_id", "password", "api_key", "token",
    }
    for field in entity.fields:
        assert all("password" not in key.lower() for key in field.metadata)


def test_build_source_fields_emits_no_values_even_for_unknown_types():
    class Sensitive:
        def __repr__(self) -> str:  # pragma: no cover - defensive
            return "SENTINEL-NOTE-CONFIDENTIAL"

        def __str__(self) -> str:  # pragma: no cover - defensive
            return "SENTINEL-NOTE-CONFIDENTIAL"

    inference = DocumentStructureInference()
    inference.observe({"odd": Sensitive()})

    built = build_source_fields(inference.observations())

    _assert_clean(json.dumps(built.fields[0].to_json_dict(), default=str))
