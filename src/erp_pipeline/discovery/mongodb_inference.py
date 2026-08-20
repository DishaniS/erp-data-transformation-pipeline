"""Observed-structure inference over MongoDB documents.

This module is the pure half of Phase 5: it turns an iterable of already-read
documents into aggregate observations, and those observations into Phase 1
``SourceField`` objects. It performs no I/O, opens no connection and imports
neither ``pymongo`` nor ``bson``, which is what makes every inference rule in
here unit-testable with plain Python dicts. ``discovery.mongodb`` is the other
half: it obtains the documents.

What "observed" means
---------------------
An ordinary MongoDB collection declares no schema. Everything computed here
describes A BOUNDED SAMPLE of documents and nothing else:

    "``customer.name`` was a string in 64 of the 100 documents sampled"

not

    "``customer.name`` is an optional string column"

The distinction is not pedantry. A field absent from 500 sampled documents may
exist in the 500 001st, and no amount of sampling can turn an observation into
a constraint. Every requiredness decision below is therefore conservative:
this module claims a field is required only when the sample gives it no reason
to doubt it, and records the evidence alongside so a reader can disagree.

Privacy
-------
A value's TYPE is counted; the value itself is discarded immediately and is
never stored, logged, hashed or returned. Nothing in this module can emit a
document value - the accumulators only hold integers.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.discovery.models import FieldObservation, MongoInferenceOptions
from erp_pipeline.schemas.enums import FieldDataType
from erp_pipeline.schemas.identity import IdentityError, hash_json_payload, normalize_identifier
from erp_pipeline.schemas.source_models import SourceField

#: Path segment standing for "the elements of this array". Kept as its own
#: segment rather than glued to the parent name so ``items[].sku`` stays
#: mechanically decomposable into ``items`` -> elements -> ``sku``.
ARRAY_ELEMENT_SEGMENT = "[]"

#: The MongoDB ``_id`` field. Every document in a normal collection has one.
ID_FIELD = "_id"

#: BSON alias reported for a null value. Deliberately NOT a member of the
#: type distribution used to pick a normalized type - a null says nothing
#: about what type the field has when it is populated.
NULL_ALIAS = "null"

#: BSON alias reported for a value this module does not recognize.
UNKNOWN_ALIAS = "unknown"


# ============================================================
# BSON type observation (Step 8)
# ============================================================
#
# Aliases are MongoDB's own ``$type`` vocabulary ("objectId", "binData",
# "decimal", "long", ...) rather than Python class names, so a stored observed
# schema speaks the source technology's language exactly as
# ``SourceField.source_data_type`` requires.

#: Recognized by class NAME, so this module works whether or not ``bson`` is
#: installed and never imports it. Checked BEFORE the isinstance table because
#: several bson types subclass a builtin: ``Int64`` is an ``int``, ``Binary``
#: is ``bytes`` and ``Code`` is a ``str``, so an isinstance-first order would
#: silently lose the more precise BSON type.
_BSON_CLASS_ALIASES: Mapping[str, str] = {
    "ObjectId": "objectId",
    "Decimal128": "decimal",
    "Binary": "binData",
    "Int64": "long",
    "Regex": "regex",
    "Timestamp": "timestamp",
    "Code": "javascript",
    "MinKey": "minKey",
    "MaxKey": "maxKey",
    "UUID": "binData",
    "Pattern": "regex",
}

#: Widest 64-bit-safe range for a plain Python ``int``. MongoDB stores an
#: integer as a 32-bit ``int`` or a 64-bit ``long``, and a driver hands back a
#: plain ``int`` for both, so the magnitude is the only available signal.
_INT32_MIN = -(2 ** 31)
_INT32_MAX = 2 ** 31 - 1

#: BSON alias -> the coarse cross-source type a mapping layer reasons about.
#: Only EXISTING ``FieldDataType`` members are used; Phase 5 adds none.
BSON_ALIAS_TO_FIELD_TYPE: Mapping[str, FieldDataType] = {
    "string": FieldDataType.STRING,
    # An ObjectId is a 12-byte identifier with a canonical 24-character hex
    # form. STRING is how every consumer will actually handle it; the exact
    # BSON type survives verbatim in source_data_type.
    "objectId": FieldDataType.STRING,
    "regex": FieldDataType.STRING,
    "int": FieldDataType.INTEGER,
    "long": FieldDataType.INTEGER,
    "double": FieldDataType.DECIMAL,
    "decimal": FieldDataType.DECIMAL,
    "bool": FieldDataType.BOOLEAN,
    "date": FieldDataType.DATETIME,
    # A BSON Timestamp is an internal replication type, not a user date, but
    # it does denote a point in time.
    "timestamp": FieldDataType.DATETIME,
    "binData": FieldDataType.BINARY,
    "object": FieldDataType.OBJECT,
    "array": FieldDataType.ARRAY,
    # Honestly unknown rather than guessed.
    "javascript": FieldDataType.UNKNOWN,
    "minKey": FieldDataType.UNKNOWN,
    "maxKey": FieldDataType.UNKNOWN,
    UNKNOWN_ALIAS: FieldDataType.UNKNOWN,
}


def bson_type_alias(value: Any) -> str:
    """Report the BSON type alias of one value, without retaining the value."""
    class_alias = _BSON_CLASS_ALIASES.get(type(value).__name__)
    if class_alias is not None:
        return class_alias

    if value is None:
        return NULL_ALIAS

    # bool before int: bool is an int subclass in Python.
    if isinstance(value, bool):
        return "bool"

    if isinstance(value, int):
        return "int" if _INT32_MIN <= value <= _INT32_MAX else "long"

    if isinstance(value, float):
        return "double"

    if isinstance(value, Decimal):
        return "decimal"

    if isinstance(value, str):
        return "string"

    if isinstance(value, (bytes, bytearray, memoryview)):
        return "binData"

    # datetime before date: datetime is a date subclass.
    if isinstance(value, datetime):
        return "date"

    if isinstance(value, date):
        return "date"

    if isinstance(value, Mapping):
        return "object"

    if isinstance(value, (list, tuple)):
        return "array"

    return UNKNOWN_ALIAS


def normalize_bson_alias(alias: str) -> FieldDataType:
    """Map one BSON alias to the common ``FieldDataType``."""
    return BSON_ALIAS_TO_FIELD_TYPE.get(alias, FieldDataType.UNKNOWN)


def resolve_normalized_type(type_counts: Mapping[str, int]) -> FieldDataType:
    """Pick one ``FieldDataType`` for a field from its observed distribution.

    The policy is deterministic and deliberately pessimistic (Step 9):

    * nothing but nulls observed -> ``UNKNOWN``. A null reveals no type.
    * one type -> that type.
    * several types that all normalize the same way (``int`` + ``long``,
      ``date`` + ``timestamp``) -> that shared type. The distribution still
      records the difference.
    * ``INTEGER`` + ``DECIMAL`` -> ``DECIMAL``. Widening is lossless: every
      integer is representable as a decimal, so a consumer reading the field
      as decimal is right about every observed document.
    * anything else (``integer`` + ``string``, ``object`` + ``array``) ->
      ``UNKNOWN``. There is no type that is true of all observed values, and
      silently electing the majority would state something false about the
      minority.
    """
    observed = {
        alias: count
        for alias, count in type_counts.items()
        if alias != NULL_ALIAS and count > 0
    }

    if not observed:
        return FieldDataType.UNKNOWN

    normalized = {normalize_bson_alias(alias) for alias in observed}

    if len(normalized) == 1:
        return next(iter(normalized))

    if normalized == {FieldDataType.INTEGER, FieldDataType.DECIMAL}:
        return FieldDataType.DECIMAL

    return FieldDataType.UNKNOWN


def render_source_data_type(
    type_counts: Mapping[str, int],
    element_type_counts: Mapping[str, int] | None = None,
) -> str | None:
    """Render the observed BSON type(s) as ``SourceField.source_data_type``.

    Examples::

        {"string": 7}                    -> "string"
        {"objectId": 7}                  -> "objectId"
        {"int": 4, "double": 3}          -> "mixed<double|int>"
        {"array": 5} + {"object": 12}    -> "array<object>"
        {"array": 5} + {"int": 2, "string": 1} -> "array<mixed<int|string>>"
        {"array": 5} + {}                -> "array<empty>"

    Alias names are sorted, so the rendering depends only on WHICH types were
    seen - never on how many, nor on the order documents happened to arrive
    in. That matters because this string is part of the structural hash: a
    rendering that moved with the counts would mint a new catalog version
    every time the sample size changed.
    """
    observed = sorted(
        alias for alias, count in type_counts.items()
        if alias != NULL_ALIAS and count > 0
    )

    if not observed:
        return NULL_ALIAS if type_counts.get(NULL_ALIAS) else None

    if observed == ["array"]:
        return f"array<{_render_element_types(element_type_counts or {})}>"

    if len(observed) == 1:
        return observed[0]

    rendered = "|".join(observed)
    if "array" in observed:
        # A field that is sometimes an array and sometimes a scalar. Reporting
        # the element types too would suggest more regularity than exists.
        return f"mixed<{rendered}>"

    return f"mixed<{rendered}>"


def _render_element_types(element_type_counts: Mapping[str, int]) -> str:
    observed = sorted(
        alias for alias, count in element_type_counts.items() if count > 0
    )

    if not observed:
        return "empty"

    if observed == [NULL_ALIAS]:
        return NULL_ALIAS

    non_null = [alias for alias in observed if alias != NULL_ALIAS]

    if len(non_null) == 1:
        return non_null[0]

    return f"mixed<{'|'.join(non_null)}>"


# ============================================================
# Observation accumulation (Steps 6, 7, 10, 17, 18)
# ============================================================

@dataclass
class _PathAccumulator:
    """Mutable counters for one field path. Integers only - never a value."""

    segments: tuple[str, ...]
    present_count: int = 0
    null_count: int = 0
    value_count: int = 0
    type_counts: Counter = dataclass_field(default_factory=Counter)
    element_type_counts: Counter = dataclass_field(default_factory=Counter)
    truncated_due_to_depth: bool = False
    array_elements_truncated: bool = False


class DocumentStructureInference:
    """Accumulates the observed structure of a stream of documents.

    Usage::

        inference = DocumentStructureInference(options)
        for document in documents:
            inference.observe(document)
        observations = inference.observations()

    Determinism (Step 15). Two properties make the result independent of the
    order documents and keys arrive in:

    * keys within a document are visited in sorted order, so which paths get
      dropped when ``max_fields_per_collection`` is hit does not depend on
      MongoDB's document key order;
    * ``observations()`` sorts by path segments, so field ordering in the
      resulting entity is fixed.
    """

    def __init__(self, options: MongoInferenceOptions | None = None) -> None:
        self._options = options or MongoInferenceOptions()
        self._paths: dict[tuple[str, ...], _PathAccumulator] = {}
        self._documents_sampled = 0
        self._field_limit_reached = False
        self._depth_limit_reached = False
        self._dropped_path_count = 0

    # ---- state ----

    @property
    def documents_sampled(self) -> int:
        return self._documents_sampled

    @property
    def field_limit_reached(self) -> bool:
        """True when ``max_fields_per_collection`` stopped new paths being
        recorded. The result is then explicitly partial, never silently so."""
        return self._field_limit_reached

    @property
    def depth_limit_reached(self) -> bool:
        return self._depth_limit_reached

    @property
    def dropped_path_count(self) -> int:
        return self._dropped_path_count

    @property
    def partial(self) -> bool:
        return self._field_limit_reached or self._depth_limit_reached

    # ---- accumulation ----

    def observe(self, document: Any) -> None:
        """Record the structure of one document."""
        if not isinstance(document, Mapping):
            # A non-document (a driver returning a scalar from an unusual
            # cursor) is counted but contributes no structure, rather than
            # crashing an otherwise good inference run.
            self._documents_sampled += 1
            return

        self._documents_sampled += 1
        seen: set[tuple[str, ...]] = set()
        self._walk(document, (), seen)

        for segments in seen:
            self._paths[segments].present_count += 1

    def observe_all(self, documents: Iterable[Any]) -> None:
        for document in documents:
            self.observe(document)

    def _walk(
        self,
        mapping: Mapping[Any, Any],
        prefix: tuple[str, ...],
        seen: set[tuple[str, ...]],
    ) -> None:
        for key in sorted(mapping, key=str):
            segments = prefix + (str(key),)
            accumulator = self._accumulator(segments)

            if accumulator is None:
                continue

            value = mapping[key]
            alias = bson_type_alias(value)

            seen.add(segments)
            accumulator.value_count += 1

            if alias == NULL_ALIAS:
                accumulator.null_count += 1
                accumulator.type_counts[NULL_ALIAS] += 1
                continue

            accumulator.type_counts[alias] += 1

            if alias == "object":
                self._descend_object(value, segments, seen, accumulator)
            elif alias == "array":
                self._descend_array(value, segments, seen, accumulator)

    def _descend_object(
        self,
        value: Mapping[Any, Any],
        segments: tuple[str, ...],
        seen: set[tuple[str, ...]],
        accumulator: _PathAccumulator,
    ) -> None:
        if not self._may_descend(segments):
            accumulator.truncated_due_to_depth = True
            self._depth_limit_reached = True
            return

        self._walk(value, segments, seen)

    def _descend_array(
        self,
        value: Sequence[Any],
        segments: tuple[str, ...],
        seen: set[tuple[str, ...]],
        accumulator: _PathAccumulator,
    ) -> None:
        limit = self._options.max_array_elements_per_document

        elements = list(value)[:limit]
        if len(value) > limit:
            accumulator.array_elements_truncated = True

        element_prefix = segments + (ARRAY_ELEMENT_SEGMENT,)

        for element in elements:
            element_alias = bson_type_alias(element)
            accumulator.element_type_counts[element_alias] += 1

            if element_alias != "object":
                # An array of arrays records "array" as its element type and
                # stops there: expanding it would need a second, ambiguous
                # element marker for a shape that carries no field names.
                continue

            if not self._may_descend(element_prefix):
                accumulator.truncated_due_to_depth = True
                self._depth_limit_reached = True
                continue

            self._walk(element, element_prefix, seen)

    def _may_descend(self, segments: tuple[str, ...]) -> bool:
        """Whether a child of ``segments`` is still within ``max_depth``.

        Depth counts real field-name segments; the ``[]`` element marker is
        structural punctuation and does not consume a level, so ``max_depth=2``
        admits ``items[].sku`` exactly as it admits ``customer.id``.
        """
        named = sum(1 for segment in segments if segment != ARRAY_ELEMENT_SEGMENT)
        return named < self._options.max_depth

    def _accumulator(self, segments: tuple[str, ...]) -> _PathAccumulator | None:
        """Return the accumulator for a path, or ``None`` once the field
        budget is exhausted and the path is new."""
        existing = self._paths.get(segments)
        if existing is not None:
            return existing

        if len(self._paths) >= self._options.max_fields_per_collection:
            self._field_limit_reached = True
            self._dropped_path_count += 1
            return None

        created = _PathAccumulator(segments=segments)
        self._paths[segments] = created
        return created

    # ---- results ----

    def observations(self) -> tuple[FieldObservation, ...]:
        """Every observed path, in deterministic order.

        ``_id`` is emitted first (it is the collection's identity, and being
        first also guarantees it wins the normalized name ``id`` against any
        field that would otherwise collide with it); everything else follows
        in path order.
        """
        ordered = sorted(self._paths.values(), key=lambda item: _ordering_key(item.segments))

        return tuple(
            FieldObservation(
                path=render_path(accumulator.segments),
                segments=accumulator.segments,
                documents_sampled=self._documents_sampled,
                present_count=accumulator.present_count,
                null_count=accumulator.null_count,
                value_count=accumulator.value_count,
                type_counts=dict(accumulator.type_counts),
                element_type_counts=dict(accumulator.element_type_counts),
                truncated_due_to_depth=accumulator.truncated_due_to_depth,
                array_elements_truncated=accumulator.array_elements_truncated,
            )
            for accumulator in ordered
        )


def _ordering_key(segments: tuple[str, ...]) -> tuple[int, tuple[str, ...]]:
    return (0 if segments == (ID_FIELD,) else 1, segments)


def render_path(segments: Sequence[str]) -> str:
    """Render path segments readably: ``("items", "[]", "sku")`` -> ``items[].sku``."""
    rendered = ""

    for segment in segments:
        if segment == ARRAY_ELEMENT_SEGMENT:
            rendered += ARRAY_ELEMENT_SEGMENT
        elif rendered:
            rendered += f".{segment}"
        else:
            rendered = segment

    return rendered


# ============================================================
# Observations -> Phase 1 SourceField (Steps 6, 11, 12)
# ============================================================

@dataclass(frozen=True)
class InferredFields:
    """The Phase 1 fields one collection's observations produced."""

    fields: tuple[SourceField, ...]
    primary_key_fields: tuple[str, ...]
    notes: tuple[str, ...] = ()


