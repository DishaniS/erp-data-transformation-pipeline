"""Pure structural inference over CSV values.

The I/O-free half of CSV ingestion, mirroring the split Phase 5 established
between ``discovery.mongodb`` and ``discovery.mongodb_inference``: every typing
rule lives here and can be exercised with plain strings, while
``csv_ingestion`` owns files, encodings and streaming.

What a CSV actually tells you
-----------------------------
A CSV declares nothing. Every cell is text, and any type beyond that is an
inference drawn from how the text looks. So, exactly as in Phase 5, the result
is an OBSERVED structure over a bounded sample of rows, and the schema is
marked ``SchemaOrigin.INFERRED``.

The one place this differs from MongoDB inference - and the difference is
principled - is what happens when a column holds incompatible types. In
MongoDB an integer really is an integer and a string really is a string, so a
column holding both has no honest common type and resolves to ``UNKNOWN``. In
a CSV, every value is *already* text: ``STRING`` is true of every observed
value, so it is the correct conservative answer rather than a guess. See
``resolve_field_type``.

Privacy: a value's CATEGORY and LENGTH are counted; the value itself is never
retained. Nothing in this module can emit a cell's contents.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.ingestion.models import CsvOptions, FieldObservation
from erp_pipeline.schemas.enums import FieldDataType
from erp_pipeline.schemas.identity import IdentityError, hash_json_payload, normalize_identifier
from erp_pipeline.schemas.source_models import SourceField

#: Value categories observed in a CSV cell. An internal parser vocabulary, not
#: a competing public type enum: each maps onto the existing
#: ``FieldDataType`` in ``CATEGORY_TO_FIELD_TYPE`` below.
CATEGORY_EMPTY = "empty"
CATEGORY_NULL_MARKER = "null_marker"
CATEGORY_BOOLEAN = "boolean"
CATEGORY_INTEGER = "integer"
CATEGORY_DECIMAL = "decimal"
CATEGORY_DATE = "date"
CATEGORY_DATETIME = "datetime"
CATEGORY_STRING = "string"

#: Categories that carry no type evidence - they say a value is absent, not
#: what type it would have been.
EMPTY_CATEGORIES = frozenset({CATEGORY_EMPTY, CATEGORY_NULL_MARKER})

CATEGORY_TO_FIELD_TYPE: Mapping[str, FieldDataType] = {
    CATEGORY_BOOLEAN: FieldDataType.BOOLEAN,
    CATEGORY_INTEGER: FieldDataType.INTEGER,
    CATEGORY_DECIMAL: FieldDataType.DECIMAL,
    CATEGORY_DATE: FieldDataType.DATE,
    CATEGORY_DATETIME: FieldDataType.DATETIME,
    CATEGORY_STRING: FieldDataType.STRING,
}

#: Only these spellings are read as booleans. ``1``/``0`` are deliberately
#: absent: they are far more often integers, and a column of flags typed as
#: BOOLEAN when it is really a quantity would be a silent data error.
#: ``yes``/``no`` are likewise absent - they are ordinary domain vocabulary in
#: plenty of ERP exports.
BOOLEAN_TOKENS = frozenset({"true", "false"})

#: Date formats accepted without ambiguity. ``DD/MM/YYYY`` and ``MM/DD/YYYY``
#: are excluded on purpose: 03/04/2026 is two different dates depending on
#: locale, and no amount of sampling can settle which. Such columns are
#: reported as STRING, which is honest and lossless - a mapping profile can
#: convert them later once a human has stated the locale.
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")


def classify_value(text: str, null_tokens: frozenset[str] = frozenset(),
                   case_insensitive: bool = True) -> str:
    """Classify one raw CSV cell into a value category.

    Order matters and is chosen so a more specific reading always wins over a
    more general one. ``STRING`` is the fallback, never a preference.
    """
    if text is None:
        return CATEGORY_EMPTY

    stripped = text.strip()

    if not stripped:
        return CATEGORY_EMPTY

    if null_tokens:
        probe = stripped.lower() if case_insensitive else stripped
        if probe in null_tokens:
            return CATEGORY_NULL_MARKER

    if stripped.lower() in BOOLEAN_TOKENS:
        return CATEGORY_BOOLEAN

    if _is_integer(stripped):
        return CATEGORY_INTEGER

    if _is_decimal(stripped):
        return CATEGORY_DECIMAL

    if _is_date(stripped):
        return CATEGORY_DATE

    if _is_datetime(stripped):
        return CATEGORY_DATETIME

    return CATEGORY_STRING


def _is_integer(text: str) -> bool:
    """Whether a value is an integer, treating zero-padded codes as text.

    ``007``, ``0012`` and ``00`` are NOT integers here. A leading zero is
    almost always significant - a cost centre, an account code, a country
    prefix - and converting it to an int silently destroys information that
    cannot be recovered downstream. A bare ``0`` is of course still an integer.
    """
    body = text[1:] if text[:1] in "+-" else text

    if not body.isdigit():
        return False

    if len(body) > 1 and body[0] == "0":
        return False

    return True


def _is_decimal(text: str) -> bool:
    """Whether a value parses as a decimal number.

    Requires a decimal point or an exponent, so integers do not also match.
    Rejects NaN and Infinity, which ``Decimal`` accepts but which are not
    meaningful ERP quantities and are not JSON-representable.
    """
    if not any(marker in text for marker in (".", "e", "E")):
        return False

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return False

    return value.is_finite()


def _is_date(text: str) -> bool:
    if len(text) != 10:
        return False

    for pattern in _DATE_FORMATS:
        try:
            datetime.strptime(text, pattern)
            return True
        except ValueError:
            continue

    return False


def _is_datetime(text: str) -> bool:
    """Whether a value is an ISO-8601 timestamp.

    ``fromisoformat`` covers ``2026-01-15T09:30:00``, a space separator, and
    offsets including ``Z`` on modern Python. A bare date is excluded so it
    keeps the more precise DATE category.
    """
    # A bare date is 10 characters and is excluded here so it keeps the more
    # precise DATE category.
    if len(text) < 11:
        return False

    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False

    return True


def resolve_field_type(category_counts: Mapping[str, int]) -> FieldDataType:
    """Pick one ``FieldDataType`` for a column from its observed categories.

    Deterministic, documented, and conservative:

    * nothing but empties / null tokens -> ``UNKNOWN``. An absent value reveals
      no type.
    * one category -> that category's type.
    * ``INTEGER`` + ``DECIMAL`` -> ``DECIMAL``. Widening is lossless: every
      observed value really is a decimal.
    * ``DATE`` + ``DATETIME`` -> ``DATETIME``. Same family, wider member.
    * anything else -> ``STRING``.

    That last rule is the CSV-specific one, and it is not a cop-out. In a CSV
    every cell IS text, so ``STRING`` is a statement that is true of every
    single observed value - unlike MongoDB, where a column holding an ``int``
    and a ``string`` has no common type and Phase 5 correctly reports
    ``UNKNOWN``. Choosing ``STRING`` here loses no information (the source
    bytes are preserved verbatim in ``SourceRow``) and lets a later mapping
    profile apply a locale-aware or business-aware conversion.
    """
    observed = {
        category: count
        for category, count in category_counts.items()
        if category not in EMPTY_CATEGORIES and count > 0
    }

    if not observed:
        return FieldDataType.UNKNOWN

    types = {CATEGORY_TO_FIELD_TYPE[category] for category in observed}

    if len(types) == 1:
        return next(iter(types))

    if types == {FieldDataType.INTEGER, FieldDataType.DECIMAL}:
        return FieldDataType.DECIMAL

    if types == {FieldDataType.DATE, FieldDataType.DATETIME}:
        return FieldDataType.DATETIME

    return FieldDataType.STRING


def render_source_data_type(category_counts: Mapping[str, int]) -> str | None:
    """Render the observed categories as ``SourceField.source_data_type``.

    Examples::

        {"integer": 7}                     -> "integer"
        {"integer": 4, "decimal": 3}       -> "mixed<decimal|integer>"
        {"integer": 4, "string": 1}        -> "mixed<integer|string>"
        {"empty": 9}                       -> "empty"

    Category names are sorted, so the rendering depends only on WHICH
    categories were seen, never on how many. That matters because this string
    feeds the structural hash: a rendering that moved with the counts would
    mint a new catalog version every time the sample size changed.
    """
    observed = sorted(
        category for category, count in category_counts.items()
        if category not in EMPTY_CATEGORIES and count > 0
    )

    if not observed:
        return CATEGORY_EMPTY

    if len(observed) == 1:
        return observed[0]

    return f"mixed<{'|'.join(observed)}>"


# ============================================================
# Column accumulation
# ============================================================

@dataclass
class _ColumnAccumulator:
    """Mutable counters for one column. Integers only - never a value."""

    source_name: str
    column_index: int
    present_count: int = 0
    empty_count: int = 0
    null_marker_count: int = 0
    max_observed_length: int = 0
    category_counts: Counter = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.category_counts is None:
            self.category_counts = Counter()


class CsvStructureInference:
    """Accumulates the observed structure of a stream of CSV rows.

    Usage::

        inference = CsvStructureInference(header, options)
        for values in rows:
            inference.observe(values)
        observations = inference.observations()

    Column identity is POSITIONAL, which is what makes duplicate header names
    work: two columns both called ``amount`` are two accumulators, not one.
    """

    def __init__(self, header: Sequence[str], options: CsvOptions | None = None) -> None:
        self._options = options or CsvOptions()
        self._null_tokens = self._options.normalized_null_tokens()
        self._columns = [
            _ColumnAccumulator(source_name=name, column_index=index)
            for index, name in enumerate(header)
        ]
        self._rows_sampled = 0

    @property
    def rows_sampled(self) -> int:
        return self._rows_sampled

    def observe(self, values: Sequence[str]) -> None:
        """Record one physical row.

        A short row leaves its missing columns un-incremented, which is what
        makes ``present_count`` genuinely mean "this column had a cell here".
        Extra values beyond the header are ignored for typing - there is no
        column to attribute them to - and are reported separately by the reader
        as a malformed-row warning.
        """
        self._rows_sampled += 1

        for column in self._columns:
            if column.column_index >= len(values):
                continue

            raw = values[column.column_index]
            column.present_count += 1

            category = classify_value(
                raw,
                self._null_tokens,
                self._options.case_insensitive_null_tokens,
            )

            if category == CATEGORY_EMPTY:
                column.empty_count += 1
                continue

            if category == CATEGORY_NULL_MARKER:
                column.null_marker_count += 1
                continue

            column.category_counts[category] += 1
            # A length is structural (it informs a future VARCHAR width); the
            # text it measures is discarded.
            column.max_observed_length = max(
                column.max_observed_length, len(raw.strip())
            )

    def observe_all(self, rows: Iterable[Sequence[str]]) -> None:
        for values in rows:
            self.observe(values)

    def observations(self) -> tuple[FieldObservation, ...]:
        """Every column, in file order.

        Column order is the source's own and is preserved exactly - it is
        meaningful to whoever produced the export, and reordering it would make
        the schema harder to compare against the file a human is looking at.
        """
        return tuple(
            FieldObservation(
                source_name=column.source_name,
                column_index=column.column_index,
                rows_sampled=self._rows_sampled,
                present_count=column.present_count,
                empty_count=column.empty_count,
                null_marker_count=column.null_marker_count,
                category_counts=dict(column.category_counts),
                max_observed_length=column.max_observed_length,
            )
            for column in self._columns
        )


# ============================================================
# Observations -> Phase 1 SourceField
# ============================================================

@dataclass(frozen=True)
class InferredFields:
    """The Phase 1 fields one CSV's observations produced."""

    fields: tuple[SourceField, ...]
    notes: tuple[str, ...] = ()


