"""The canonical target model the mapping engine maps toward.

WHY THIS MODULE HAD TO BE CREATED
---------------------------------
A mapping engine needs a deterministic target vocabulary: a list of canonical
fields, each with a type and a name, that a source field can be matched
against. The repository did not have one, and that absence was deliberate
rather than an oversight. ``docs/canonical_erp_model.md`` says so explicitly:

    "Which keys belong in normalized_data for a given entity type is decided
     by a mapping profile, not by this contract."

``CanonicalRecord.entity_type`` is an open normalized string and
``normalized_data`` is an open JSON object, precisely so that a new ERP domain
object needs no code change. That openness is correct for the CONTRACT layer -
and it means the mapping engine has nothing to aim at until something declares
the target vocabulary.

So Phase 8 declares the smallest such vocabulary it can, and refuses to invent
a large speculative ERP ontology.

GROUNDED VERSUS EXTENDED
------------------------
Every field records where its name came from, and this is machine-checked by
``tests/erp_pipeline/mapping/test_canonical_model.py`` rather than merely
claimed here:

``FieldProvenance.REPOSITORY``
    The name is ALREADY used as a canonical field in this repository - in
    ``docs/canonical_erp_model.md`` section 4 and in the Phase 1 cross-source
    demonstration (``tests/erp_pipeline/test_cross_source_canonicalization.py``,
    whose ``EXPECTED_CANONICAL_DATA`` is the authoritative example of a
    canonical invoice). These names are reused verbatim; not one was renamed.

``FieldProvenance.PHASE_8_EXTENSION``
    Added by Phase 8 because the mapping engine needed a target that the
    repository had not yet exercised. Each one carries a ``reason``. These are
    additions, and are labelled as such so nobody later mistakes them for
    established vocabulary.

CONFIGURABLE, NOT HARD-CODED
----------------------------
``DEFAULT_CANONICAL_MODEL`` is a default, not a law. A model can be built from
a plain dictionary (``CanonicalTargetModel.from_dict``), so a research run can
supply its own vocabulary without editing this file - and its ``model_id`` and
``version`` travel into every mapping profile so a stored mapping always says
which target model it was generated against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Mapping, Sequence

from erp_pipeline.schemas.enums import FieldDataType
from erp_pipeline.schemas.identity import normalize_identifier

#: Version of the DEFAULT vocabulary below. Bumped whenever a canonical field
#: is added, removed or retyped, so a stored mapping profile can state which
#: target model produced it (Step 37).
DEFAULT_MODEL_ID = "erp_core"
DEFAULT_MODEL_VERSION = "1.0"


class FieldProvenance(str, Enum):
    """Where a canonical field name came from.

    The whole point of this enum is to stop Phase 8 quietly passing invented
    vocabulary off as established. A reader (or a test) can ask any field
    whether the repository already used it.
    """

    #: Already used as a canonical field name in this repository.
    REPOSITORY = "repository"
    #: Introduced by Phase 8, with a stated reason.
    PHASE_8_EXTENSION = "phase_8_extension"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class CanonicalField:
    """One field of one canonical entity.

    ``aliases`` are EXPLICIT. The engine never guesses that ``cust_no`` means
    ``customer_id``; it knows because this registry says so, and the match is
    reported as ``explicit_alias`` evidence. Keeping them here rather than
    inside the scorer is what makes them reviewable and versionable (Step 8).
    """

    entity_type: str
    name: str
    data_type: FieldDataType
    required: bool = False
    #: True for the field that identifies the record within its entity.
    is_identifier: bool = False
    aliases: tuple[str, ...] = ()
    description: str | None = None
    provenance: FieldProvenance = FieldProvenance.PHASE_8_EXTENSION
    #: Required when provenance is PHASE_8_EXTENSION - why the addition was
    #: necessary. Enforced in __post_init__.
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.provenance is FieldProvenance.PHASE_8_EXTENSION and not self.reason:
            raise ValueError(
                f"Canonical field {self.qualified_name!r} is a Phase 8 "
                "extension and must state why it was added."
            )

    @property
    def qualified_name(self) -> str:
        """``invoice.amount`` - used in candidates and evidence.

        The persisted ``FieldMapping.target_field`` uses the BARE name
        (``amount``), because ``MappingProfile.target_entity_type`` already
        scopes it and ``normalized_data`` keys in the canonical model are bare.
        The qualified form exists for explainability, where a reader needs to
        see which entity a candidate belonged to.
        """
        return f"{self.entity_type}.{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "data_type": self.data_type.value,
            "required": self.required,
            "is_identifier": self.is_identifier,
            "aliases": list(self.aliases),
            "description": self.description,
            "provenance": self.provenance.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CanonicalEntity:
    """One canonical business object - ``invoice``, ``customer``.

    ``entity_type`` matches ``CanonicalRecord.entity_type`` exactly, and is a
    normalized identifier for the same reason that contract requires one.
    """

    entity_type: str
    fields: tuple[CanonicalField, ...]
    aliases: tuple[str, ...] = ()
    description: str | None = None
    provenance: FieldProvenance = FieldProvenance.PHASE_8_EXTENSION
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.provenance is FieldProvenance.PHASE_8_EXTENSION and not self.reason:
            raise ValueError(
                f"Canonical entity {self.entity_type!r} is a Phase 8 extension "
                "and must state why it was added."
            )

        duplicates = {
            name
            for name in (f.name for f in self.fields)
            if [f.name for f in self.fields].count(name) > 1
        }
        if duplicates:
            raise ValueError(
                f"Canonical entity {self.entity_type!r} declares field(s) "
                f"{sorted(duplicates)} more than once."
            )

        for canonical_field in self.fields:
            if canonical_field.entity_type != self.entity_type:
                raise ValueError(
                    f"Canonical field {canonical_field.name!r} declares entity "
                    f"{canonical_field.entity_type!r} but is listed under "
                    f"{self.entity_type!r}."
                )

    @property
    def required_fields(self) -> tuple[CanonicalField, ...]:
        return tuple(item for item in self.fields if item.required)

    @property
    def identifier_field(self) -> CanonicalField | None:
        for item in self.fields:
            if item.is_identifier:
                return item
        return None

    def field_by_name(self, name: str) -> CanonicalField | None:
        for item in self.fields:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "aliases": list(self.aliases),
            "description": self.description,
            "provenance": self.provenance.value,
            "reason": self.reason,
            "fields": [item.to_dict() for item in self.fields],
        }


@dataclass(frozen=True)
class CanonicalTargetModel:
    """A versioned registry of canonical entities and fields.

    Identity (``model_id`` + ``version``) is recorded on every mapping profile
    the engine produces, so a stored mapping never silently claims to have been
    generated against a vocabulary it never saw (Step 37).
    """

    model_id: str
    version: str
    entities: tuple[CanonicalEntity, ...]
    description: str | None = None

    def __post_init__(self) -> None:
        names = [entity.entity_type for entity in self.entities]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(
                f"Canonical model {self.model_id!r} declares entity "
                f"{sorted(duplicates)} more than once."
            )

    @property
    def identity(self) -> str:
        """``erp_core@1.0`` - the value recorded on generated profiles."""
        return f"{self.model_id}@{self.version}"

    @property
    def entity_types(self) -> tuple[str, ...]:
        return tuple(entity.entity_type for entity in self.entities)

    def entity(self, entity_type: str) -> CanonicalEntity | None:
        for candidate in self.entities:
            if candidate.entity_type == entity_type:
                return candidate
        return None

    def field(self, entity_type: str, name: str) -> CanonicalField | None:
        entity = self.entity(entity_type)
        return entity.field_by_name(name) if entity else None

    def field_by_qualified_name(self, qualified: str) -> CanonicalField | None:
        if "." not in qualified:
            return None
        entity_type, name = qualified.split(".", 1)
        return self.field(entity_type, name)

    def iter_fields(self) -> Iterator[CanonicalField]:
        """Every field in the model, in a deterministic order.

        Entities and fields are yielded in declaration order, which is fixed in
        source, so candidate generation can never depend on dictionary
        iteration order.
        """
        for entity in self.entities:
            yield from entity.fields

    @property
    def extension_fields(self) -> tuple[CanonicalField, ...]:
        """Fields Phase 8 added, for the honesty report and its test."""
        return tuple(
            item
            for item in self.iter_fields()
            if item.provenance is FieldProvenance.PHASE_8_EXTENSION
        )

    @property
    def repository_fields(self) -> tuple[CanonicalField, ...]:
        """Fields whose names the repository already used."""
        return tuple(
            item
            for item in self.iter_fields()
            if item.provenance is FieldProvenance.REPOSITORY
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "identity": self.identity,
            "description": self.description,
            "entities": [entity.to_dict() for entity in self.entities],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalTargetModel":
        """Build a model from a plain dictionary.

        This is what makes the vocabulary configurable rather than hard-coded:
        a research run can declare its own canonical model in JSON/YAML and
        hand it to the engine without touching this module.
        """
        entities: list[CanonicalEntity] = []

        for entity_payload in payload.get("entities", ()):
            entity_type = normalize_identifier(entity_payload["entity_type"])
            fields = tuple(
                CanonicalField(
                    entity_type=entity_type,
                    name=normalize_identifier(field_payload["name"]),
                    data_type=FieldDataType.from_value(field_payload["data_type"]),
                    required=bool(field_payload.get("required", False)),
                    is_identifier=bool(field_payload.get("is_identifier", False)),
                    aliases=tuple(field_payload.get("aliases", ())),
                    description=field_payload.get("description"),
                    provenance=FieldProvenance(
                        field_payload.get("provenance", "phase_8_extension")
                    ),
                    reason=field_payload.get("reason"),
                )
                for field_payload in entity_payload.get("fields", ())
            )
            entities.append(
                CanonicalEntity(
                    entity_type=entity_type,
                    fields=fields,
                    aliases=tuple(entity_payload.get("aliases", ())),
                    description=entity_payload.get("description"),
                    provenance=FieldProvenance(
                        entity_payload.get("provenance", "phase_8_extension")
                    ),
                    reason=entity_payload.get("reason"),
                )
            )

        return cls(
            model_id=payload["model_id"],
            version=str(payload["version"]),
            entities=tuple(entities),
            description=payload.get("description"),
        )


# ============================================================
# The default vocabulary
# ============================================================
#
# GROUNDING. Every REPOSITORY-provenance name below appears verbatim in
# docs/canonical_erp_model.md section 4 and/or
# tests/erp_pipeline/test_cross_source_canonicalization.py
# (EXPECTED_CANONICAL_DATA = invoice_id, customer_id, amount, status;
#  the doc's worked example adds currency). entity_type "invoice" is used 19
# times across the repository and "purchase_order" once.
#
# Everything else is labelled PHASE_8_EXTENSION with a reason.

_REPO = FieldProvenance.REPOSITORY
_EXT = FieldProvenance.PHASE_8_EXTENSION

_INVOICE_FIELDS = (
    CanonicalField(
        entity_type="invoice", name="invoice_id", data_type=FieldDataType.STRING,
        required=True, is_identifier=True, provenance=_REPO,
        description="Business key of the invoice in the source system.",
        aliases=("invoice_no", "invoice_number", "inv_id", "inv_no",
                 "invoiceid", "invoicenumber", "invoice_ref", "invoice_reference",
                 "invoice", "bill_no", "bill_number"),
    ),
    CanonicalField(
        entity_type="invoice", name="customer_id", data_type=FieldDataType.STRING,
        required=True, provenance=_REPO,
        description="Identifier of the customer the invoice is issued to.",
        aliases=("customer_ref", "customer_no", "customer_number", "cust_id",
                 "cust_no", "customerid", "customercode", "customer_code",
                 "client_ref", "client_id", "buyer", "buyer_id"),
    ),
    CanonicalField(
        entity_type="invoice", name="amount", data_type=FieldDataType.DECIMAL,
        required=True, provenance=_REPO,
        description="Total monetary value of the invoice.",
        aliases=("total", "total_amount", "total_amt", "totalamount",
                 "invoice_value", "invoicevalue", "grand_total", "net_amount",
                 "financial.total", "amount_total"),
    ),
    CanonicalField(
        entity_type="invoice", name="currency", data_type=FieldDataType.STRING,
        provenance=_REPO,
        description="ISO currency code the amount is denominated in.",
        aliases=("currency_code", "currencycode", "curr", "iso_currency"),
    ),
    CanonicalField(
        entity_type="invoice", name="status", data_type=FieldDataType.STRING,
        provenance=_REPO,
        description="Workflow state of the invoice.",
        aliases=("approval_status", "approvalstatus", "approval", "state",
                 "invoice_status", "approved_flag", "approvedflag"),
    ),
    CanonicalField(
        entity_type="invoice", name="issued_on", data_type=FieldDataType.DATE,
        provenance=_EXT,
        reason=(
            "The repository's canonical invoice example carries no date field, "
            "but every source fixture in Phases 4-7 exposes an issue date, and "
            "a mapping engine with no temporal target could not demonstrate "
            "DATE/DATETIME compatibility at all."
        ),
        description="Date the invoice was issued.",
        aliases=("issue_date", "issued_at", "invoice_date", "issuedon",
                 "date_issued", "created_on", "document_date"),
    ),
)

_CUSTOMER_FIELDS = (
    CanonicalField(
        entity_type="customer", name="customer_id", data_type=FieldDataType.STRING,
        required=True, is_identifier=True, provenance=_REPO,
        description="Business key of the customer.",
        aliases=("customer_ref", "customer_no", "customer_number", "cust_id",
                 "cust_no", "customerid", "customercode", "customer_code",
                 "client_id", "client_ref"),
    ),
    CanonicalField(
        entity_type="customer", name="name", data_type=FieldDataType.STRING,
        required=True, provenance=_EXT,
        reason=(
            "A customer entity with no name has no usable required-coverage "
            "story, and Step 24 of the phase brief uses exactly customer_id + "
            "name as its required-coverage example."
        ),
        description="Display or legal name of the customer.",
        aliases=("customer_name", "display_name", "displayname", "legal_name",
                 "company_name", "full_name", "cust_name"),
    ),
    CanonicalField(
        entity_type="customer", name="email", data_type=FieldDataType.STRING,
        provenance=_EXT,
        reason=(
            "Required by the cross-source demonstration: five of the six "
            "source technologies expose an email field under a different name "
            "(email, email_addr, email_address, emailAddress, contact.email), "
            "and there was no canonical target for any of them."
        ),
        description="Primary contact email address of the customer.",
        aliases=("email_addr", "email_address", "emailaddress", "e_mail",
                 "contact_email", "contact.email", "mail"),
    ),
    CanonicalField(
        entity_type="customer", name="phone", data_type=FieldDataType.STRING,
        provenance=_EXT,
        reason=(
            "Added alongside email so the customer contact block has more than "
            "one optional target, which is what makes the ambiguity and "
            "unmapped-field behaviour testable rather than trivial."
        ),
        description="Primary contact telephone number.",
        aliases=("phone_number", "phonenumber", "telephone", "tel",
                 "contact_phone", "contact.phone", "mobile"),
    ),
)

_PURCHASE_ORDER_FIELDS = (
    CanonicalField(
        entity_type="purchase_order", name="purchase_order_id",
        data_type=FieldDataType.STRING, required=True, is_identifier=True,
        provenance=_EXT,
        reason=(
            "entity_type 'purchase_order' is already used in the repository, "
            "but no canonical field names were ever declared for it. An "
            "identifier is the minimum needed for the entity to be a usable "
            "mapping target."
        ),
        description="Business key of the purchase order.",
        aliases=("po_no", "po_number", "order_id", "order_no", "ponumber",
                 "purchaseorderid", "purchase_order_no"),
    ),
    CanonicalField(
        entity_type="purchase_order", name="supplier_id",
        data_type=FieldDataType.STRING, provenance=_EXT,
        reason="A purchase order without a counterparty target is not mappable.",
        description="Identifier of the supplier the order is placed with.",
        aliases=("supplier_no", "supplier_number", "vendor_id", "vendor_no",
                 "supplierid", "supplier_code", "supplier.code"),
    ),
    CanonicalField(
        entity_type="purchase_order", name="amount",
        data_type=FieldDataType.DECIMAL, provenance=_EXT,
        reason=(
            "Mirrors invoice.amount so that the SAME source token ('total', "
            "'amount') can legitimately match two different canonical "
            "entities - which is what makes the entity-context and ambiguity "
            "tests meaningful."
        ),
        description="Total monetary value of the purchase order.",
        aliases=("total", "total_amount", "total_amt", "order_total",
                 "order_value"),
    ),
    CanonicalField(
        entity_type="purchase_order", name="status",
        data_type=FieldDataType.STRING, provenance=_EXT,
        reason="Mirrors invoice.status for the same entity-context reason.",
        description="Workflow state of the purchase order.",
        aliases=("order_status", "state", "approval_status"),
    ),
)

DEFAULT_CANONICAL_MODEL = CanonicalTargetModel(
    model_id=DEFAULT_MODEL_ID,
    version=DEFAULT_MODEL_VERSION,
    description=(
        "The smallest canonical target vocabulary the Phase 8 mapping engine "
        "needs. Invoice field names are reused verbatim from the repository's "
        "existing canonical example; everything labelled phase_8_extension is "
        "an addition and says why."
    ),
    entities=(
        CanonicalEntity(
            entity_type="invoice",
            fields=_INVOICE_FIELDS,
            provenance=_REPO,
            description="A billing document issued to a customer.",
            aliases=("invoices", "fin_invoice", "invoice_header",
                     "invoiceheader", "tbl_invoice", "billing_document",
                     "inv"),
        ),
        CanonicalEntity(
            entity_type="customer",
            fields=_CUSTOMER_FIELDS,
            provenance=_EXT,
            reason=(
                "The repository uses entity_type 'invoice' and "
                "'purchase_order' but never declared a customer entity, and "
                "the cross-source demonstration required by the phase brief "
                "maps customer identity and contact details from all six "
                "source technologies."
            ),
            description="A party invoices are issued to.",
            aliases=("customers", "customer_master", "customermaster",
                     "tbl_customer", "client", "clients", "buyer", "account"),
        ),
        CanonicalEntity(
            entity_type="purchase_order",
            fields=_PURCHASE_ORDER_FIELDS,
            provenance=_REPO,
            description="An order placed with a supplier.",
            aliases=("purchase_orders", "purchaseorder", "purchaseorders",
                     "po", "pos", "orders", "order"),
        ),
    ),
)

#: The canonical invoice field names the repository already used, kept here as
#: a checkable constant. ``test_canonical_model.py`` asserts the model's
#: REPOSITORY-provenance invoice fields are exactly this set, so the grounding
#: claim cannot silently rot.
REPOSITORY_INVOICE_FIELDS: frozenset[str] = frozenset(
    {"invoice_id", "customer_id", "amount", "currency", "status"}
)


__all__ = [
    "DEFAULT_MODEL_ID",
    "DEFAULT_MODEL_VERSION",
    "FieldProvenance",
    "CanonicalField",
    "CanonicalEntity",
    "CanonicalTargetModel",
    "DEFAULT_CANONICAL_MODEL",
    "REPOSITORY_INVOICE_FIELDS",
]
