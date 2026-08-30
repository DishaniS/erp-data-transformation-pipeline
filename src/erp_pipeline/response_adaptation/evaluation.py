"""The Phase 14 labelled dataset, the baselines, and the metrics.

WHAT IS BEING MEASURED
----------------------
Whether ERP-aware adaptive transformation produces better LLM context than the
two things a system would otherwise do: send the response as it arrived, or
flatten it generically.

    RAW       the response verbatim. What a system does with no adaptation.
    GENERIC   unwrapped and flattened, no ERP vocabulary, no query awareness.
              This is the FAIR baseline - a competent engineer's first attempt.
    ADAPTIVE  the proposed method.

The generic baseline deliberately gets the envelope unwrapping for free. Making
it worse to flatter the proposed method would be the easiest possible way to
manufacture a result, and unwrapping is not the contribution being claimed.

HOW THE LABELS WERE MADE
------------------------
Each case names the fields a correct answer to its question NEEDS
(``relevant``) and the fields that are operational noise for that question
(``irrelevant``). Both are written against the SOURCE field names, because that
is what every method sees, and the scorer resolves canonical output names back
through the mapping so no method is credited or penalised for renaming.

The labels are the author's, written per case from the question, not derived
from any method's output. That is a real limitation and is stated in the
artifact: a single annotator cannot measure their own agreement.

RECALL IS THE METRIC THAT MATTERS
---------------------------------
A dropped relevant field is unrecoverable downstream - the model cannot ask for
it back. A retained irrelevant field only costs context. The two are not
symmetric, and the artifact reports them separately rather than blending them
into one score that would hide the trade.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from erp_pipeline.response_adaptation.models import (
    AdaptationOptions,
    ResponseEnvelope,
    serialized_size,
)
from erp_pipeline.response_adaptation.service import ResponseAdaptationService
from erp_pipeline.response_adaptation.structured import (
    count_leaf_fields,
    flatten_record,
    unwrap_payload,
)

METHOD_RAW = "raw"
METHOD_GENERIC = "generic"
METHOD_ADAPTIVE = "erp_aware_adaptive"

METHODS = (METHOD_RAW, METHOD_GENERIC, METHOD_ADAPTIVE)


@dataclass(frozen=True)
class EvaluationCase:
    """One labelled response/question pair."""

    case_id: str
    entity: str
    query: str
    body: Any
    #: Source field names a correct answer needs.
    relevant: tuple[str, ...]
    #: Source field names that are noise for THIS question.
    irrelevant: tuple[str, ...]
    source_system_id: str = "finance_erp"
    endpoint: str | None = None
    content_type: str = "application/json"
    note: str | None = None

    def envelope(self) -> ResponseEnvelope:
        return ResponseEnvelope(
            query=self.query,
            source_system_id=self.source_system_id,
            endpoint=self.endpoint,
            http_status=200,
            content_type=self.content_type,
            body=self.body,
        )


# ======================================================================
# The response shapes
# ======================================================================
#
# Each is a realistic ERP payload: vendor-specific field spellings, an
# envelope, and the operational columns real systems carry (row versions, ETL
# batch ids, audit users). Values are synthetic.

_INVOICE = {
    "result": {
        "inv_no": "INV-2041",
        "cust_ref": "CUS-17",
        "total_amt": "45000.00",
        "curr": "LKR",
        "approval_status": "A",
        "issue_dt": "2026-01-05",
        "row_version": 7,
        "etl_batch_id": "B-2026-01",
        "created_by": "svc_integration",
        "last_modified_ts": "2026-01-06T11:02:00Z",
    },
    "success": True,
    "server_time": "2026-08-22T09:00:00Z",
}

_INVOICE_SAP = {
    "d": {
        "BELNR": "INV-9080",
        "KUNNR": "CUS-42",
        "NETWR": "128500.50",
        "WAERS": "EUR",
        "STATUS": "P",
        "BLDAT": "2026-02-14",
        "MANDT": "800",
        "AEDAT": "2026-02-15",
        "ERNAM": "BATCHUSR",
    },
    "success": True,
}

_INVOICE_FLAT = {
    "invoice_number": "INV-3312",
    "customer_number": "CUS-08",
    "grand_total": "9900.00",
    "currency_code": "USD",
    "invoice_status": "REJECTED",
    "document_date": "2026-03-01",
    "internal_seq": 44821,
    "sync_token": "tok-99",
}

_CUSTOMER = {
    "data": {
        "cust_id": "CUS-17",
        "cust_name": "Lanka Traders (Pvt) Ltd",
        "email_addr": "accounts@lankatraders.example",
        "phone_number": "+94112345678",
        "record_status": "ACTIVE",
        "created_on": "2024-06-01",
        "etl_batch_id": "B-2026-01",
        "row_version": 12,
    },
    "count": 1,
}

_CUSTOMER_CONTACT = {
    "customer": {
        "customerid": "CUS-88",
        "display_name": "Northwind Supplies",
        "contact": {"email": "ap@northwind.example", "phone": "+441234567890"},
        "audit": {"created_by": "sys", "updated_by": "sys"},
    }
}

_PURCHASE_ORDER = {
    "result": {
        "po_no": "PO-5512",
        "supplier_no": "SUP-3",
        "order_total": "310000.00",
        "order_status": "OPEN",
        "row_version": 3,
        "etl_batch_id": "B-2026-02",
        "requisition_ref": "REQ-88",
    },
    "success": True,
}

_PURCHASE_ORDER_LIST = {
    "data": [
        {"po_number": "PO-6001", "vendor_id": "SUP-9", "total_amount": "12000.00",
         "state": "CLOSED", "internal_seq": 7},
        {"po_number": "PO-6002", "vendor_id": "SUP-9", "total_amount": "8400.00",
         "state": "OPEN", "internal_seq": 8},
    ],
    "count": 2,
    "page": 1,
}

# A process/case response. The canonical model has no "case" entity, so this
# exercises the passthrough path - which is why it is in the dataset rather
# than being quietly left out.
_PROCESS_CASE = {
    "result": {
        "case_id": "CASE-771",
        "activity": "Approve Purchase Order",
        "resource": "user_14",
        "start_timestamp": "2026-04-01T08:00:00Z",
        "end_timestamp": "2026-04-01T08:12:00Z",
        "variant_index": 3,
        "trace_hash": "9f2c",
        "etl_batch_id": "B-2026-04",
    },
    "success": True,
}

_POLICY_DOCUMENT = {
    "document": {
        "doc_id": "POL-2026-03",
        "title": "Procurement Approval Policy",
        "effective_date": "2026-01-01",
        "owner": "Finance",
        "classification": "INTERNAL",
        "revision": 4,
        "checksum": "ab12cd34",
        "storage_key": "s3://internal/policies/POL-2026-03.pdf",
    },
    "success": True,
}

_RECEIPT_METADATA = {
    "result": {
        "receipt_no": "RCP-4410",
        "merchant_name": "City Fuel Station",
        "total_amount": "7500.00",
        "currency_code": "LKR",
        "captured_on": "2026-05-19",
        "image_width": 1080,
        "image_height": 1920,
        "ocr_confidence": 0.87,
        "upload_batch": "U-77",
    },
    "success": True,
}


def _case(
    case_id: str,
    entity: str,
    query: str,
    body: Any,
    relevant: Sequence[str],
    irrelevant: Sequence[str],
    endpoint: str | None = None,
    note: str | None = None,
) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        entity=entity,
        query=query,
        body=body,
        relevant=tuple(relevant),
        irrelevant=tuple(irrelevant),
        endpoint=endpoint,
        note=note,
    )


# Operational columns that are noise for EVERY question in this dataset. Named
# once so a labelling slip cannot make one case quietly easier than another.
_INVOICE_NOISE = ("row_version", "etl_batch_id", "created_by", "last_modified_ts")
_SAP_NOISE = ("MANDT", "AEDAT", "ERNAM")
_CUSTOMER_NOISE = ("etl_batch_id", "row_version")


def build_cases() -> tuple[EvaluationCase, ...]:
    """The labelled dataset.

    Sixty-six cases across six response families, covering the question forms
    an ERP assistant actually receives: a specific attribute, an identity, a
    status check, a "who"/"when"/"how much" phrasing, and a broad request.
    """
    cases: list[EvaluationCase] = []
    invoice_endpoint = "/api/invoices/INV-2041"

    # -- invoice, vendor-abbreviated ------------------------------------
    cases += [
        _case("inv-01", "invoice", "How much is invoice INV-2041 for?", _INVOICE,
              ["inv_no", "total_amt"], [*_INVOICE_NOISE, "issue_dt"],
              invoice_endpoint),
        _case("inv-02", "invoice", "What currency is this invoice in?", _INVOICE,
              ["inv_no", "curr"], [*_INVOICE_NOISE, "issue_dt", "cust_ref"],
              invoice_endpoint),
        _case("inv-03", "invoice", "Who is the customer on this invoice?", _INVOICE,
              ["inv_no", "cust_ref"], [*_INVOICE_NOISE, "curr", "total_amt"],
              invoice_endpoint),
        _case("inv-04", "invoice", "Has this invoice been approved?", _INVOICE,
              ["inv_no", "approval_status"], [*_INVOICE_NOISE, "curr", "issue_dt"],
              invoice_endpoint),
        _case("inv-05", "invoice", "When was the invoice issued?", _INVOICE,
              ["inv_no", "issue_dt"], [*_INVOICE_NOISE, "curr", "total_amt"],
              invoice_endpoint),
        _case("inv-06", "invoice", "What is the total amount and the currency?",
              _INVOICE, ["inv_no", "total_amt", "curr"],
              [*_INVOICE_NOISE, "issue_dt"], invoice_endpoint),
        _case("inv-07", "invoice", "What is the invoice number?", _INVOICE,
              ["inv_no"], [*_INVOICE_NOISE, "curr", "issue_dt"], invoice_endpoint),
        _case("inv-08", "invoice", "What is the approval status and the amount?",
              _INVOICE, ["inv_no", "approval_status", "total_amt"],
              [*_INVOICE_NOISE, "issue_dt"], invoice_endpoint),
        _case("inv-09", "invoice", "How much does this customer owe on INV-2041?",
              _INVOICE, ["inv_no", "total_amt", "cust_ref"],
              [*_INVOICE_NOISE, "issue_dt"], invoice_endpoint),
        _case("inv-10", "invoice", "What is the invoice date and the customer?",
              _INVOICE, ["inv_no", "issue_dt", "cust_ref"],
              [*_INVOICE_NOISE, "curr"], invoice_endpoint),
        _case("inv-11", "invoice", "Give me everything about this invoice.",
              _INVOICE,
              ["inv_no", "cust_ref", "total_amt", "curr", "approval_status",
               "issue_dt"], [], invoice_endpoint,
              note="broad question: no field is irrelevant"),
        _case("inv-12", "invoice", "Is the invoice in euros?", _INVOICE,
              ["inv_no", "curr"], [*_INVOICE_NOISE, "issue_dt"], invoice_endpoint),
    ]

    # -- invoice, SAP-style opaque field names --------------------------
    # Four-letter German mnemonics. These are the case the canonical alias
    # vocabulary either covers or does not, with no lexical similarity to fall
    # back on - the hardest honest test of the ERP-awareness claim.
    sap_endpoint = "/sap/opu/odata/invoices/INV-9080"
    cases += [
        _case("sap-01", "invoice", "How much is this invoice for?", _INVOICE_SAP,
              ["BELNR", "NETWR"], [*_SAP_NOISE, "BLDAT"], sap_endpoint,
              note="opaque vendor field names"),
        _case("sap-02", "invoice", "What currency?", _INVOICE_SAP,
              ["BELNR", "WAERS"], [*_SAP_NOISE, "BLDAT"], sap_endpoint),
        _case("sap-03", "invoice", "Which customer does this belong to?",
              _INVOICE_SAP, ["BELNR", "KUNNR"], [*_SAP_NOISE, "BLDAT"],
              sap_endpoint),
        _case("sap-04", "invoice", "What is the status of this invoice?",
              _INVOICE_SAP, ["BELNR", "STATUS"], [*_SAP_NOISE, "BLDAT"],
              sap_endpoint),
        _case("sap-05", "invoice", "What is the document date?", _INVOICE_SAP,
              ["BELNR", "BLDAT"], [*_SAP_NOISE, "WAERS"], sap_endpoint),
        _case("sap-06", "invoice", "What is the invoice number?", _INVOICE_SAP,
              ["BELNR"], [*_SAP_NOISE, "BLDAT", "WAERS"], sap_endpoint),
    ]

    # -- invoice, spelled-out field names, no envelope -------------------
    flat_endpoint = "/api/v2/invoices/INV-3312"
    cases += [
        _case("flat-01", "invoice", "How much is invoice INV-3312 for?",
              _INVOICE_FLAT, ["invoice_number", "grand_total"],
              ["internal_seq", "sync_token", "document_date"], flat_endpoint,
              note="no envelope to unwrap"),
        _case("flat-02", "invoice", "Was this invoice rejected?", _INVOICE_FLAT,
              ["invoice_number", "invoice_status"],
              ["internal_seq", "sync_token", "document_date"], flat_endpoint),
        _case("flat-03", "invoice", "Which customer is it for?", _INVOICE_FLAT,
              ["invoice_number", "customer_number"],
              ["internal_seq", "sync_token", "document_date"], flat_endpoint),
        _case("flat-04", "invoice", "What currency is the total in?", _INVOICE_FLAT,
              ["invoice_number", "currency_code"],
              ["internal_seq", "sync_token", "document_date"], flat_endpoint),
        _case("flat-05", "invoice", "When is the document dated?", _INVOICE_FLAT,
              ["invoice_number", "document_date"],
              ["internal_seq", "sync_token", "currency_code"], flat_endpoint),
        _case("flat-06", "invoice", "What is the status and the total?",
              _INVOICE_FLAT,
              ["invoice_number", "invoice_status", "grand_total"],
              ["internal_seq", "sync_token", "document_date"], flat_endpoint),
    ]

    # -- customer --------------------------------------------------------
    customer_endpoint = "/api/customers/CUS-17"
    cases += [
        _case("cus-01", "customer", "What is the customer's name?", _CUSTOMER,
              ["cust_id", "cust_name"],
              [*_CUSTOMER_NOISE, "created_on", "phone_number"], customer_endpoint),
        _case("cus-02", "customer", "What is their email address?", _CUSTOMER,
              ["cust_id", "email_addr"],
              [*_CUSTOMER_NOISE, "created_on", "phone_number"], customer_endpoint),
        _case("cus-03", "customer", "How do I contact this customer?", _CUSTOMER,
              ["cust_id", "email_addr", "phone_number"],
              [*_CUSTOMER_NOISE, "created_on"], customer_endpoint),
        _case("cus-04", "customer", "What is the phone number?", _CUSTOMER,
              ["cust_id", "phone_number"],
              [*_CUSTOMER_NOISE, "created_on", "email_addr"], customer_endpoint),
        _case("cus-05", "customer", "Is this customer active?", _CUSTOMER,
              ["cust_id", "record_status"],
              [*_CUSTOMER_NOISE, "created_on", "email_addr"], customer_endpoint),
        _case("cus-06", "customer", "What is the customer id?", _CUSTOMER,
              ["cust_id"],
              [*_CUSTOMER_NOISE, "created_on", "phone_number"], customer_endpoint),
        _case("cus-07", "customer", "When was the customer created?", _CUSTOMER,
              ["cust_id", "created_on"],
              [*_CUSTOMER_NOISE, "email_addr", "phone_number"], customer_endpoint),
        _case("cus-08", "customer", "Give me the full customer record.", _CUSTOMER,
              ["cust_id", "cust_name", "email_addr", "phone_number",
               "record_status", "created_on"], [], customer_endpoint,
              note="broad question"),
        _case("cus-09", "customer", "Who is this customer and what is their name?",
              _CUSTOMER, ["cust_id", "cust_name"],
              [*_CUSTOMER_NOISE, "created_on"], customer_endpoint),
    ]

    # -- customer, nested contact block ----------------------------------
    nested_endpoint = "/api/customers/CUS-88"
    cases += [
        _case("nest-01", "customer", "What is the email address?",
              _CUSTOMER_CONTACT, ["contact.email"],
              ["audit.created_by", "audit.updated_by"], nested_endpoint,
              note="nested field, dotted path"),
        _case("nest-02", "customer", "How do I reach them by phone?",
              _CUSTOMER_CONTACT, ["contact.phone"],
              ["audit.created_by", "audit.updated_by"], nested_endpoint),
        _case("nest-03", "customer", "What is the company called?",
              _CUSTOMER_CONTACT, ["display_name"],
              ["audit.created_by", "audit.updated_by"], nested_endpoint),
        _case("nest-04", "customer", "What is the customer id?", _CUSTOMER_CONTACT,
              ["customerid"], ["audit.created_by", "audit.updated_by"],
              nested_endpoint),
        _case("nest-05", "customer", "Give me their contact details.",
              _CUSTOMER_CONTACT, ["contact.email", "contact.phone"],
              ["audit.created_by", "audit.updated_by"], nested_endpoint),
    ]

    # -- purchase order ---------------------------------------------------
    po_endpoint = "/api/purchase-orders/PO-5512"
    po_noise = ["row_version", "etl_batch_id"]
    cases += [
        _case("po-01", "purchase_order", "What is the total on this purchase order?",
              _PURCHASE_ORDER, ["po_no", "order_total"],
              [*po_noise, "requisition_ref"], po_endpoint),
        _case("po-02", "purchase_order", "Which supplier is this order with?",
              _PURCHASE_ORDER, ["po_no", "supplier_no"],
              [*po_noise, "requisition_ref"], po_endpoint),
        _case("po-03", "purchase_order", "Is the order still open?", _PURCHASE_ORDER,
              ["po_no", "order_status"], [*po_noise, "requisition_ref"],
              po_endpoint),
        _case("po-04", "purchase_order", "What is the PO number?", _PURCHASE_ORDER,
              ["po_no"], [*po_noise, "requisition_ref"], po_endpoint),
        _case("po-05", "purchase_order", "How much did we order and from whom?",
              _PURCHASE_ORDER, ["po_no", "order_total", "supplier_no"],
              [*po_noise, "requisition_ref"], po_endpoint),
        _case("po-06", "purchase_order", "What is the order status and the vendor?",
              _PURCHASE_ORDER, ["po_no", "order_status", "supplier_no"],
              [*po_noise, "requisition_ref"], po_endpoint),
        _case("po-07", "purchase_order", "Give me the whole purchase order.",
              _PURCHASE_ORDER,
              ["po_no", "supplier_no", "order_total", "order_status"], [],
              po_endpoint, note="broad question"),
    ]

    # -- purchase order, list response ------------------------------------
    po_list_endpoint = "/api/purchase-orders"
    cases += [
        _case("polist-01", "purchase_order", "What is the total on this order?",
              _PURCHASE_ORDER_LIST, ["po_number", "total_amount"],
              ["internal_seq"], po_list_endpoint,
              note="list envelope; only the first record is adapted"),
        _case("polist-02", "purchase_order", "Which vendor supplied it?",
              _PURCHASE_ORDER_LIST, ["po_number", "vendor_id"], ["internal_seq"],
              po_list_endpoint),
        _case("polist-03", "purchase_order", "Is it closed?", _PURCHASE_ORDER_LIST,
              ["po_number", "state"], ["internal_seq"], po_list_endpoint),
        _case("polist-04", "purchase_order", "What is the order number?",
              _PURCHASE_ORDER_LIST, ["po_number"], ["internal_seq"],
              po_list_endpoint),
    ]

    # -- process / case ---------------------------------------------------
    # The canonical model has no "case" entity. These measure what happens when
    # ERP vocabulary does NOT cover the response - the honest hard case.
    case_endpoint = "/api/process/cases/CASE-771"
    case_noise = ["variant_index", "trace_hash", "etl_batch_id"]
    cases += [
        _case("proc-01", "process_case", "Which activity was performed?",
              _PROCESS_CASE, ["case_id", "activity"], case_noise, case_endpoint,
              note="no canonical entity covers this response"),
        _case("proc-02", "process_case", "Who performed this activity?",
              _PROCESS_CASE, ["case_id", "resource"], case_noise, case_endpoint),
        _case("proc-03", "process_case", "When did the case start?", _PROCESS_CASE,
              ["case_id", "start_timestamp"], case_noise, case_endpoint),
        _case("proc-04", "process_case", "What is the case id?", _PROCESS_CASE,
              ["case_id"], case_noise, case_endpoint),
        _case("proc-05", "process_case", "When did it start and end?",
              _PROCESS_CASE, ["case_id", "start_timestamp", "end_timestamp"],
              case_noise, case_endpoint),
        _case("proc-06", "process_case", "What activity and which resource?",
              _PROCESS_CASE, ["case_id", "activity", "resource"], case_noise,
              case_endpoint),
    ]

    # -- policy document metadata -----------------------------------------
    doc_endpoint = "/api/documents/POL-2026-03"
    doc_noise = ["checksum", "storage_key", "revision"]
    cases += [
        _case("doc-01", "document", "What is this policy called?", _POLICY_DOCUMENT,
              ["doc_id", "title"], doc_noise, doc_endpoint),
        _case("doc-02", "document", "When did the policy take effect?",
              _POLICY_DOCUMENT, ["doc_id", "effective_date"], doc_noise,
              doc_endpoint),
        _case("doc-03", "document", "Who owns this document?", _POLICY_DOCUMENT,
              ["doc_id", "owner"], doc_noise, doc_endpoint),
        _case("doc-04", "document", "How is this document classified?",
              _POLICY_DOCUMENT, ["doc_id", "classification"], doc_noise,
              doc_endpoint),
        _case("doc-05", "document", "What is the document id?", _POLICY_DOCUMENT,
              ["doc_id"], doc_noise, doc_endpoint),
        _case("doc-06", "document", "What is the title and the owner?",
              _POLICY_DOCUMENT, ["doc_id", "title", "owner"], doc_noise,
              doc_endpoint),
    ]

    # -- receipt / image metadata ------------------------------------------
    rcp_endpoint = "/api/receipts/RCP-4410"
    rcp_noise = ["image_width", "image_height", "ocr_confidence", "upload_batch"]
    cases += [
        _case("rcp-01", "receipt", "How much was this receipt for?",
              _RECEIPT_METADATA, ["receipt_no", "total_amount"], rcp_noise,
              rcp_endpoint),
        _case("rcp-02", "receipt", "Which merchant issued it?", _RECEIPT_METADATA,
              ["receipt_no", "merchant_name"], rcp_noise, rcp_endpoint),
        _case("rcp-03", "receipt", "What currency was it in?", _RECEIPT_METADATA,
              ["receipt_no", "currency_code"], rcp_noise, rcp_endpoint),
        _case("rcp-04", "receipt", "When was the receipt captured?",
              _RECEIPT_METADATA, ["receipt_no", "captured_on"], rcp_noise,
              rcp_endpoint),
        _case("rcp-05", "receipt", "What is the receipt number?",
              _RECEIPT_METADATA, ["receipt_no"], rcp_noise, rcp_endpoint),
        _case("rcp-06", "receipt", "How much and from which merchant?",
              _RECEIPT_METADATA,
              ["receipt_no", "total_amount", "merchant_name"], rcp_noise,
              rcp_endpoint),
        _case("rcp-07", "receipt", "Give me the receipt details.",
              _RECEIPT_METADATA,
              ["receipt_no", "merchant_name", "total_amount", "currency_code",
               "captured_on"], [], rcp_endpoint, note="broad question"),
    ]

    return tuple(cases)


# ======================================================================
# The methods under comparison
# ======================================================================


@dataclass(frozen=True)
class MethodOutput:
    """What one method produced for one case."""

    payload: Mapping[str, Any]
    #: The SOURCE field names present in the output. Resolving canonical names
    #: back to their source is what keeps the comparison fair: no method is
    #: rewarded or punished for renaming a field it correctly kept.
    source_fields: frozenset[str]
    output_bytes: int
    latency_ms: float
    succeeded: bool = True


def run_raw(case: EvaluationCase) -> MethodOutput:
    """The baseline of doing nothing: hand over the response as it arrived."""
    started = time.perf_counter()
    payload = case.body
    elapsed = (time.perf_counter() - started) * 1000.0

    return MethodOutput(
        payload=payload if isinstance(payload, Mapping) else {"body": payload},
        source_fields=frozenset(_leaf_names(flatten_record(payload))),
        output_bytes=serialized_size(payload),
        latency_ms=elapsed,
    )


def run_generic(case: EvaluationCase) -> MethodOutput:
    """Unwrap the envelope and flatten. No ERP vocabulary, no query awareness.

    Given the envelope unwrapping for free ON PURPOSE - it is not the
    contribution being claimed, and withholding it would make the baseline a
    straw man.
    """
    started = time.perf_counter()

    try:
        records, _ = unwrap_payload(case.body)
        flat = flatten_record(records[0])
        succeeded = True
    except Exception:  # noqa: BLE001 - a failed baseline is a real result
        flat = {}
        succeeded = False

    elapsed = (time.perf_counter() - started) * 1000.0

    return MethodOutput(
        payload=flat,
        source_fields=frozenset(_leaf_names(flat)),
        output_bytes=serialized_size(flat),
        latency_ms=elapsed,
        succeeded=succeeded,
    )


def run_adaptive(
    case: EvaluationCase,
    service: ResponseAdaptationService,
    options: AdaptationOptions | None = None,
) -> MethodOutput:
    """The proposed method."""
    started = time.perf_counter()
    result = service.adapt(case.envelope(), options)
    elapsed = (time.perf_counter() - started) * 1000.0

    report = result.report
    kept = frozenset(
        decision.source_field
        for decision in (report.field_decisions if report else ())
        if decision.selected
    )

    if report is not None and report.decisions_truncated:
        # The report was capped, so it is no longer a complete record of what
        # was selected. Scoring recall from a truncated report would understate
        # it, so the run refuses rather than reporting a number it cannot
        # stand behind.
        raise RuntimeError(
            f"case {case.case_id!r} produced a truncated decision report; "
            "raise max_reported_fields before measuring recall"
        )

    return MethodOutput(
        payload=result.llm_ready,
        source_fields=frozenset(_leaf_names({name: None for name in kept})),
        output_bytes=result.transformation.output_bytes,
        latency_ms=elapsed,
        succeeded=result.success,
    )


def _leaf_names(flat: Mapping[str, Any]) -> tuple[str, ...]:
    """The field names a method produced, as it spells them."""
    return tuple(flat)


def field_present(label: str, produced: frozenset[str]) -> bool:
    """Whether a labelled field appears in what a method produced.

    THE SAME MATCHER IS USED FOR ALL THREE METHODS, and it has to be, because
    they spell nested paths differently through no fault of their own. The RAW
    baseline never unwraps, so its path to a nested contact address is
    ``customer.contact.email``; the other two unwrap first and reach the same
    value at ``contact.email``. Requiring an exact string match would have
    scored RAW as missing fields it plainly contains - penalising a baseline
    for a difference the label never intended to test.

    A label therefore matches a produced name that equals it, or that ends with
    it on a path boundary. The check runs in that one direction only: a label
    naming a specific path is not satisfied by a bare leaf somewhere else in
    the document.
    """
    if label in produced:
        return True

    suffix = "." + label

    return any(name.endswith(suffix) for name in produced)


# ======================================================================
# Metrics
# ======================================================================


@dataclass
class MethodTotals:
    """Accumulated per-case results for one method."""

    relevant_kept: int = 0
    relevant_total: int = 0
    irrelevant_removed: int = 0
    irrelevant_total: int = 0
    input_fields: int = 0
    output_fields: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    successes: int = 0
    cases: int = 0
    perfect_recall_cases: int = 0
    latencies: list[float] = field(default_factory=list)

    def add(self, case: EvaluationCase, output: MethodOutput,
            input_fields: int, input_bytes: int) -> None:
        present = output.source_fields
        kept = sum(1 for name in case.relevant if field_present(name, present))

        self.relevant_kept += kept
        self.relevant_total += len(case.relevant)
        self.irrelevant_removed += sum(
            1 for name in case.irrelevant if not field_present(name, present)
        )
        self.irrelevant_total += len(case.irrelevant)
        self.input_fields += input_fields
        # LEAVES on both sides, for every method. Counting top-level keys would
        # credit the RAW baseline with a 70% "field reduction" for handing over
        # an untouched three-key envelope wrapping ten leaves - a method that
        # removes nothing must measure as removing nothing.
        self.output_fields += count_leaf_fields(output.payload) if output.payload else 0
        self.input_bytes += input_bytes
        self.output_bytes += output.output_bytes
        self.successes += int(output.succeeded)
        self.cases += 1
        self.perfect_recall_cases += int(kept == len(case.relevant))
        self.latencies.append(output.latency_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            # THE metric. A dropped relevant field cannot be recovered
            # downstream; a retained irrelevant one only costs context.
            "relevant_field_recall": _ratio(self.relevant_kept, self.relevant_total),
            "cases_with_perfect_recall": _ratio(
                self.perfect_recall_cases, self.cases
            ),
            "irrelevant_field_removal_rate": _ratio(
                self.irrelevant_removed, self.irrelevant_total
            ),
            "field_reduction_ratio": _reduction(self.input_fields,
                                                self.output_fields),
            "context_reduction_ratio": _reduction(self.input_bytes,
                                                  self.output_bytes),
            "adaptation_success_rate": _ratio(self.successes, self.cases),
            "latency_ms": {
                "median": _percentile(self.latencies, 50),
                "p95": _percentile(self.latencies, 95),
                "mean": round(statistics.fmean(self.latencies), 4)
                if self.latencies else 0.0,
            },
            "totals": {
                "relevant_fields_kept": self.relevant_kept,
                "relevant_fields_labelled": self.relevant_total,
                "irrelevant_fields_removed": self.irrelevant_removed,
                "irrelevant_fields_labelled": self.irrelevant_total,
                "input_fields": self.input_fields,
                "output_fields": self.output_fields,
                "input_bytes": self.input_bytes,
                "output_bytes": self.output_bytes,
            },
        }


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 6) if whole else 0.0


def _reduction(before: int, after: int) -> float:
    return round(1.0 - (after / before), 6) if before else 0.0


def _percentile(values: Sequence[float], percentile: int) -> float:
    """A percentile without numpy, using nearest-rank.

    Nearest-rank rather than interpolation: with sixty-odd samples the
    interpolated value invents a latency that was never observed, and a
    reported p95 should be a measurement.
    """
    if not values:
        return 0.0

    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(percentile / 100 * len(ordered))))

    return round(ordered[rank - 1], 4)


def evaluate(
    cases: Sequence[EvaluationCase] | None = None,
    service: ResponseAdaptationService | None = None,
    options: AdaptationOptions | None = None,
) -> dict[str, Any]:
    """Run all three methods over every case and return the comparison."""
    cases = cases or build_cases()
    service = service or ResponseAdaptationService()
    options = options or service.options

    totals = {name: MethodTotals() for name in METHODS}
    by_entity: dict[str, dict[str, MethodTotals]] = {}
    per_case: list[dict[str, Any]] = []

    for case in cases:
        input_fields = count_leaf_fields(case.body)
        input_bytes = serialized_size(case.body)

        outputs = {
            METHOD_RAW: run_raw(case),
            METHOD_GENERIC: run_generic(case),
            METHOD_ADAPTIVE: run_adaptive(case, service, options),
        }

        bucket = by_entity.setdefault(
            case.entity, {name: MethodTotals() for name in METHODS}
        )

        for name, output in outputs.items():
            totals[name].add(case, output, input_fields, input_bytes)
            bucket[name].add(case, output, input_fields, input_bytes)

        adaptive = outputs[METHOD_ADAPTIVE]
        missed = [
            name
            for name in case.relevant
            if not field_present(name, adaptive.source_fields)
        ]

        per_case.append(
            {
                "case_id": case.case_id,
                "entity": case.entity,
                "query": case.query,
                "input_fields": input_fields,
                "relevant_labelled": list(case.relevant),
                "irrelevant_labelled": list(case.irrelevant),
                "adaptive_output_fields": sorted(adaptive.payload),
                # Named explicitly. A per-case list of failures is the first
                # thing a reader should be able to check, and burying them in
                # an aggregate would be the easiest place to hide a weak result.
                "adaptive_missed_relevant": missed,
                "note": case.note,
            }
        )

    return {
        "methods": {name: total.to_dict() for name, total in totals.items()},
        "per_entity": {
            entity: {
                name: total.to_dict() for name, total in by_entity[entity].items()
            }
            for entity in sorted(by_entity)
        },
        "per_case": per_case,
    }


def run_ablation(
    cases: Sequence[EvaluationCase] | None = None,
    service: ResponseAdaptationService | None = None,
) -> dict[str, Any]:
    """The single ablation: the proposed method with and without query relevance.

    Isolates the ONE mechanism this phase contributes. Everything else -
    unwrapping, canonical mapping, budgets - is identical between the two arms,
    so the difference is attributable.
    """
    cases = cases or build_cases()
    service = service or ResponseAdaptationService()

    with_relevance = MethodTotals()
    without_relevance = MethodTotals()
    off = replace(service.options, enable_relevance_selection=False)

    for case in cases:
        input_fields = count_leaf_fields(case.body)
        input_bytes = serialized_size(case.body)

        with_relevance.add(
            case, run_adaptive(case, service, service.options),
            input_fields, input_bytes,
        )
        without_relevance.add(
            case, run_adaptive(case, service, off), input_fields, input_bytes
        )

    return {
        "with_query_relevance": with_relevance.to_dict(),
        "without_query_relevance": without_relevance.to_dict(),
    }


__all__ = [
    "METHOD_RAW",
    "METHOD_GENERIC",
    "METHOD_ADAPTIVE",
    "METHODS",
    "EvaluationCase",
    "MethodOutput",
    "MethodTotals",
    "build_cases",
    "run_raw",
    "run_generic",
    "run_adaptive",
    "evaluate",
    "run_ablation",
    "field_present",
]