def build_source_fields(
    observations: Sequence[FieldObservation],
    options: CsvOptions | None = None,
) -> InferredFields:
    """Turn column observations into ``SourceField`` objects.

    Requiredness policy, deliberately identical in spirit to Phase 5's: a
    column is ``required`` only when every sampled row had a cell for it and
    none of those cells was empty or a configured null token. That is OBSERVED
    requiredness over a bounded sample, never a constraint - a CSV declares
    none.

    No primary key is ever inferred. A column of distinct values is not a
    declared key, and asserting one would invent a constraint the file does
    not have. ``SourceEntity.primary_key_fields`` therefore stays empty for
    every CSV.
    """
    options = options or CsvOptions()

    fields: list[SourceField] = []
    notes: list[str] = []
    used_names: dict[str, int] = {}

    for observation in observations:
        normalized_name = _unique_normalized_name(
            observation.source_name, observation.column_index, used_names, notes
        )

        normalized_type = resolve_field_type(observation.category_counts)
        populated = observation.observed_always_populated

        fields.append(
            SourceField(
                source_name=observation.source_name or _placeholder_name(
                    observation.column_index
                ),
                normalized_name=normalized_name,
                source_data_type=render_source_data_type(observation.category_counts),
                normalized_data_type=normalized_type,
                nullable=not populated,
                required=populated,
                # A CSV declares no keys and no uniqueness constraints.
                is_primary_key=False,
                is_unique=False,
                is_array=False,
                # A CSV is flat by definition.
                nested_path=None,
                # Phase 6 never infers business meaning. That is Phase 8.
                semantic_type=None,
                description=None,
                ordinal=observation.column_index,
                metadata=_field_metadata(observation),
            )
        )

    return InferredFields(fields=tuple(fields), notes=tuple(notes))


