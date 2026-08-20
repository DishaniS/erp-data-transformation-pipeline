"""Deterministic synthetic FinanceERP schema fixture.

Generates a non-trivial PostgreSQL-shaped ``SourceSchema`` with exactly:

    25 entities
    184 fields
    31 relationships

programmatically - none of the 184 fields is written out by hand. The
generator is pure Python with no randomness, so calling it twice with the
same arguments produces byte-identical structures (and therefore an identical
``compute_schema_hash()``), which is exactly the property the Phase 2
idempotency tests rely on.

This is a research/demonstration fixture proving the catalog can persist and
losslessly reconstruct a schema of realistic size. It does not claim that
actual PostgreSQL discovery exists - Phase 2 explicitly does not implement
that.
"""

from __future__ import annotations

from erp_pipeline.schemas import (
    EntityKind,
    FieldDataType,
    RelationshipType,
    SchemaOrigin,
    SourceEntity,
    SourceField,
    SourceRelationship,
    SourceSchema,
    SourceSystem,
    SourceType,
)

FINANCE_ERP_SOURCE_SYSTEM_ID = "finance_erp_pg"
FINANCE_ERP_SCHEMA_NAME = "public"

# 25 entity names. Order matters: it drives both the field-count distribution
# and the entity_position persisted by the catalog.
_ENTITY_NAMES: tuple[str, ...] = (
    "invoice",
    "invoice_line",
    "customer",
    "vendor",
    "payment",
    "payment_line",
    "purchase_order",
    "purchase_order_line",
    "goods_receipt",
    "goods_receipt_line",
    "cost_center",
    "gl_account",
    "gl_journal",
    "gl_journal_line",
    "budget",
    "budget_line",
    "tax_code",
    "currency",
    "exchange_rate",
    "bank_account",
    "bank_transaction",
    "approval_workflow",
    "approval_step",
    "employee",
    "department",
)

# 31 conceptual foreign-key edges (from_entity -> to_entity). Expressed as
# data, not as 31 hand-written SourceRelationship objects.
_RELATIONSHIP_EDGES: tuple[tuple[str, str], ...] = (
    ("invoice_line", "invoice"),
    ("invoice", "customer"),
    ("invoice", "currency"),
    ("invoice", "cost_center"),
    ("invoice", "tax_code"),
    ("payment", "invoice"),
    ("payment_line", "payment"),
    ("payment", "bank_account"),
    ("purchase_order", "vendor"),
    ("purchase_order", "currency"),
    ("purchase_order", "cost_center"),
    ("purchase_order_line", "purchase_order"),
    ("goods_receipt", "purchase_order"),
    ("goods_receipt_line", "goods_receipt"),
    ("gl_journal", "gl_account"),
    ("gl_journal_line", "gl_journal"),
    ("gl_journal_line", "gl_account"),
    ("budget", "cost_center"),
    ("budget_line", "budget"),
    ("budget_line", "gl_account"),
    ("exchange_rate", "currency"),
    ("bank_transaction", "bank_account"),
    ("bank_transaction", "currency"),
    ("approval_step", "approval_workflow"),
    ("approval_workflow", "employee"),
    ("approval_step", "employee"),
    ("employee", "department"),
    ("vendor", "currency"),
    ("customer", "currency"),
    ("invoice", "approval_workflow"),
    ("purchase_order", "approval_workflow"),
)

# Pool of plausible ERP attribute names, cycled deterministically per entity.
_ATTRIBUTE_POOL: tuple[str, ...] = (
    "amount",
    "currency_code",
    "status",
    "description",
    "quantity",
    "unit_price",
    "tax_amount",
    "discount_amount",
    "reference_number",
    "notes",
    "is_active",
    "priority",
    "category",
    "region",
    "owner_id",
    "approved_by",
    "approved_at",
    "due_date",
    "effective_date",
    "expiry_date",
    "external_reference",
    "version",
    "sequence_number",
    "remarks",
    "total_amount",
    "net_amount",
    "gross_amount",
    "exchange_rate_value",
    "payment_terms",
    "credit_limit",
    "balance",
    "budget_amount",
    "actual_amount",
    "variance_amount",
    "fiscal_year",
    "period",
    "gl_code",
    "cost_type",
    "location",
    "contact_email",
)

