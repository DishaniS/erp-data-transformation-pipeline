"""Retrieval filters over the identity facts stored beside every vector.

WHY THIS IS DELIBERATELY SMALL
------------------------------
A general query language over a vector store is a large surface with a large
failure mode: silently returning the wrong subset. What a downstream consumer
actually needs is narrow - *only invoices*, *only this ERP system*, *only this
document* - so this supports exactly those, over a closed set of fields, with
equality semantics and nothing else.

Anything not in the core :data:`FILTERABLE_FIELDS` or the current discovered
schema allow-list is REFUSED, not ignored. A filter that is silently dropped
returns a plausible-looking unfiltered result, which is the single worst thing
a retrieval API can do to a caller who is about to act on those results.

WHERE FILTERING HAPPENS
-----------------------
    HOT / WARM   pushed into Qdrant as a server-side ``Filter`` over the
                 vector payload, so the ANN search itself is constrained
    COLD         applied to the tier-state metadata BEFORE rehydration, which
                 is both correct and cheaper - a filtered-out archive is never
                 decrypted at all

Both paths match the same fields with the same equality semantics, so a query
cannot mean one thing for online tiers and another for the archive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from erp_pipeline.schemas.enums import ContentKind, EntityKind, SensitivityLevel
from erp_pipeline.schemas.search_fields import render_filter_value
from erp_pipeline.storage.errors import StorageError

#: The closed set of filterable fields. Each is an identity or provenance fact
#: that lives BOTH in the vector payload and on ``StorageRecordMetadata``,
#: which is what lets the online and archive paths agree.
FILTERABLE_FIELDS: tuple[str, ...] = (
    "entity_type",
    "source_system_id",
    "source_entity",
    #: Canonical business identity inside the source entity.
    "record_key",
    "sensitivity",
    "document_id",
    # ------------------------------------------------------------------
    # Phase 4 - ERP identity. These are what turn "documents that mention a
    # birth certificate" into "EMP002's birth certificate".
    # ------------------------------------------------------------------
    #: ``structured_record`` or ``document_chunk``.
    "content_kind",
    #: The ERP row a document hangs off.
    "parent_record_id",
    #: The binary column it came out of.
    "source_field",
    #: Generic business identity - ``employee_id`` / ``EMP002``. Generic on
    #: purpose: an ERP-independent pipeline cannot name ``employee_id`` in its
    #: retrieval contract, because the next source calls it ``machine_code``.
    "business_key_name",
    "business_key_value",
    #: What the business calls the document.
    "document_type",
    # ------------------------------------------------------------------
    # Phase 7 - scoping a schema search. Two fields, not six: the rest of a
    # schema's provenance is returned with the hit and nobody filters on it.
    # ------------------------------------------------------------------
    #: The schema/catalog a table lives in - ``public``, ``dbo``, ``sales``.
    "schema_name",
    #: ``table``, ``view``, ``collection``, ``dataset``.
    "entity_kind",
)

#: DELIBERATELY NOT FILTERABLE, though returned with every schema hit:
#: ``schema_id``, ``schema_version``, ``schema_hash``, ``entity_id``,
#: ``database_name``, ``schema_chunk_index``.
#:
#: ``schema_id`` is the strongest temptation and the clearest case. It is a
#: content-addressed SNAPSHOT id that changes every time the schema changes, so
#: a caller filtering on one they read yesterday gets nothing today and cannot
#: tell that from "no such schema". ``source_system_id`` + ``schema_name``
#: expresses what people actually ask for, and stays true across versions.

#: DELIBERATELY NOT FILTERABLE: ``page_start``, ``page_end``, ``chunk_index``.
#:
#: They are returned with every hit as provenance, and they are in the vector
#: payload, but they are not matchable. Two reasons, in order of weight:
#:
#: 1. There is no query for them. "Give me chunk 3 of something" is not a
#:    question a retrieval caller asks; they want the document, and the chunk
#:    ordinal is how the answer describes itself afterwards.
#: 2. This contract is string equality throughout - ``_validate_value``
#:    renders every value with ``str()``. Page numbers are stored as the
#:    integers they are, so a filter for ``page_start=1`` would compare
#:    ``"1"`` against ``1`` and match nothing, and the fix is either to
#:    stringify the payload (losing the type) or to introduce typed filters
#:    (a real change to a load-bearing contract). Neither is worth doing for
#:    a query nobody makes.
#:
#: If a page-range query is ever genuinely needed it wants ``>=`` / ``<=``
#: anyway, which this contract does not express - so it would be a designed
#: addition, not a line added to the tuple above.
PROVENANCE_ONLY_FIELDS: tuple[str, ...] = (
    "page_start",
    "page_end",
    "chunk_index",
)

#: Fields whose value must be a member of a declared enum.
#:
#: ``content_kind`` is closed rather than open: a filter that accepted
#: ``"schema"`` today would tell a caller the system holds schema vectors,
#: which it does not. When they exist, the enum gains a member.
_ENUM_FIELDS: Mapping[str, Any] = {
    "sensitivity": SensitivityLevel,
    "content_kind": ContentKind,
    # Closed for the same reason: `entity_kind=spreadsheet` is a typo, not a
    # query returning nothing.
    "entity_kind": EntityKind,
}


class UnknownFilterFieldError(StorageError):
    """A filter named a field that cannot be filtered on.

    Carries the offending names and the supported set, so the caller is told
    what to use rather than merely that they were wrong.
    """

    def __init__(
        self,
        unknown: Sequence[str],
        supported: Sequence[str] = FILTERABLE_FIELDS,
    ) -> None:
        self.unknown = tuple(sorted(unknown))
        self.supported = tuple(supported)

        super().__init__(
            "unsupported search filter(s): "
            + ", ".join(repr(name) for name in self.unknown)
            + ". Supported fields are: "
            + ", ".join(self.supported)
            + "."
        )


class InvalidFilterValueError(StorageError):
    """A filter value is empty, the wrong type, or outside a declared enum."""

    def __init__(self, field: str, value: Any, detail: str) -> None:
        self.field = field
        self.value = value

        super().__init__(f"invalid value for filter {field!r}: {detail}")


@dataclass(frozen=True)
class SearchFilters:
    """A validated set of equality constraints on retrieval."""

    criteria: Mapping[str, str]

    @property
    def is_empty(self) -> bool:
        return not self.criteria

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(sorted(self.criteria))

    # ------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        allowed_fields: Sequence[str] = (),
    ) -> "SearchFilters":
        """Validate a caller-supplied mapping into filters, or refuse it."""
        if not raw:
            return cls(criteria={})

        if not isinstance(raw, Mapping):
            raise InvalidFilterValueError(
                "filters", raw, "filters must be an object of field -> value"
            )

        supported = tuple(dict.fromkeys((*FILTERABLE_FIELDS, *allowed_fields)))
        unknown = [key for key in raw if key not in supported]

        if unknown:
            raise UnknownFilterFieldError(unknown, supported)

        criteria: dict[str, str] = {}

        for field, value in raw.items():
            criteria[field] = _validate_value(field, value)

        return cls(criteria=criteria)

    # ------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------

    def matches(self, subject: Any) -> bool:
        """Whether one record satisfies every criterion.

        Accepts a ``StorageRecordMetadata`` or any mapping, so the same rules
        serve the archive path, an in-memory tier, and a test.
        """
        if self.is_empty:
            return True

        for field, expected in self.criteria.items():
            actual = _read(subject, field)
            if isinstance(actual, Sequence) and not isinstance(actual, (str, bytes)):
                if expected not in {_render(item) for item in actual}:
                    return False
            elif _render(actual) != expected:
                return False

        return True

    def apply(self, subjects: Sequence[Any]) -> tuple[Any, ...]:
        """Keep only the subjects that satisfy every criterion."""
        if self.is_empty:
            return tuple(subjects)

        return tuple(item for item in subjects if self.matches(item))

    # ------------------------------------------------------------
    # Qdrant
    # ------------------------------------------------------------

    def to_qdrant_filter(self) -> Any | None:
        """Build a server-side Qdrant filter, or ``None`` when unfiltered.

        Imported lazily so this module - and everything that merely validates
        filters, including the API layer - carries no hard dependency on the
        vector-store client.
        """
        if self.is_empty:
            return None

        from qdrant_client import models as M

        return M.Filter(
            must=[
                M.FieldCondition(key=field, match=M.MatchValue(value=value))
                for field, value in sorted(self.criteria.items())
            ]
        )

    def to_dict(self) -> dict[str, str]:
        return dict(sorted(self.criteria.items()))

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return not self.is_empty


#: An unfiltered query, named so call sites read clearly.
NO_FILTERS = SearchFilters(criteria={})


def _validate_value(field: str, value: Any) -> str:
    """Normalize and check one filter value."""
    if value is None:
        raise InvalidFilterValueError(field, value, "value must not be null")

    if isinstance(value, bool) or isinstance(value, (list, tuple, dict, set)):
        raise InvalidFilterValueError(
            field, value, "value must be a single string"
        )

    rendered = _render(value)

    if not rendered:
        raise InvalidFilterValueError(field, value, "value must not be empty")

    enum_type = _ENUM_FIELDS.get(field)

    if enum_type is not None:
        try:
            return enum_type(rendered).value
        except ValueError:
            allowed = ", ".join(member.value for member in enum_type)

            raise InvalidFilterValueError(
                field, value, f"must be one of: {allowed}"
            ) from None

    return rendered


#: The ONE value-normalization function, shared with ingestion-time
#: tokenization (``schemas.search_fields.filter_value_token``) so a value
#: never normalizes one way when it is written and another when it is
#: searched for. See ``render_filter_value`` for what it actually does.
_render = render_filter_value


def _read(subject: Any, field: str) -> Any:
    """Read one field from a metadata object or a mapping."""
    if isinstance(subject, Mapping):
        value = subject.get(field)
        if field == "record_key" and value is None:
            return subject.get("business_key_value")
        if value is None:
            dynamic = subject.get("filter_attributes")
            if isinstance(dynamic, Mapping):
                return dynamic.get(field)
        return value

    value = getattr(subject, field, None)
    if field == "record_key" and value is None:
        return getattr(subject, "business_key_value", None)
    if value is None:
        dynamic = getattr(subject, "filter_attributes", None)
        if isinstance(dynamic, Mapping):
            return dynamic.get(field)
    return value


__all__ = [
    "FILTERABLE_FIELDS",
    "PROVENANCE_ONLY_FIELDS",
    "SearchFilters",
    "NO_FILTERS",
    "UnknownFilterFieldError",
    "InvalidFilterValueError",
]