def build_source_fields(
    observations: Sequence[FieldObservation],
    options: MongoInferenceOptions | None = None,
) -> InferredFields:
    """Turn observations into ``SourceField`` objects.

    Requiredness policy (Step 11). ``required`` is set ONLY when the path was
    present in every sampled document and was never null. That is observed
    requiredness within the sample, not a MongoDB constraint - a collection
    validator, if one exists, is reported separately and is never folded into
    this decision. ``nullable`` is its mirror: a field that was ever missing or
    ever null is nullable.
    """
    options = options or MongoInferenceOptions()

    fields: list[SourceField] = []
    primary_key_fields: list[str] = []
    notes: list[str] = []
    used_names: dict[str, int] = {}
    ordinal = 0

    for observation in observations:
        source_name = observation.segments[-1]
        nested_path = observation.segments[:-1] or None

        if not source_name.strip():
            # MongoDB tolerates a blank field name; Phase 1's SourceField
            # does not, and inventing a name for it would be a fabrication.
            # Skipped explicitly and reported, never dropped in silence.
            notes.append(
                f"Skipped observed path {observation.path!r}: its leaf key is "
                "blank, which cannot be represented as a SourceField name."
            )
            continue

        normalized_name = _unique_normalized_name(
            observation.segments, used_names, notes
        )

        normalized_type = resolve_normalized_type(observation.type_counts)
        source_data_type = render_source_data_type(
            observation.type_counts, observation.element_type_counts
        )

        is_array = normalized_type is FieldDataType.ARRAY
        is_identifier = _is_identity_field(observation)
        required = is_identifier or observation.observed_always_present

        fields.append(
            SourceField(
                source_name=source_name,
                normalized_name=normalized_name,
                source_data_type=source_data_type,
                normalized_data_type=normalized_type,
                nullable=not required,
                required=required,
                is_primary_key=is_identifier,
                # MongoDB guarantees _id uniqueness. No other observed field
                # may be called unique: distinctness in a sample is not a
                # uniqueness constraint, and Phase 5 does not guess.
                is_unique=is_identifier,
                is_array=is_array,
                nested_path=nested_path,
                # Phase 5 never infers business meaning. That is a later phase.
                semantic_type=None,
                description=None,
                ordinal=ordinal,
                metadata=_field_metadata(observation, options),
            )
        )

        if is_identifier:
            primary_key_fields.append(normalized_name)

        ordinal += 1

    return InferredFields(
        fields=tuple(fields),
        primary_key_fields=tuple(primary_key_fields),
        notes=tuple(notes),
    )