EXPECTED_ENTITY_COUNT = 25
EXPECTED_FIELD_COUNT = 184
EXPECTED_RELATIONSHIP_COUNT = 31

assert len(_ENTITY_NAMES) == EXPECTED_ENTITY_COUNT
assert len(_RELATIONSHIP_EDGES) == EXPECTED_RELATIONSHIP_COUNT
assert len(set(_RELATIONSHIP_EDGES)) == EXPECTED_RELATIONSHIP_COUNT, "duplicate edge"
assert len(set(_ENTITY_NAMES)) == EXPECTED_ENTITY_COUNT, "duplicate entity name"


def _infer_field_type(attribute_name: str) -> tuple[FieldDataType, str]:
    """Deterministically infer (normalized_type, vendor_type) from a name."""
    if attribute_name.endswith("_amount") or attribute_name in {"balance", "credit_limit"}:
        return FieldDataType.DECIMAL, "NUMERIC(14,2)"
    if attribute_name.endswith("_at"):
        return FieldDataType.DATETIME, "TIMESTAMPTZ"
    if attribute_name.endswith("_date"):
        return FieldDataType.DATE, "DATE"
    if attribute_name.startswith("is_"):
        return FieldDataType.BOOLEAN, "BOOLEAN"
    if attribute_name in {"quantity", "version", "sequence_number", "fiscal_year", "period"}:
        return FieldDataType.INTEGER, "INTEGER"
    if attribute_name == "exchange_rate_value":
        return FieldDataType.DECIMAL, "NUMERIC(18,8)"
    if attribute_name.endswith("_id"):
        return FieldDataType.STRING, "VARCHAR(40)"
    return FieldDataType.STRING, "VARCHAR(200)"


def _extra_field_count(entity_index: int) -> int:
    """9 entities get 4 extra fields (8 total), 16 entities get 3 (7 total).

    9*8 + 16*7 = 72 + 112 = 184, matching EXPECTED_FIELD_COUNT exactly with
    the 4-field baseline every entity carries.
    """
    return 4 if entity_index < 9 else 3


def _build_entity(entity_index: int, entity_name: str) -> SourceEntity:
    baseline_fields = (
        SourceField(
            source_name="id",
            normalized_name="id",
            source_data_type="UUID",
            normalized_data_type=FieldDataType.STRING,
            is_primary_key=True,
            nullable=False,
            is_unique=True,
            ordinal=0,
        ),
        SourceField(
            source_name=f"{entity_name}_code",
            normalized_name=f"{entity_name}_code",
            source_data_type="VARCHAR(50)",
            normalized_data_type=FieldDataType.STRING,
            nullable=False,
            is_unique=True,
            ordinal=1,
        ),
        SourceField(
            source_name="created_at",
            normalized_name="created_at",
            source_data_type="TIMESTAMPTZ",
            normalized_data_type=FieldDataType.DATETIME,
            nullable=False,
            ordinal=2,
        ),
        SourceField(
            source_name="updated_at",
            normalized_name="updated_at",
            source_data_type="TIMESTAMPTZ",
            normalized_data_type=FieldDataType.DATETIME,
            nullable=False,
            ordinal=3,
        ),
    )

    extra_count = _extra_field_count(entity_index)
    pool_size = len(_ATTRIBUTE_POOL)
    offset = (entity_index * 7) % pool_size
    baseline_names = {"id", f"{entity_name}_code", "created_at", "updated_at"}

    # Walk the pool starting at a per-entity offset, skipping any name that
    # collides with this entity's own baseline fields (e.g. the `currency`
    # entity's baseline `currency_code` also appears in the attribute pool).
    # The walk is still fully deterministic: same entity_index always visits
    # the pool in the same order and picks the same extra_count names.
    selected_names: list[str] = []
    step = 0
    while len(selected_names) < extra_count:
        candidate = _ATTRIBUTE_POOL[(offset + step) % pool_size]
        step += 1
        if candidate in baseline_names or candidate in selected_names:
            continue
        selected_names.append(candidate)

    extra_fields = tuple(
        SourceField(
            source_name=name,
            normalized_name=name,
            source_data_type=_infer_field_type(name)[1],
            normalized_data_type=_infer_field_type(name)[0],
            nullable=True,
            ordinal=4 + position,
        )
        for position, name in enumerate(selected_names)
    )

    return SourceEntity(
        entity_id=f"finance_erp_pg_{entity_name}",
        source_name=entity_name,
        normalized_name=entity_name,
        entity_kind=EntityKind.TABLE,
        namespace="public",
        primary_key_fields=("id",),
        fields=baseline_fields + extra_fields,
        description=f"Synthetic FinanceERP entity: {entity_name}",
    )


