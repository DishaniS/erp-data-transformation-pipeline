"""Response classification and structured ERP adaptation (Phase 14).

The tests here assert the two claims the structured path rests on: that the
type of a response is decided by its BYTES rather than by what the server said,
and that an API response is mapped by the pipeline's existing ERP engine rather
than by a second one written for HTTP.
"""

from __future__ import annotations

import pytest

from erp_pipeline.response_adaptation.detector import detect_response_type
from erp_pipeline.response_adaptation.errors import MalformedResponseError
from erp_pipeline.response_adaptation.models import DetectionEvidence, ResponseType
from erp_pipeline.response_adaptation.structured import (
    StructuredResponseAdapter,
    count_leaf_fields,
    flatten_record,
    infer_response_schema,
    unwrap_payload,
)
from erp_pipeline.schemas.enums import SchemaOrigin

# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------


def test_decoded_json_body_is_structured():
    result = detect_response_type("application/json", body={"a": 1})

    assert result.response_type is ResponseType.STRUCTURED
    assert result.evidence is DetectionEvidence.PAYLOAD_STRUCTURE


def test_json_bytes_are_recognised_without_a_content_type():
    result = detect_response_type(None, raw=b'{"invoice": 1}')

    assert result.response_type is ResponseType.STRUCTURED
    assert result.evidence is DetectionEvidence.MAGIC_BYTES


def test_pdf_bytes_beat_a_lying_content_type():
    """A legacy ERP that labels a PDF ``application/json`` is not hypothetical.

    The bytes decide, and the disagreement is REPORTED rather than silently
    resolved - a caller needs to learn their ERP is mislabelling responses.
    """
    result = detect_response_type("application/json", raw=b"%PDF-1.7\n%...")

    assert result.response_type is ResponseType.DOCUMENT
    assert result.evidence is DetectionEvidence.MAGIC_BYTES
    assert result.content_type_mismatch is True
    assert result.detail


