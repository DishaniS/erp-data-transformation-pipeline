"""Structure inference from JSON examples, for specifications that declare none.

Postman is the reason this module exists. A Postman collection declares no
types at all: a request body is a literal JSON payload someone once sent, and
a saved response is a literal payload the server once returned. The only way
to describe those contracts is to observe their structure - which is exactly
the problem Phase 5 solved for MongoDB documents.

REUSE, not reimplementation
---------------------------
``discovery.mongodb_inference.DocumentStructureInference`` already accumulates
observed structure over a stream of JSON-shaped objects: nested paths, arrays
of objects, presence counts, type distributions, depth and field budgets, and
the guarantee that no value is ever retained. That engine is pure, imports no
driver, and is covered by the Phase 5 suite.

Writing a second inference engine here would duplicate the riskiest logic in
the codebase and give it half the test coverage. So this module reuses the
engine and adds only what is genuinely API-specific:

* a JSON type vocabulary - the engine speaks BSON aliases (``int``,
  ``double``, ``bool``), and an API contract should read in JSON terms
  (``integer``, ``number``, ``boolean``);
* field construction with API metadata and an ``inferred_from_examples``
  provenance marker, so an inferred field is never mistaken for a declared
  one.

(If a fourth consumer of the engine appears, it should be promoted out of the
``discovery`` package into a shared module. Two consumers do not yet justify
editing a frozen phase.)

PRIVACY
-------
The engine counts a value's TYPE and discards the value. Nothing here can emit
``"INV-1001"`` or ``4200`` - the accumulators hold integers only. That is the
whole reason to reuse it rather than write something new under time pressure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from erp_pipeline.api_specs.models import ApiSpecOptions, StructureOrigin
from erp_pipeline.discovery.mongodb_inference import (
    ARRAY_ELEMENT_SEGMENT,
    DocumentStructureInference,
    normalize_bson_alias,
    render_path,
    resolve_normalized_type,
)
from erp_pipeline.discovery.models import FieldObservation, MongoInferenceOptions
from erp_pipeline.schemas.enums import FieldDataType
from erp_pipeline.schemas.identity import IdentityError, hash_json_payload, normalize_identifier
from erp_pipeline.schemas.source_models import SourceField

#: BSON alias -> JSON type name. The engine's vocabulary is BSON's because it
#: was written for MongoDB; an API contract should describe itself in JSON
#: terms, so the rendering is translated at the boundary rather than the
#: engine being changed.
_ALIAS_TO_JSON_TYPE: Mapping[str, str] = {
    "string": "string",
    "int": "integer",
    "long": "integer",
    "double": "number",
    "decimal": "number",
    "bool": "boolean",
    "object": "object",
    "array": "array",
    "null": "null",
    "date": "string",
    "binData": "string",
}


@dataclass(frozen=True)
class InferredStructure:
    """The structure a set of JSON examples exhibited."""

    fields: tuple[SourceField, ...]
    examples_observed: int
    root_type: FieldDataType
    root_source_type: str
    partial: bool = False
    observations: tuple[FieldObservation, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.fields


def json_type_name(alias: str) -> str:
    """Translate one BSON alias into its JSON type name."""
    return _ALIAS_TO_JSON_TYPE.get(alias, "unknown")


def render_observed_type(observation: FieldObservation) -> str:
    """Render a field's observed types in JSON vocabulary.

    Mirrors Phase 5's rendering rules - sorted names, so the string depends
    only on WHICH types were seen and never on how many. That matters because
    ``source_data_type`` feeds the structural hash: a rendering that moved with
    the counts would mint a new catalog version every time an example was
    added.
    """
    names = sorted(
        {
            json_type_name(alias)
            for alias, count in observation.type_counts.items()
            if alias != "null" and count > 0
        }
    )

    if not names:
        return "null" if observation.type_counts.get("null") else "unknown"

    if names == ["array"]:
        element_names = sorted(
            {
                json_type_name(alias)
                for alias, count in observation.element_type_counts.items()
                if alias != "null" and count > 0
            }
        )
        if not element_names:
            return "array<empty>"
        if len(element_names) == 1:
            return f"array<{element_names[0]}>"
        return f"array<mixed<{'|'.join(element_names)}>>"

    if len(names) == 1:
        return names[0]

    return f"mixed<{'|'.join(names)}>"


def infer_structure_from_examples(
    payloads: Sequence[Any],
    options: ApiSpecOptions,
    entity_hint: str = "",
) -> InferredStructure:
    """Observe the common structure of one or more JSON payloads.

    Multiple examples are COMBINED rather than compared-and-discarded
    (Step 32): a field present in one payload and absent from another is
    recorded at 50% presence, and a field that is an integer in one and a
    string in another keeps both in its type distribution and resolves to
    ``UNKNOWN``. Structural disagreement between examples is information, not
    noise, and hiding it would let a later phase assume a consistency the API
    does not have.

    A payload whose root is an array is described by its ELEMENTS, because
    that is where the fields are; the array-ness is recorded on the root type
    rather than lost.
    """
    engine_options = MongoInferenceOptions(
        max_documents_per_collection=max(len(payloads), 1),
        max_total_documents=max(len(payloads), 1),
        max_depth=options.max_nesting_depth,
        max_fields_per_collection=options.max_fields_per_schema,
        # An example array is illustrative; a handful of elements shows the
        # shape and the rest add cost without adding structure.
        max_array_elements_per_document=25,
    )

    inference = DocumentStructureInference(engine_options)

    root_type = FieldDataType.UNKNOWN
    root_source_type = "unknown"
    observed = 0

    for payload in payloads:
        if isinstance(payload, Mapping):
            inference.observe(payload)
            root_type = FieldDataType.OBJECT
            root_source_type = "object"
            observed += 1
        elif isinstance(payload, (list, tuple)):
            root_type = FieldDataType.ARRAY
            root_source_type = "array<object>"
            observed += 1
            for element in payload[: engine_options.max_array_elements_per_document]:
                if isinstance(element, Mapping):
                    inference.observe(element)
        else:
            # A scalar body - a bare string or number. Structurally there is
            # nothing to describe, and inventing a wrapper field would be a
            # fabrication.
            observed += 1
            if root_type is FieldDataType.UNKNOWN:
                root_source_type = "scalar"

    observations = inference.observations()

    return InferredStructure(
        fields=build_inferred_fields(observations, entity_hint),
        examples_observed=observed,
        root_type=root_type,
        root_source_type=root_source_type,
        partial=inference.partial,
        observations=observations,
    )


def build_inferred_fields(
    observations: Sequence[FieldObservation], entity_hint: str = ""
) -> tuple[SourceField, ...]:
    """Turn observations into ``SourceField`` objects with API semantics.

    Requiredness policy, identical in spirit to Phase 5 and Phase 6: a field is
    ``required`` only when every observed example contained it and none was
    null. That is OBSERVED requiredness over the examples that happened to be
    saved - never a contract guarantee. A Postman collection with one saved
    response makes every field look required, which is honest and is exactly
    why ``examples_observed`` travels with the claim.
    """
    fields: list[SourceField] = []
    used_names: dict[str, int] = {}

    for observation in observations:
        source_name = observation.segments[-1]

        if not source_name.strip():
            continue

        normalized_type = resolve_normalized_type(observation.type_counts)
        always_present = observation.observed_always_present

        fields.append(
            SourceField(
                source_name=source_name,
                normalized_name=_unique_name(observation.segments, used_names),
                source_data_type=render_observed_type(observation),
                normalized_data_type=normalized_type,
                nullable=not always_present,
                required=always_present,
                # Examples declare no keys and no uniqueness.
                is_primary_key=False,
                is_unique=False,
                is_array=normalized_type is FieldDataType.ARRAY,
                nested_path=observation.segments[:-1] or None,
                # Phase 7 never infers business meaning. That is Phase 8.
                semantic_type=None,
                description=None,
                ordinal=len(fields),
                metadata=_inferred_field_metadata(observation),
            )
        )

    return tuple(fields)


def _inferred_field_metadata(observation: FieldObservation) -> dict[str, Any]:
    """Aggregate-only evidence for one inferred field.

    Counts and type names, never a value. Excluded from the structural hash
    (Phase 1 ignores metadata), so adding a saved example changes these numbers
    without manufacturing a catalog version - while a genuinely new field, or a
    field that stops being always-present, does change the hash.
    """
    return {
        "field_path": observation.path,
        "structure_origin": StructureOrigin.INFERRED_FROM_EXAMPLES.value,
        "inference_method": "json_example_observation",
        "observed": {
            "examples_sampled": observation.documents_sampled,
            "present_count": observation.present_count,
            "missing_count": observation.missing_count,
            "null_count": observation.null_count,
            "presence_ratio": observation.presence_ratio,
            "values_observed": observation.value_count,
        },
        "json_type_distribution": _json_type_distribution(observation.type_counts),
        **(
            {
                "array_element_type_distribution": _json_type_distribution(
                    observation.element_type_counts
                )
            }
            if observation.element_type_counts
            else {}
        ),
        "mixed_types": len(
            [alias for alias, count in observation.type_counts.items()
             if alias != "null" and count > 0]
        ) > 1,
    }


def _json_type_distribution(counts: Mapping[str, int]) -> dict[str, int]:
    """Re-key a BSON-alias distribution into JSON type names."""
    distribution: dict[str, int] = {}

    for alias, count in counts.items():
        name = json_type_name(alias)
        distribution[name] = distribution.get(name, 0) + count

    return dict(sorted(distribution.items()))


def _unique_name(segments: Sequence[str], used_names: dict[str, int]) -> str:
    path = render_path(segments)

    try:
        base = normalize_identifier(path)
    except IdentityError:
        base = f"field.{hash_json_payload(list(segments))[:12]}"

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


def infer_fields_from_parameters(
    parameter_names: Sequence[tuple[str, bool]],
) -> tuple[SourceField, ...]:
    """Describe a body declared only as named parameters.

    Postman ``urlencoded`` and ``formdata`` bodies carry names but no types.
    Every field is therefore ``STRING`` - which is not a guess but a fact about
    the encoding: form encodings transmit text, and claiming otherwise would
    invent type information the collection does not contain.
    """
    fields: list[SourceField] = []
    used_names: dict[str, int] = {}

    for name, enabled in parameter_names:
        if not name.strip():
            continue

        fields.append(
            SourceField(
                source_name=name,
                normalized_name=_unique_name((name,), used_names),
                source_data_type="string",
                normalized_data_type=FieldDataType.STRING,
                nullable=not enabled,
                required=enabled,
                is_primary_key=False,
                is_unique=False,
                is_array=False,
                nested_path=None,
                semantic_type=None,
                description=None,
                ordinal=len(fields),
                metadata={
                    "structure_origin": (
                        StructureOrigin.INFERRED_FROM_PARAMETERS.value
                    ),
                    "inference_method": "form_parameter_names",
                    "enabled": bool(enabled),
                },
            )
        )

    return tuple(fields)


__all__ = [
    "ARRAY_ELEMENT_SEGMENT",
    "InferredStructure",
    "infer_structure_from_examples",
    "build_inferred_fields",
    "infer_fields_from_parameters",
    "json_type_name",
    "render_observed_type",
]