def build_finance_erp_schema(
    schema_id: str = "finance_erp_pg_public_v1",
    source_system_id: str = FINANCE_ERP_SOURCE_SYSTEM_ID,
    schema_version: str = "1",
) -> SourceSchema:
    """Build the deterministic synthetic FinanceERP schema.

    Calling this twice with the same arguments produces two ``SourceSchema``
    objects that are structurally identical (same ``compute_schema_hash()``)
    even though each carries its own fresh ``created_at`` timestamp - exactly
    mirroring what two independent discovery runs against an unchanged real
    source would produce.
    """
    entities = tuple(
        _build_entity(index, name) for index, name in enumerate(_ENTITY_NAMES)
    )

    relationships = tuple(
        SourceRelationship(
            relationship_id=f"rel_{from_entity}_to_{to_entity}",
            relationship_type=RelationshipType.FOREIGN_KEY,
            from_entity=from_entity,
            to_entity=to_entity,
            from_fields=(f"{to_entity}_id",),
            to_fields=("id",),
            confidence=1.0,
        )
        for from_entity, to_entity in _RELATIONSHIP_EDGES
    )

    field_count = sum(len(entity.fields) for entity in entities)
    assert field_count == EXPECTED_FIELD_COUNT, (
        f"fixture generator produced {field_count} fields, expected "
        f"{EXPECTED_FIELD_COUNT}"
    )

    return SourceSchema(
        schema_id=schema_id,
        source_system_id=source_system_id,
        schema_name=FINANCE_ERP_SCHEMA_NAME,
        origin=SchemaOrigin.DISCOVERED,
        schema_version=schema_version,
        entities=entities,
        relationships=relationships,
        metadata={"fixture": "synthetic_finance_erp", "generator_version": "1"},
    )


def build_finance_erp_source_system(
    source_system_id: str = FINANCE_ERP_SOURCE_SYSTEM_ID,
) -> SourceSystem:
    return SourceSystem(
        source_system_id=source_system_id,
        name="Finance ERP (synthetic research fixture)",
        source_type=SourceType.POSTGRESQL,
        environment="research",
        description=(
            "Deterministic synthetic PostgreSQL-shaped finance ERP schema used "
            "to demonstrate Phase 2 catalog persistence at non-trivial scale."
        ),
    )