def test_png_bytes_are_detected_as_an_image():
    result = detect_response_type("image/png", raw=b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    assert result.response_type is ResponseType.IMAGE
    assert result.content_type_mismatch is False


def test_an_image_claim_with_no_bytes_is_unknown_not_image():
    """Claiming an image without sending one leaves nothing to extract.

    Reporting IMAGE here would route the response to a handler guaranteed to
    fail; UNKNOWN is the truthful answer.
    """
    result = detect_response_type("image/png", body=None, raw=None)

    assert result.response_type is ResponseType.UNKNOWN
    assert result.evidence is DetectionEvidence.CONTENT_TYPE


def test_an_empty_response_falls_back_explicitly():
    result = detect_response_type(None, None, None)

    assert result.response_type is ResponseType.UNKNOWN
    assert result.evidence is DetectionEvidence.FALLBACK


def test_unnameable_bytes_are_binary():
    result = detect_response_type(None, raw=b"\x00\x01\x02\x03\xff\xfe")

    assert result.response_type is ResponseType.BINARY
    assert result.evidence is DetectionEvidence.FALLBACK


# ----------------------------------------------------------------------
# Envelope unwrapping
# ----------------------------------------------------------------------


def test_a_single_nested_record_beside_scalars_is_unwrapped():
    records, path = unwrap_payload(
        {"result": {"inv_no": "INV-1"}, "success": True, "server_time": "t"}
    )

    assert path == ("result",)
    assert records[0]["inv_no"] == "INV-1"


def test_a_list_envelope_is_unwrapped_to_its_records():
    records, path = unwrap_payload({"data": [{"a": 1}, {"a": 2}], "count": 2})

    assert path == ("data",)
    assert len(records) == 2


def test_two_sibling_records_are_not_unwrapped():
    """``{"invoice": ..., "customer": ...}`` is two business objects.

    Picking one would be a guess, so the whole mapping is treated as the
    record. This is the case a hard-coded list of wrapper names gets wrong.
    """
    body = {"invoice": {"inv_no": "INV-1"}, "customer": {"cust_no": "C-1"}}
    records, path = unwrap_payload(body)

    assert path == ()
    assert set(records[0]) == {"invoice", "customer"}


def test_a_bare_record_is_returned_unchanged():
    records, path = unwrap_payload({"inv_no": "INV-1", "total_amt": "1.00"})

    assert path == ()
    assert records[0]["inv_no"] == "INV-1"


def test_nesting_is_followed_through_more_than_one_wrapper():
    records, path = unwrap_payload(
        {"data": {"result": {"inv_no": "INV-1"}, "count": 1}, "success": True}
    )

    assert path == ("data", "result")


def test_unwrapping_stops_at_the_configured_depth():
    body = {"a": {"b": {"c": {"d": {"inv_no": "INV-1"}}}}}
    records, path = unwrap_payload(body, max_depth=2)

    assert len(path) == 2


def test_a_scalar_response_cannot_be_read_as_a_record():
    with pytest.raises(MalformedResponseError):
        unwrap_payload("just a string")


def test_an_unwrapped_record_is_not_treated_as_an_entity_named_result():
    """A wrapper key must never become the ERP entity.

    The wrapper path is reported separately precisely so it stays visible
    without being mistaken for business structure.
    """
    records, path = unwrap_payload({"result": {"inv_no": "INV-1"}, "success": True})

    assert "result" not in records[0]
    assert path == ("result",)


# ----------------------------------------------------------------------
# Field counting
# ----------------------------------------------------------------------


def test_leaf_counting_descends_into_nesting():
    assert count_leaf_fields({"a": 1, "b": {"c": 2, "d": 3}}) == 3


def test_leaf_counting_describes_a_list_by_its_first_element():
    """A reduction ratio should describe the record SHAPE, not the row count.

    Counting every row would make a two-row response look twice as reducible
    as a one-row response carrying identical fields.
    """
    assert count_leaf_fields({"rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}) == 2


def test_flattening_uses_dotted_paths():
    flat = flatten_record({"customer": {"contact": {"email": "a@b.c"}}})

    assert flat == {"customer.contact.email": "a@b.c"}


# ----------------------------------------------------------------------
# Schema inference and ERP mapping
# ----------------------------------------------------------------------


def test_an_inferred_response_schema_is_marked_inferred():
    """A response schema is an observation, never a declared contract.

    It must not be mistakable for something the catalog published.
    """
    schema = infer_response_schema(
        [{"inv_no": "INV-1", "total_amt": "1.00"}],
        "finance_erp",
        endpoint="/api/invoices/INV-1",
    )

    assert schema.origin is SchemaOrigin.INFERRED
    assert schema.entities[0].source_name == "invoices"
    assert {field.source_name for field in schema.entities[0].fields} == {
        "inv_no",
        "total_amt",
    }


def test_a_vendor_response_is_mapped_to_canonical_erp_fields():
    """The headline claim of the structured path.

    ``inv_no``/``cust_ref``/``total_amt``/``curr`` are four different vendors'
    spellings; all four arrive as canonical names because the response went
    through the SAME mapping engine a CSV would.
    """
    record = {
        "inv_no": "INV-204",
        "cust_ref": "CUS-17",
        "total_amt": "45000.00",
        "curr": "LKR",
        "approval_status": "A",
    }
    schema = infer_response_schema([record], "finance_erp",
                                   endpoint="/api/invoices/INV-204")
    result = StructuredResponseAdapter().adapt(
        record, schema, "finance_erp", "/api/invoices/INV-204"
    )

    assert result.entity_type == "invoice"
    assert result.canonical_data["invoice_id"] == "INV-204"
    assert result.canonical_data["customer_id"] == "CUS-17"
    assert result.canonical_data["currency"] == "LKR"
    assert str(result.canonical_data["amount"]) == "45000.00"


def test_a_mapped_response_gets_a_deterministic_canonical_record_id():
    record = {"inv_no": "INV-204", "cust_ref": "CUS-17", "total_amt": "1.00"}
    schema = infer_response_schema([record], "finance_erp",
                                   endpoint="/api/invoices/INV-204")
    adapter = StructuredResponseAdapter()

    first = adapter.adapt(record, schema, "finance_erp", "/api/invoices/INV-204")
    second = adapter.adapt(record, schema, "finance_erp", "/api/invoices/INV-204")

    assert first.canonical_record_id == second.canonical_record_id
    assert first.canonical_record_id.startswith("erp:finance_erp:invoice:")


def test_the_amount_is_converted_to_a_number_not_left_as_a_string():
    """Type conversion is the transformation layer's job and it still runs.

    An ERP that returns ``"45000.00"`` as text should not force a downstream
    model to parse money out of a string.
    """
    from decimal import Decimal

    record = {"inv_no": "INV-1", "cust_ref": "C-1", "total_amt": "45000.00"}
    schema = infer_response_schema([record], "erp", endpoint="/api/invoices")
    result = StructuredResponseAdapter().adapt(record, schema, "erp", "/api/invoices")

    assert isinstance(result.canonical_data["amount"], Decimal)