def _placeholder_name(column_index: int) -> str:
    """Positional stand-in for a blank header cell.

    A header like ``id,,total`` has a real, unnamed second column. Dropping it
    would misalign every row; naming it positionally keeps the schema aligned
    with the file and says plainly where the name came from.
    """
    return f"column_{column_index + 1}"


def _unique_normalized_name(
    source_name: str,
    column_index: int,
    used_names: dict[str, int],
    notes: list[str],
) -> str:
    """Normalize a header to a unique field name within the entity.

    Duplicate header names are common in exported CSVs and are NOT an error
    here: the exact ``source_name`` is preserved on every field, and only the
    normalized name is disambiguated. Phase 1 requires normalized names to be
    unique within an entity, so a collision has to be resolved rather than
    allowed to abort the ingestion.

    Deterministic because columns are processed left to right: the first
    occurrence keeps the plain name and later ones take ``.2``, ``.3``.
    """
    candidate_source = source_name.strip() if source_name else ""

    if not candidate_source:
        base = _placeholder_name(column_index)
        notes.append(
            f"Column {column_index + 1} has a blank header; recorded as "
            f"{base!r}."
        )
    else:
        try:
            base = normalize_identifier(candidate_source)
        except IdentityError:
            # A header made entirely of characters normalization strips ("---",
            # "###") leaves nothing to name the field with. A content-derived
            # fallback keeps it representable AND deterministic.
            base = f"column.{hash_json_payload([candidate_source, column_index])[:12]}"
            notes.append(
                f"Column {column_index + 1} header contains no characters usable "
                f"in a normalized name; recorded as {base!r}."
            )

    unique = deduplicate_normalized_name(base, used_names)

    if unique != base:
        notes.append(
            f"Column {column_index + 1} normalizes to {base!r}, which is already "
            f"used by an earlier column; recorded as {unique!r}."
        )

    return unique