def build_finance_erp_schema_v2(
    schema_id: str = "finance_erp_pg_public_v2",
    source_system_id: str = FINANCE_ERP_SOURCE_SYSTEM_ID,
) -> SourceSchema:
    """A controlled, structurally different revision of the V1 fixture.

    Applies exactly four changes relative to ``build_finance_erp_schema()``,
    on different entities so none of them could be mistaken for each other by
    the conservative rename heuristic:

    1. adds one new optional field to ``customer``
    2. removes one existing optional field from ``vendor``
    3. changes the normalized type of one field on ``payment`` (a breaking
       change, deliberately, to exercise breaking-change classification)
    4. adds one new relationship (``budget`` -> ``employee``)
    """
    base = build_finance_erp_schema(
        schema_id="finance_erp_pg_public_v2_base", source_system_id=source_system_id
    )

    entities = list(base.entities)
    entity_by_name = {entity.normalized_name: index for index, entity in enumerate(entities)}

    # 1. Add one optional field to `customer`.
    customer_index = entity_by_name["customer"]
    customer = entities[customer_index]
    entities[customer_index] = SourceEntity(
        entity_id=customer.entity_id,
        source_name=customer.source_name,
        normalized_name=customer.normalized_name,
        entity_kind=customer.entity_kind,
        namespace=customer.namespace,
        primary_key_fields=customer.primary_key_fields,
        description=customer.description,
        metadata=customer.metadata,
        fields=customer.fields
        + (
            SourceField(
                source_name="loyalty_tier",
                normalized_name="loyalty_tier",
                source_data_type="VARCHAR(20)",
                normalized_data_type=FieldDataType.STRING,
                nullable=True,
                ordinal=len(customer.fields),
            ),
        ),
    )

    # 2. Remove one optional (non-baseline) field from `vendor`.
    vendor_index = entity_by_name["vendor"]
    vendor = entities[vendor_index]
    removable = next(
        f for f in vendor.fields if f.normalized_name not in {"id", "vendor_code", "created_at", "updated_at"}
    )
    entities[vendor_index] = SourceEntity(
        entity_id=vendor.entity_id,
        source_name=vendor.source_name,
        normalized_name=vendor.normalized_name,
        entity_kind=vendor.entity_kind,
        namespace=vendor.namespace,
        primary_key_fields=vendor.primary_key_fields,
        description=vendor.description,
        metadata=vendor.metadata,
        fields=tuple(f for f in vendor.fields if f.normalized_name != removable.normalized_name),
    )

    # 3. Change one field's normalized type on `payment` (breaking).
    payment_index = entity_by_name["payment"]
    payment = entities[payment_index]
    changed_fields = []
    changed_one = False
    for source_field in payment.fields:
        if not changed_one and source_field.normalized_name not in {
            "id",
            "payment_code",
            "created_at",
            "updated_at",
        }:
            changed_fields.append(
                SourceField(
                    source_name=source_field.source_name,
                    normalized_name=source_field.normalized_name,
                    source_data_type="INTEGER",
                    normalized_data_type=FieldDataType.INTEGER,
                    nullable=source_field.nullable,
                    required=source_field.required,
                    is_primary_key=source_field.is_primary_key,
                    is_unique=source_field.is_unique,
                    is_array=source_field.is_array,
                    nested_path=source_field.nested_path,
                    semantic_type=source_field.semantic_type,
                    description=source_field.description,
                    ordinal=source_field.ordinal,
                    metadata=source_field.metadata,
                )
            )
            changed_one = True
        else:
            changed_fields.append(source_field)
    entities[payment_index] = SourceEntity(
        entity_id=payment.entity_id,
        source_name=payment.source_name,
        normalized_name=payment.normalized_name,
        entity_kind=payment.entity_kind,
        namespace=payment.namespace,
        primary_key_fields=payment.primary_key_fields,
        description=payment.description,
        metadata=payment.metadata,
        fields=tuple(changed_fields),
    )

    # 4. Add one new relationship.
    relationships = base.relationships + (
        SourceRelationship(
            relationship_id="rel_budget_to_employee",
            relationship_type=RelationshipType.FOREIGN_KEY,
            from_entity="budget",
            to_entity="employee",
            from_fields=("employee_id",),
            to_fields=("id",),
            confidence=1.0,
        ),
    )

    return SourceSchema(
        schema_id=schema_id,
        source_system_id=source_system_id,
        schema_name=FINANCE_ERP_SCHEMA_NAME,
        origin=SchemaOrigin.DISCOVERED,
        schema_version="2",
        entities=tuple(entities),
        relationships=relationships,
        metadata={"fixture": "synthetic_finance_erp", "generator_version": "2"},
    )


__all__ = [
    "FINANCE_ERP_SOURCE_SYSTEM_ID",
    "FINANCE_ERP_SCHEMA_NAME",
    "EXPECTED_ENTITY_COUNT",
    "EXPECTED_FIELD_COUNT",
    "EXPECTED_RELATIONSHIP_COUNT",
    "build_finance_erp_schema",
    "build_finance_erp_schema_v2",
    "build_finance_erp_source_system",
]