def _is_identity_field(observation: FieldObservation) -> bool:
    """Whether this observation is the collection's ``_id`` (Step 12).

    Requires ``_id`` at the document root, present in every sampled document
    and never null - which is what MongoDB itself guarantees. A collection
    whose sample somehow disagrees gets an ordinary field rather than a
    primary key asserted against the evidence.
    """
    return (
        observation.segments == (ID_FIELD,)
        and observation.documents_sampled > 0
        and observation.present_count == observation.documents_sampled
        and observation.null_count == 0
    )


def _unique_normalized_name(
    segments: Sequence[str],
    used_names: dict[str, int],
    notes: list[str],
) -> str:
    """Normalize a path to a unique field name within its entity.

    Two distinct MongoDB paths can normalize to one name - ``Amount`` and
    ``amount`` legitimately coexist in a schemaless collection, and ``_id``
    normalizes to ``id`` just as a field literally named ``id`` does. Phase 1
    requires normalized names to be unique within an entity, so a collision
    must be resolved rather than allowed to abort the whole inference run.
    Resolution is deterministic: paths are processed in a fixed order, the
    first claims the plain name, and later ones take ``.2``, ``.3``, ...
    """
    path = render_path(segments)

    try:
        base = normalize_identifier(path)
    except IdentityError:
        # A key made entirely of characters normalization strips ("---",
        # "___") leaves nothing to name the field with. A content-derived
        # fallback keeps the path representable AND deterministic: the same
        # key always yields the same name across runs.
        base = f"field.{hash_json_payload(list(segments))[:12]}"
        notes.append(
            f"Field path {path!r} contains no characters usable in a "
            f"normalized name; recorded as {base!r}."
        )

    unique = deduplicate_normalized_name(base, used_names)

    if unique != base:
        notes.append(
            f"Field path {path!r} normalizes to {base!r}, which is already used "
            f"in this collection; recorded as {unique!r}."
        )

    return unique


