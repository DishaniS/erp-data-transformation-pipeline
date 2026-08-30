"""Payload indexes for the filterable fields (post-audit remediation).

WHY THIS MODULE EXISTS
----------------------
Managed Qdrant REFUSES a filtered search on a field that has no payload index,
with ``400 Bad Request: Index required but not found``. A local single-node
Qdrant accepts the same query and simply scans, which is why this never
appeared during development and appeared immediately on the first cloud
deployment.

The indexes were created by hand during that deployment. Nothing in the code
recreated them, so any future ``ensure_collection(recreate=True)`` would drop
them and break every filtered search until someone remembered.

ONE LIST, NOT TWO
-----------------
The fields come from ``FILTERABLE_FIELDS`` - the same tuple the filter builder
and the API validation already use. A second hand-maintained list would
eventually disagree with the first, and the failure mode of that disagreement is
a 400 on a filter the API advertises as supported.

WHY KEYWORD
-----------
Every filterable field is matched with ``MatchValue`` on a string, and several
are closed enums. That is exactly Qdrant's ``keyword`` schema. No field is
matched by range, prefix or full text, so no other index type is warranted -
and creating one would cost storage for a query shape the filter builder cannot
produce.
"""

from __future__ import annotations

from typing import Any

from erp_pipeline.storage.filters import FILTERABLE_FIELDS

#: The schema every filterable field is indexed under. See the module docstring
#: for why this is uniform rather than per-field.
PAYLOAD_INDEX_SCHEMA = "keyword"


def required_payload_indexes() -> tuple[str, ...]:
    """The fields that must be indexed for filtered search to work."""
    return tuple(FILTERABLE_FIELDS)


def ensure_payload_indexes(
    client: Any,
    collection_name: str,
    field_names: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Create any missing payload index on ``collection_name``. Idempotent.

    Safe to call on every startup and in every state the collection can be in:

    * collection missing            -> reported, nothing attempted
    * collection with no indexes    -> all created
    * partially indexed collection  -> only the gaps created
    * fully indexed collection      -> nothing created
    * repeated startup              -> nothing created

    Never recreates or deletes a collection: adding an index does not require
    it, and doing so would destroy vectors to fix metadata.

    Failures are REPORTED, not raised. A tier that cannot add an index still
    stores and retrieves vectors; refusing to start the whole service over it
    would trade a degraded filter for a total outage.
    """
    from qdrant_client.models import PayloadSchemaType

    report: dict[str, Any] = {
        "collection": collection_name,
        "created": [],
        "already_present": [],
        "failed": {},
    }

    try:
        existing_collections = {
            item.name for item in client.get_collections().collections
        }
    except Exception as error:  # noqa: BLE001 - reported, never fatal
        report["failed"]["__collections__"] = type(error).__name__

        return report

    if collection_name not in existing_collections:
        report["missing_collection"] = True

        return report

    try:
        info = client.get_collection(collection_name)
        indexed = set(getattr(info, "payload_schema", None) or {})
    except Exception:  # noqa: BLE001 - fall back to attempting every field
        indexed = set()

    wanted = required_payload_indexes() if field_names is None else field_names

    for field_name in wanted:
        if field_name in indexed:
            report["already_present"].append(field_name)
            continue

        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
                wait=True,
            )
            report["created"].append(field_name)
        except Exception as error:  # noqa: BLE001
            message = str(error).lower()

            # Qdrant reports an existing index as an error on some versions.
            # That is not a failure - the desired end state already holds.
            if "already exists" in message:
                report["already_present"].append(field_name)
            else:
                report["failed"][field_name] = type(error).__name__

    return report


__all__ = [
    "PAYLOAD_INDEX_SCHEMA",
    "ensure_payload_indexes",
    "required_payload_indexes",
]
