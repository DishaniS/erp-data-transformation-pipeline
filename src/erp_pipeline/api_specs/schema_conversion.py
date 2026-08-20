"""OpenAPI Schema Objects -> Phase 1 ``SourceField`` trees.

The structural heart of Phase 7, and pure: it walks an already-loaded schema
object and produces fields. It performs no I/O and never touches a network.

Path vocabulary
---------------
Nested paths use exactly the convention Phase 5 established for MongoDB, so
``items[].sku`` means the same thing whether it came from a document store or
from an API contract. ``ARRAY_ELEMENT_SEGMENT`` is imported from that module
rather than redefined, because two spellings of the same idea would be worse
than one shared import.

Composition, and what this module refuses to guess
--------------------------------------------------
``allOf`` is merged: the specification says an object satisfies ALL branches
simultaneously, so their properties genuinely coexist.

``oneOf``/``anyOf`` are NOT merged and NOT collapsed to one branch. The
specification says the payload matches one (or some) of several alternatives,
so flattening them together would describe a shape that never occurs, and
picking the first would silently discard the others. The field records that it
is a variant, names the alternatives, and stops.

Examples
--------
``example`` and ``examples`` are never read for type information and never
stored. An example in an ERP specification is routinely a real customer
record, and a declared ``type`` is authoritative anyway - so examples can only
add risk, not information.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Mapping, Sequence

from erp_pipeline.api_specs.models import ApiSpecOptions, ApiSpecWarning
from erp_pipeline.api_specs.references import ReferenceResolver, RefStatus
from erp_pipeline.api_specs.safety import truncate_description
from erp_pipeline.discovery.mongodb_inference import ARRAY_ELEMENT_SEGMENT, render_path
from erp_pipeline.schemas.enums import FieldDataType
from erp_pipeline.schemas.identity import IdentityError, hash_json_payload, normalize_identifier
from erp_pipeline.schemas.source_models import SourceField

#: ``type`` + ``format`` -> the common cross-source type. Format is checked
#: first so ``string``+``date-time`` beats plain ``string``.
_FORMAT_TYPES: Mapping[tuple[str, str], FieldDataType] = {
    ("string", "date"): FieldDataType.DATE,
    ("string", "date-time"): FieldDataType.DATETIME,
    ("string", "byte"): FieldDataType.BINARY,
    ("string", "binary"): FieldDataType.BINARY,
    ("string", "password"): FieldDataType.STRING,
    ("string", "uuid"): FieldDataType.STRING,
    ("string", "email"): FieldDataType.STRING,
    ("string", "uri"): FieldDataType.STRING,
    ("integer", "int32"): FieldDataType.INTEGER,
    ("integer", "int64"): FieldDataType.INTEGER,
    ("number", "float"): FieldDataType.DECIMAL,
    ("number", "double"): FieldDataType.DECIMAL,
}

#: Bare ``type`` -> the common cross-source type.
_BASE_TYPES: Mapping[str, FieldDataType] = {
    "string": FieldDataType.STRING,
    "integer": FieldDataType.INTEGER,
    "number": FieldDataType.DECIMAL,
    "boolean": FieldDataType.BOOLEAN,
    "object": FieldDataType.OBJECT,
    "array": FieldDataType.ARRAY,
    # OpenAPI 3.1 / JSON Schema allow an explicit null type.
    "null": FieldDataType.UNKNOWN,
}

_COMPOSITION_KEYS: tuple[str, ...] = ("allOf", "oneOf", "anyOf")


@dataclass
class ConversionContext:
    """Shared state for converting one schema into fields.

    Carries the budgets and the warning sink so the walker itself stays a
    plain recursive function rather than an object with a dozen attributes.
    """

    resolver: ReferenceResolver
    options: ApiSpecOptions
    warnings: list[ApiSpecWarning] = dataclass_field(default_factory=list)
    fields: list[SourceField] = dataclass_field(default_factory=list)
    used_names: dict[str, int] = dataclass_field(default_factory=dict)
    #: Declared schema names this conversion referred to, in encounter order.
    referenced_schemas: list[tuple[str, str]] = dataclass_field(default_factory=list)
    field_limit_reached: bool = False
    depth_limit_reached: bool = False

    def warn(self, category: str, message: str, pointer: str | None = None) -> None:
        self.warnings.append(
            ApiSpecWarning(category=category, message=message, pointer=pointer)
        )


@dataclass(frozen=True)
class ConvertedSchema:
    """The fields one schema produced, plus what happened while producing them."""

    fields: tuple[SourceField, ...]
    warnings: tuple[ApiSpecWarning, ...] = ()
    referenced_schemas: tuple[tuple[str, str], ...] = ()
    partial: bool = False
    root_type: FieldDataType = FieldDataType.OBJECT
    root_source_type: str | None = None


# ============================================================
# Type normalization (Step 12)
# ============================================================

def normalize_schema_type(schema: Mapping[str, Any]) -> tuple[FieldDataType, bool]:
    """Map a Schema Object's ``type``/``format`` onto ``FieldDataType``.

    Returns ``(type, nullable)``. Nullability arrives two different ways and
    both are honoured: Swagger 2 / OpenAPI 3.0 use ``nullable: true``, while
    OpenAPI 3.1 uses a type array such as ``["string", "null"]``.
    """
    declared = schema.get("type")
    nullable = bool(schema.get("nullable", False))

    if isinstance(declared, (list, tuple)):
        members = [str(item) for item in declared]
        if "null" in members:
            nullable = True
        remaining = [item for item in members if item != "null"]

        if len(remaining) == 1:
            declared = remaining[0]
        elif not remaining:
            return FieldDataType.UNKNOWN, True
        else:
            # A genuine multi-type union. No single common type is true of all
            # of them, so it stays honestly unknown.
            return FieldDataType.UNKNOWN, nullable

    if not isinstance(declared, str):
        # No declared type. An object with properties is still an object; a
        # composition is handled by the caller; anything else is unknown.
        if "properties" in schema:
            return FieldDataType.OBJECT, nullable
        if "items" in schema:
            return FieldDataType.ARRAY, nullable
        return FieldDataType.UNKNOWN, nullable

    declared_format = schema.get("format")
    if isinstance(declared_format, str):
        formatted = _FORMAT_TYPES.get((declared, declared_format))
        if formatted is not None:
            return formatted, nullable

    return _BASE_TYPES.get(declared, FieldDataType.UNKNOWN), nullable


def render_source_data_type(schema: Mapping[str, Any]) -> str | None:
    """Render the declared type verbatim, preserving vendor detail.

    ``string(date-time)``, ``integer(int64)``, ``array<object>``,
    ``oneOf<Customer|Company>``. Precision the specification declared is
    exactly what a later conversion phase needs and is unrecoverable once
    discarded.
    """
    for key in _COMPOSITION_KEYS:
        branches = schema.get(key)
        if isinstance(branches, Sequence) and not isinstance(branches, (str, bytes)):
            names = _branch_names(branches)
            return f"{key}<{'|'.join(names)}>" if names else key

    declared = schema.get("type")

    if isinstance(declared, (list, tuple)):
        members = sorted(str(item) for item in declared)
        return "|".join(members) if members else None

    if not isinstance(declared, str):
        if "properties" in schema:
            return "object"
        return None

    declared_format = schema.get("format")
    if isinstance(declared_format, str) and declared_format:
        return f"{declared}({declared_format})"

    return declared


def _branch_names(branches: Sequence[Any]) -> list[str]:
    """Readable names for composition branches, in declaration order.

    Order is preserved rather than sorted: ``oneOf`` branch order is part of
    how the specification's author described the alternatives.
    """
    names: list[str] = []

    for index, branch in enumerate(branches):
        if not isinstance(branch, Mapping):
            names.append(f"branch{index + 1}")
            continue

        pointer = branch.get("$ref")
        if isinstance(pointer, str):
            from erp_pipeline.api_specs.references import reference_target_name

            names.append(reference_target_name(pointer) or f"branch{index + 1}")
            continue

        declared = branch.get("type")
        names.append(str(declared) if isinstance(declared, str)
                     else f"branch{index + 1}")

    return names


# ============================================================
# Conversion
# ============================================================

def convert_schema_to_fields(
    schema: Mapping[str, Any],
    resolver: ReferenceResolver,
    options: ApiSpecOptions,
) -> ConvertedSchema:
    """Convert one Schema Object into a flat list of nested-path fields.

    The root itself does not become a field - it becomes the entity. Its
    properties, and their properties, become the fields.
    """
    context = ConversionContext(resolver=resolver, options=options)

    resolved_root, root_note = _resolve_if_reference(schema, context, "#")
    if root_note is not None:
        context.warn(*root_note)

    root_type, _ = normalize_schema_type(resolved_root)
    root_source_type = render_source_data_type(resolved_root)

    if root_type is FieldDataType.ARRAY:
        # A response whose root is an array of objects: describe the ELEMENT,
        # since that is where the fields live. The array-ness is recorded on
        # the entity, not lost.
        items = resolved_root.get("items")
        if isinstance(items, Mapping):
            item_ref = items.get("$ref") if isinstance(items.get("$ref"), str) else None
            resolved_items, item_note = _resolve_if_reference(
                items, context, "#/items"
            )
            if item_note is not None:
                context.warn(*item_note)

            if item_ref is not None:
                target = _target_name(item_ref)
                if target:
                    context.referenced_schemas.append((ARRAY_ELEMENT_SEGMENT, target))
                context.resolver.enter(item_ref)

            try:
                _walk_object(resolved_items, (), context, depth=1, pointer="#/items")
            finally:
                if item_ref is not None:
                    context.resolver.leave()
    else:
        _walk_object(resolved_root, (), context, depth=1, pointer="#")

    return ConvertedSchema(
        fields=tuple(context.fields),
        warnings=tuple(context.warnings),
        referenced_schemas=tuple(context.referenced_schemas),
        partial=context.field_limit_reached or context.depth_limit_reached,
        root_type=root_type,
        root_source_type=root_source_type,
    )


def _resolve_if_reference(
    schema: Mapping[str, Any], context: ConversionContext, pointer: str
) -> tuple[Mapping[str, Any], tuple[str, str, str] | None]:
    """Follow a ``$ref`` if the node is one. Never fetches anything remote."""
    ref = schema.get("$ref")

    if not isinstance(ref, str):
        return schema, None

    resolved = context.resolver.resolve(ref)

    if resolved.status is RefStatus.RESOLVED and isinstance(resolved.target, Mapping):
        return resolved.target, None

    note = {
        RefStatus.REMOTE_NOT_FETCHED: (
            "remote_reference_not_fetched",
            f"Reference {ref!r} points outside this document and was NOT "
            "fetched; Phase 7 performs no network access.",
            pointer,
        ),
        RefStatus.CIRCULAR: (
            "circular_reference",
            f"Reference {ref!r} closes a cycle; expansion stopped here.",
            pointer,
        ),
        RefStatus.DEPTH_EXCEEDED: (
            "reference_depth_exceeded",
            f"Reference {ref!r} exceeded max_reference_depth "
            f"({context.options.max_reference_depth}); expansion stopped.",
            pointer,
        ),
        RefStatus.NOT_FOUND: (
            "unresolved_reference",
            f"Reference {ref!r} names something this document does not "
            "contain.",
            pointer,
        ),
    }.get(resolved.status)

    return schema, note


def _walk_object(
    schema: Mapping[str, Any],
    prefix: tuple[str, ...],
    context: ConversionContext,
    depth: int,
    pointer: str,
) -> None:
    """Emit fields for every property of an object schema."""
    merged, required = _merge_composition(schema, context, depth, pointer)

    properties = merged.get("properties")

    if not isinstance(properties, Mapping):
        return

    # Sorted so the field order of a schema never depends on dictionary order
    # in the source document.
    for name in sorted(properties, key=str):
        child = properties[name]

        if not isinstance(child, Mapping):
            context.warn(
                "invalid_property",
                f"Property {str(name)!r} is not a schema object and was "
                "skipped.",
                f"{pointer}/properties/{name}",
            )
            continue

        _emit_field(
            name=str(name),
            schema=child,
            prefix=prefix,
            required=str(name) in required,
            context=context,
            depth=depth,
            pointer=f"{pointer}/properties/{name}",
        )


def _merge_composition(
    schema: Mapping[str, Any],
    context: ConversionContext,
    depth: int,
    pointer: str,
) -> tuple[Mapping[str, Any], set[str]]:
    """Merge ``allOf`` branches into one effective object schema.

    Only ``allOf`` is merged, and legitimately so: the specification asserts
    the payload satisfies every branch at once, so their properties really do
    coexist. ``oneOf``/``anyOf`` are left alone here and handled at field
    level, because merging alternatives would describe a shape that never
    occurs.

    Property conflicts between branches are resolved conservatively: the first
    declaration wins and the conflict is recorded, rather than one branch
    silently overwriting another.
    """
    all_of = schema.get("allOf")

    if not isinstance(all_of, Sequence) or isinstance(all_of, (str, bytes)):
        return schema, _required_names(schema)

    merged_properties: dict[str, Any] = {}
    merged_required: set[str] = set()
    conflicts: list[str] = []

    branches: list[Mapping[str, Any]] = []

    for index, branch in enumerate(all_of):
        if not isinstance(branch, Mapping):
            continue

        resolved, note = _resolve_if_reference(branch, context, f"{pointer}/allOf/{index}")
        if note is not None:
            context.warn(*note)
            continue

        branches.append(resolved)

    # The node's own properties participate too - allOf commonly sits beside a
    # local `properties` block.
    branches.append(schema)

    for branch in branches:
        nested, nested_required = _merge_composition(
            branch, context, depth, pointer
        ) if branch is not schema and "allOf" in branch else (branch, _required_names(branch))

        properties = nested.get("properties")
        if isinstance(properties, Mapping):
            for name, definition in properties.items():
                key = str(name)
                if key in merged_properties and merged_properties[key] != definition:
                    conflicts.append(key)
                    continue
                merged_properties[key] = definition

        merged_required |= nested_required

    if conflicts:
        context.warn(
            "composition_conflict",
            f"allOf branches declare conflicting definitions for "
            f"{sorted(set(conflicts))}; the first declaration was kept.",
            pointer,
        )

    effective = dict(schema)
    effective.pop("allOf", None)
    effective["properties"] = merged_properties
    effective["required"] = sorted(merged_required)

    return effective, merged_required


def _required_names(schema: Mapping[str, Any]) -> set[str]:
    required = schema.get("required")

    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        return {str(name) for name in required}

    return set()


def _emit_field(
    name: str,
    schema: Mapping[str, Any],
    prefix: tuple[str, ...],
    required: bool,
    context: ConversionContext,
    depth: int,
    pointer: str,
) -> None:
    """Emit one field, then descend into it if it has structure."""
    if len(context.fields) >= context.options.max_fields_per_schema:
        context.field_limit_reached = True
        return

    segments = prefix + (name,)
    reference_pointer = schema.get("$ref") if isinstance(schema.get("$ref"), str) else None

    resolved, note = _resolve_if_reference(schema, context, pointer)
    if note is not None:
        context.warn(*note)

    data_type, nullable = normalize_schema_type(resolved)
    source_type = render_source_data_type(schema if reference_pointer is None else resolved)
    variants = _variant_names(resolved)

    if variants:
        # oneOf/anyOf: the alternatives are named, never merged or chosen
        # between. An object-shaped union stays OBJECT; anything else is
        # honestly unknown.
        data_type = _variant_type(resolved, context)
        source_type = render_source_data_type(resolved)

    if reference_pointer is not None:
        target = _target_name(reference_pointer)
        if target:
            context.referenced_schemas.append((render_path(segments), target))
            source_type = source_type or target

    context.fields.append(
        SourceField(
            source_name=name,
            normalized_name=_unique_name(segments, context),
            source_data_type=source_type,
            normalized_data_type=data_type,
            nullable=nullable or not required,
            required=required,
            # An API contract declares no database keys. A property called
            # "id" is not a primary key, and Phase 7 does not guess.
            is_primary_key=False,
            is_unique=False,
            is_array=data_type is FieldDataType.ARRAY,
            nested_path=prefix or None,
            # Phase 7 never infers business meaning. That is Phase 8.
            semantic_type=None,
            description=(
                truncate_description(
                    resolved.get("description"), context.options.max_description_length
                )
                if context.options.include_descriptions
                else None
            ),
            ordinal=len(context.fields),
            metadata=_field_metadata(
                resolved, reference_pointer, variants, context.options
            ),
        )
    )

    if depth >= context.options.max_nesting_depth:
        context.depth_limit_reached = True
        return

    if variants:
        # Deliberately not descended into: expanding every branch's properties
        # would assert that they coexist, which is the opposite of what
        # oneOf/anyOf mean.
        return

    _descend(
        resolved, segments, context, depth, pointer, reference_pointer
    )


def _descend(
    schema: Mapping[str, Any],
    segments: tuple[str, ...],
    context: ConversionContext,
    depth: int,
    pointer: str,
    reference_pointer: str | None,
) -> None:
    """Recurse into an object's properties or an array's items."""
    data_type, _ = normalize_schema_type(schema)

    if reference_pointer is not None:
        context.resolver.enter(reference_pointer)

    try:
        if data_type is FieldDataType.OBJECT or "properties" in schema or "allOf" in schema:
            _walk_object(schema, segments, context, depth + 1, pointer)
            return

        if data_type is FieldDataType.ARRAY:
            items = schema.get("items")
            if not isinstance(items, Mapping):
                return

            item_pointer = items.get("$ref") if isinstance(items.get("$ref"), str) else None
            resolved_items, note = _resolve_if_reference(items, context, f"{pointer}/items")
            if note is not None:
                context.warn(*note)

            if item_pointer is not None:
                target = _target_name(item_pointer)
                if target:
                    context.referenced_schemas.append(
                        (render_path(segments), target)
                    )
                context.resolver.enter(item_pointer)

            try:
                item_type, _ = normalize_schema_type(resolved_items)
                if item_type is FieldDataType.OBJECT or "properties" in resolved_items:
                    _walk_object(
                        resolved_items,
                        segments + (ARRAY_ELEMENT_SEGMENT,),
                        context,
                        depth + 1,
                        f"{pointer}/items",
                    )
            finally:
                if item_pointer is not None:
                    context.resolver.leave()
    finally:
        if reference_pointer is not None:
            context.resolver.leave()


def _variant_names(schema: Mapping[str, Any]) -> tuple[str, ...]:
    for key in ("oneOf", "anyOf"):
        branches = schema.get(key)
        if isinstance(branches, Sequence) and not isinstance(branches, (str, bytes)):
            return tuple(_branch_names(branches))
    return ()


def _variant_type(
    schema: Mapping[str, Any], context: ConversionContext
) -> FieldDataType:
    """A union's common type, when the branches genuinely share one."""
    for key in ("oneOf", "anyOf"):
        branches = schema.get(key)
        if not isinstance(branches, Sequence) or isinstance(branches, (str, bytes)):
            continue

        types: set[FieldDataType] = set()
        for branch in branches:
            if not isinstance(branch, Mapping):
                continue
            resolved, _ = _resolve_if_reference(branch, context, "#")
            branch_type, _ = normalize_schema_type(resolved)
            types.add(branch_type)

        # "string or null" is really a nullable string; two object variants
        # are still an object. Anything more diverse has no honest common type.
        types.discard(FieldDataType.UNKNOWN)
        if len(types) == 1:
            return next(iter(types))

        return FieldDataType.UNKNOWN

    return FieldDataType.UNKNOWN


