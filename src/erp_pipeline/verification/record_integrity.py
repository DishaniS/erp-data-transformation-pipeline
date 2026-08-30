"""Checks that need only the contracts, not a live store.

Every function here is pure: give it two objects and it tells you where they
disagree. That makes the rules testable in milliseconds and reusable from the
cross-store scan, from a job stage, or from a test - without a database.

The checks recompute rather than trust. A stored ``content_hash`` is compared
against a freshly computed one, because a hash that was written wrong is
exactly the failure a verifier exists to catch, and comparing a stored value
against itself would catch nothing.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.schemas.identity import (
    IdentityError,
    looks_like_surrogate_key,
    parse_canonical_id,
)
from erp_pipeline.sync.hashing import vector_id_for
from erp_pipeline.verification.models import (
    IntegrityCode,
    IntegrityIssue,
    make_issue,
)


def check_record_identity(record_id: str) -> tuple[IntegrityIssue, ...]:
    """Validate one canonical id's grammar and its business-key component.

    Two distinct failures are separated deliberately. A malformed id means the
    grammar was violated. A surrogate-key component means the grammar held but
    the *value* came from an auto-increment column - the defect that silently
    re-identifies every record when a source table is rebuilt, and the reason
    the legacy ``case_<serial>`` form had to be refused outright.
    """
    issues: list[IntegrityIssue] = []

    try:
        _, _, stable_key = parse_canonical_id(record_id)
    except IdentityError as error:
        return (
            make_issue(
                IntegrityCode.MALFORMED_RECORD_ID,
                str(record_id),
                str(error),
            ),
        )

    if looks_like_surrogate_key(stable_key):
        issues.append(
            make_issue(
                IntegrityCode.SURROGATE_KEY_IDENTITY,
                record_id,
                f"the stable-key component {stable_key!r} is a bare integer, "
                "so this identity came from a database surrogate key rather "
                "than a business key",
                stable_key=stable_key,
            )
        )

    return tuple(issues)


def check_duplicate_ids(record_ids: Iterable[str]) -> tuple[IntegrityIssue, ...]:
    """Report identifiers that appear more than once.

    A duplicate canonical id means two different business records collapsed
    onto one identity, which silently loses one of them.
    """
    seen: dict[str, int] = {}

    for record_id in record_ids:
        seen[record_id] = seen.get(record_id, 0) + 1

    return tuple(
        make_issue(
            IntegrityCode.DUPLICATE_RECORD_ID,
            record_id,
            f"appears {count} times; a canonical id must identify exactly one "
            "record",
            occurrences=count,
        )
        for record_id, count in sorted(seen.items())
        if count > 1
    )


def check_representation(
    representation: Any,
    canonical_record: Any = None,
) -> tuple[IntegrityIssue, ...]:
    """Verify one AI representation against its canonical record.

    Checks that the representation's stored hash matches what its own content
    hashes to, and that it points back at a record that actually exists.
    """
    issues: list[IntegrityIssue] = []
    representation_id = getattr(representation, "representation_id", "<unknown>")

    stored = getattr(representation, "content_hash", None)

    if stored is not None:
        recomputed = representation.compute_hash()

        if stored != recomputed:
            issues.append(
                make_issue(
                    IntegrityCode.CONTENT_HASH_MISMATCH,
                    representation_id,
                    "the stored representation hash does not match a hash "
                    "recomputed from its own content",
                    stored=stored,
                    recomputed=recomputed,
                    layer="representation",
                )
            )

    declared = tuple(getattr(representation, "source_record_ids", ()) or ())

    if canonical_record is None:
        if declared:
            issues.append(
                make_issue(
                    IntegrityCode.CANONICAL_RECORD_MISSING,
                    representation_id,
                    f"references canonical record(s) {list(declared)!r} but no "
                    "record was found",
                    source_record_ids=list(declared),
                )
            )

        return tuple(issues)

    record_id = getattr(canonical_record, "record_id", None)

    if declared and record_id is not None and record_id not in declared:
        issues.append(
            make_issue(
                IntegrityCode.CANONICAL_REFERENCE_MISMATCH,
                representation_id,
                f"was matched to record {record_id!r}, which is not among its "
                f"declared source records {list(declared)!r}",
                record_id=record_id,
                source_record_ids=list(declared),
            )
        )

    return tuple(issues)


def check_embedding(
    embedding: Any,
    representation: Any = None,
    expected_dimension: int | None = None,
    expected_model_id: str | None = None,
) -> tuple[IntegrityIssue, ...]:
    """Verify one embedding record against its representation and the model.

    ``EMBEDDING_STALE`` is the important one: it means a vector exists but was
    produced from content that has since changed, so retrieval would return a
    hit whose text no longer says what the vector says.
    """
    issues: list[IntegrityIssue] = []
    representation_id = getattr(embedding, "representation_id", "<unknown>")

    status = getattr(embedding, "status", None)
    status_value = getattr(status, "value", status)
    produced = getattr(status, "produced_vector", None)

    if produced is False:
        issues.append(
            make_issue(
                IntegrityCode.EMBEDDING_NOT_GENERATED,
                representation_id,
                f"embedding status is {status_value!r}, so no vector was "
                "produced for this representation",
                status=status_value,
                reason=getattr(embedding, "reason", None),
            )
        )
    elif getattr(embedding, "vector", None) is None:
        issues.append(
            make_issue(
                IntegrityCode.VECTOR_MISSING,
                representation_id,
                f"embedding status is {status_value!r} but it carries no vector",
                status=status_value,
            )
        )

    vector = getattr(embedding, "vector", None)
    declared_dimension = getattr(embedding, "dimension", None)

    if vector is not None and declared_dimension is not None:
        if len(vector) != declared_dimension:
            issues.append(
                make_issue(
                    IntegrityCode.DIMENSION_MISMATCH,
                    representation_id,
                    f"vector has {len(vector)} components but the record "
                    f"declares {declared_dimension}",
                    actual=len(vector),
                    declared=declared_dimension,
                )
            )

    if expected_dimension is not None and declared_dimension is not None:
        if declared_dimension != expected_dimension:
            issues.append(
                make_issue(
                    IntegrityCode.DIMENSION_MISMATCH,
                    representation_id,
                    f"embedding declares dimension {declared_dimension} but the "
                    f"configured model produces {expected_dimension}",
                    declared=declared_dimension,
                    expected=expected_dimension,
                )
            )

    model_id = getattr(embedding, "model_id", None)

    if expected_model_id is not None and model_id != expected_model_id:
        issues.append(
            make_issue(
                IntegrityCode.MODEL_ID_MISMATCH,
                representation_id,
                f"embedding was produced by {model_id!r} but the configured "
                f"model is {expected_model_id!r}; their vectors are not "
                "comparable",
                stored=model_id,
                expected=expected_model_id,
            )
        )

    if representation is not None:
        current = representation.resolved_hash()
        embedded = getattr(embedding, "content_hash", None)

        if embedded is not None and embedded != current:
            issues.append(
                make_issue(
                    IntegrityCode.EMBEDDING_STALE,
                    representation_id,
                    "the vector was produced from content that has since "
                    "changed; retrieval would return a hit whose text no "
                    "longer matches its vector",
                    embedded_hash=embedded,
                    current_hash=current,
                )
            )

        representation_entity = getattr(representation, "entity_type", None)
        embedding_entity = getattr(embedding, "entity_type", None)

        if (
            representation_entity is not None
            and embedding_entity is not None
            and representation_entity != embedding_entity
        ):
            issues.append(
                make_issue(
                    IntegrityCode.ENTITY_TYPE_MISMATCH,
                    representation_id,
                    f"embedding says {embedding_entity!r} but the "
                    f"representation says {representation_entity!r}",
                    embedding_entity=embedding_entity,
                    representation_entity=representation_entity,
                )
            )

    return tuple(issues)


def check_vector_identity(
    representation_id: str, vector_id: str | None
) -> tuple[IntegrityIssue, ...]:
    """Verify a vector id is the deterministic derivation of its representation.

    A vector id that is not derivable means the point cannot be found again
    from the record, which is how vectors become orphaned in the first place.
    """
    if vector_id is None:
        return (
            make_issue(
                IntegrityCode.VECTOR_MISSING,
                representation_id,
                "no vector id is recorded for this representation",
            ),
        )

    expected = vector_id_for(representation_id)

    if vector_id != expected:
        return (
            make_issue(
                IntegrityCode.VECTOR_ID_MISMATCH,
                representation_id,
                "the stored vector id is not the deterministic derivation of "
                "the representation id, so the point cannot be resolved back "
                "to its record",
                stored=vector_id,
                expected=expected,
            ),
        )

    return ()


def check_metadata_agreement(
    metadata: Any, embedding: Any
) -> tuple[IntegrityIssue, ...]:
    """Verify tier state agrees with the embedding it claims to describe."""
    issues: list[IntegrityIssue] = []
    representation_id = getattr(metadata, "representation_id", "<unknown>")

    pairs = (
        ("content_hash", IntegrityCode.CONTENT_HASH_MISMATCH),
        ("model_id", IntegrityCode.MODEL_ID_MISMATCH),
        ("dimension", IntegrityCode.DIMENSION_MISMATCH),
        ("embedding_id", IntegrityCode.TIER_METADATA_MISMATCH),
    )

    for attribute, code in pairs:
        stored = getattr(metadata, attribute, None)
        actual = getattr(embedding, attribute, None)

        if stored is None or actual is None:
            continue

        if stored != actual:
            issues.append(
                make_issue(
                    code,
                    representation_id,
                    f"tier state records {attribute}={stored!r} but the "
                    f"embedding says {actual!r}",
                    attribute=attribute,
                    stored=stored,
                    actual=actual,
                    layer="tier_state",
                )
            )

    return tuple(issues)


def check_records(
    record_ids: Sequence[str],
) -> tuple[IntegrityIssue, ...]:
    """Run every identity check over a set of canonical ids."""
    issues: list[IntegrityIssue] = []

    for record_id in record_ids:
        issues.extend(check_record_identity(record_id))

    issues.extend(check_duplicate_ids(record_ids))

    return tuple(issues)


__all__ = [
    "check_record_identity",
    "check_duplicate_ids",
    "check_representation",
    "check_embedding",
    "check_vector_identity",
    "check_metadata_agreement",
    "check_records",
]