def deduplicate_normalized_name(base: str, used_names: dict[str, int]) -> str:
    """Return ``base``, or a deterministic ``base.2`` / ``base.3`` variant.

    Intentionally a local twin of
    ``discovery.mongodb_inference.deduplicate_normalized_name``: both phases
    face the same Phase 1 uniqueness rule from different sources, and the two
    are kept separate rather than shared because promoting the helper would
    mean editing a frozen phase to import it. Twelve duplicated lines is the
    cheaper of the two costs; if a third caller ever appears, that is the point
    to promote it into a shared module.

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


def _field_metadata(observation: FieldObservation) -> dict[str, Any]:
    """JSON-safe, aggregate-only evidence for one inferred column.

    Deliberately NOT part of ``SourceSchema.compute_schema_hash()``, which
    ignores metadata entirely: raising ``max_rows_for_schema_inference`` from
    1000 to 5000 changes every count here, and that must not look like a schema
    change to the catalog. What IS structural - the column's existence, its
    resolved type, and the ``required``/``nullable`` flags derived from
    presence - lives on the field itself.

    Contains no cell contents. ``max_observed_length`` is a measurement, not a
    sample.
    """
    return {
        "source_column_name": observation.source_name,
        "column_index": observation.column_index,
        "inference_method": "bounded_row_sample",
        # Stated in the data itself so no consumer mistakes a sample-derived
        # description for declared metadata.
        "schema_claim": "observed",
        "observed": {
            "rows_sampled": observation.rows_sampled,
            "present_count": observation.present_count,
            "missing_count": observation.missing_count,
            "empty_count": observation.empty_count,
            "null_marker_count": observation.null_marker_count,
            "values_observed": observation.value_count,
            "presence_ratio": observation.presence_ratio,
            "null_ratio": observation.null_ratio,
            "max_observed_length": observation.max_observed_length,
        },
        "value_category_distribution": dict(sorted(observation.category_counts.items())),
        "mixed_types": len(
            [c for c in observation.category_counts if c not in EMPTY_CATEGORIES]
        ) > 1,
    }


__all__ = [
    "CATEGORY_EMPTY",
    "CATEGORY_NULL_MARKER",
    "CATEGORY_BOOLEAN",
    "CATEGORY_INTEGER",
    "CATEGORY_DECIMAL",
    "CATEGORY_DATE",
    "CATEGORY_DATETIME",
    "CATEGORY_STRING",
    "CATEGORY_TO_FIELD_TYPE",
    "BOOLEAN_TOKENS",
    "classify_value",
    "resolve_field_type",
    "render_source_data_type",
    "CsvStructureInference",
    "InferredFields",
    "build_source_fields",
    "deduplicate_normalized_name",
]