def _target_name(pointer: str) -> str | None:
    from erp_pipeline.api_specs.references import reference_target_name

    return reference_target_name(pointer)


def _field_metadata(
    schema: Mapping[str, Any],
    reference_pointer: str | None,
    variants: Sequence[str],
    options: ApiSpecOptions,
) -> dict[str, Any]:
    """JSON-safe declared metadata for one field.

    Records what the specification DECLARED. Deliberately excludes
    ``example``/``examples``: an example in an ERP spec is routinely a real
    customer record, the declared type is authoritative anyway, and this
    metadata is published to a catalog that must stay free of business data.
    """
    metadata: dict[str, Any] = {"structure_origin": "declared"}

    declared_type = schema.get("type")
    if isinstance(declared_type, str):
        metadata["declared_type"] = declared_type
    elif isinstance(declared_type, (list, tuple)):
        metadata["declared_type"] = [str(item) for item in declared_type]

    declared_format = schema.get("format")
    if isinstance(declared_format, str) and declared_format:
        metadata["declared_format"] = declared_format

    if reference_pointer is not None:
        metadata["ref"] = reference_pointer
        target = _target_name(reference_pointer)
        if target:
            metadata["ref_target"] = target

    if variants:
        metadata["variant_of"] = list(variants)

    if options.include_enum_values:
        enum_values = schema.get("enum")
        if isinstance(enum_values, Sequence) and not isinstance(enum_values, (str, bytes)):
            # Enum members are declared CONSTRAINTS - part of the contract every
            # consumer must satisfy - not sampled business data. Bounded so a
            # generated spec with a 50 000-member enum cannot bloat the catalog.
            values = [_scalar(item) for item in enum_values][: options.max_enum_values]
            metadata["enum"] = values
            if len(enum_values) > options.max_enum_values:
                metadata["enum_truncated"] = True
                metadata["enum_total_count"] = len(enum_values)

    for flag in ("readOnly", "writeOnly", "deprecated"):
        if bool(schema.get(flag)):
            metadata[_snake(flag)] = True

    if "example" in schema or "examples" in schema:
        # Recorded as a FACT, never as a value.
        metadata["example_present"] = True

    return metadata


def _scalar(value: Any) -> Any:
    """Reduce an enum member to a JSON-safe scalar."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


def _unique_name(segments: tuple[str, ...], context: ConversionContext) -> str:
    """Normalize a field path to a name unique within its entity.

    Two distinct property paths can normalize to one name (``totalAmount`` and
    ``total_amount``), and Phase 1 requires uniqueness within an entity. Since
    properties are visited in sorted order the resolution is deterministic:
    the first claimant keeps the plain name.
    """
    path = render_path(segments)

    try:
        base = normalize_identifier(path)
    except IdentityError:
        base = f"field.{hash_json_payload(list(segments))[:12]}"

    used = context.used_names
    count = used.get(base, 0)
    used[base] = count + 1

    if count == 0:
        return base

    candidate = f"{base}.{count + 1}"
    while candidate in used:
        count += 1
        used[base] = count + 1
        candidate = f"{base}.{count + 1}"

    used[candidate] = 1
    return candidate


__all__ = [
    "ConversionContext",
    "ConvertedSchema",
    "convert_schema_to_fields",
    "normalize_schema_type",
    "render_source_data_type",
]