def deduplicate_normalized_name(base: str, used_names: dict[str, int]) -> str:
    """Return ``base``, or a deterministic ``base.2`` / ``base.3`` variant.

    Phase 1 requires normalized names to be unique within their scope, but a
    schemaless source can genuinely present two distinct names that normalize
    to one (``Amount``/``amount``, ``_id``/``id``, ``Orders``/``orders``).
    Aborting the run over that would be worse than resolving it, so callers
    that process their inputs in a FIXED order get a stable resolution: the
    first claimant keeps the plain name.

    ``used_names`` is the caller's running state and is updated in place.
    """
    count = used_names.get(base, 0)
    used_names[base] = count + 1

    if count == 0:
        return base

    candidate = f"{base}.{count + 1}"
    while candidate in used_names:
        count += 1
        used_names[base] = count + 1
        candidate = f"{base}.{count + 1}"

    used_names[candidate] = 1

    return candidate


def _field_metadata(
    observation: FieldObservation, options: MongoInferenceOptions
) -> dict[str, Any]:
    """JSON-safe, aggregate-only evidence for one inferred field.

    Deliberately NOT part of ``SourceSchema.compute_schema_hash()``, which
    ignores metadata entirely: raising ``max_documents_per_collection`` from
    500 to 1000 changes every count here, and that must not look like a schema
    change to the catalog. What IS structural - the field's existence, its
    types, and the ``required``/``nullable`` flags derived from presence -
    lives on the field itself.
    """
    metadata: dict[str, Any] = {
        "field_path": observation.path,
        "path_segments": list(observation.segments),
        "inference_method": "bounded_document_sample",
        # Stated in the data itself so no consumer can mistake a sample-derived
        # description for a declared MongoDB schema.
        "schema_claim": "observed",
    }

    if options.track_presence:
        metadata["observed"] = {
            "documents_sampled": observation.documents_sampled,
            "present_count": observation.present_count,
            "missing_count": observation.missing_count,
            "presence_ratio": observation.presence_ratio,
            "values_observed": observation.value_count,
        }

    if options.track_nulls:
        metadata.setdefault("observed", {})
        metadata["observed"]["null_count"] = observation.null_count
        metadata["observed"]["null_ratio"] = observation.null_ratio

    if options.track_type_distribution:
        metadata["bson_type_distribution"] = dict(sorted(observation.type_counts.items()))
        if observation.element_type_counts:
            metadata["array_element_bson_type_distribution"] = dict(
                sorted(observation.element_type_counts.items())
            )

    mixed = _is_mixed(observation.type_counts)
    if mixed:
        metadata["mixed_types"] = True
        metadata["mixed_type_resolution"] = resolve_normalized_type(
            observation.type_counts
        ).value

    if observation.truncated_due_to_depth:
        metadata["truncated_due_to_depth"] = True
    if observation.array_elements_truncated:
        metadata["array_elements_truncated"] = True
        metadata["max_array_elements_per_document"] = (
            options.max_array_elements_per_document
        )

    return metadata


def _is_mixed(type_counts: Mapping[str, int]) -> bool:
    observed = {
        alias for alias, count in type_counts.items()
        if alias != NULL_ALIAS and count > 0
    }
    return len(observed) > 1


__all__ = [
    "ARRAY_ELEMENT_SEGMENT",
    "ID_FIELD",
    "NULL_ALIAS",
    "UNKNOWN_ALIAS",
    "BSON_ALIAS_TO_FIELD_TYPE",
    "bson_type_alias",
    "normalize_bson_alias",
    "resolve_normalized_type",
    "render_source_data_type",
    "render_path",
    "DocumentStructureInference",
    "InferredFields",
    "build_source_fields",
    "deduplicate_normalized_name",
]
