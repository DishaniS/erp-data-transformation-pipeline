"""In-memory fakes so MongoDB inference is testable without a live server.

``FakeMongoDatabase`` implements exactly the driver surface Phase 5 uses -
``list_collections()``, ``db[name].find(...)`` and
``estimated_document_count()`` - returning the same shapes pymongo returns.
That lets every edge case (empty collection, mixed types, a validator, a
failing sort, a huge array) be constructed precisely and deterministically.

``FakeMongoConnector`` subclasses the GENUINE Phase 3 ``MongoDBConnector``, so
inference exercises the same type checking, lifecycle and
``create_database_handle()`` seam it would use against a real server; only the
handle itself is substituted. It also never needs ``pymongo`` installed,
because the real connector imports the driver lazily and the fake never
reaches that code path.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.connectors.config import ConnectionSettings
from erp_pipeline.connectors.mongodb import MongoDBConnector
from erp_pipeline.schemas.enums import SourceType


class FakeMongoCollection:
    """Mimics the subset of ``pymongo.collection.Collection`` inference uses."""

    def __init__(
        self,
        documents: Sequence[Mapping[str, Any]] = (),
        estimated_count: int | None = None,
        failing_methods: Sequence[str] = (),
        fail_sorted_find: bool = False,
    ) -> None:
        self.documents = list(documents)
        self._estimated_count = estimated_count
        self._failing = set(failing_methods)
        self._fail_sorted_find = fail_sorted_find
        #: Recorded so tests can assert HOW the sample was requested, not just
        #: what came back - the sort key and the limit are the whole
        #: determinism and safety story.
        self.find_calls: list[dict[str, Any]] = []

    def find(self, filter=None, sort=None, limit=0, **kwargs):  # noqa: A002 - driver's name
        if "find" in self._failing:
            raise RuntimeError("simulated find failure")

        if sort is not None and self._fail_sorted_find:
            raise RuntimeError("simulated sort failure")

        self.find_calls.append({"filter": filter, "sort": sort, "limit": limit})

        selected = list(self.documents)

        if sort:
            field, direction = sort[0]
            selected.sort(
                key=lambda document: _sort_key(document.get(field)),
                reverse=direction < 0,
            )

        if limit:
            selected = selected[:limit]

        return iter(selected)

    def estimated_document_count(self, **kwargs) -> int:
        if "estimated_document_count" in self._failing:
            raise RuntimeError("simulated count failure")

        if self._estimated_count is not None:
            return self._estimated_count

        return len(self.documents)


def _sort_key(value: Any) -> tuple[int, str]:
    """Total order over heterogeneous ``_id`` values.

    Real MongoDB sorts by BSON type then value; reproducing that exactly is
    unnecessary here. All the fake has to guarantee is that the SAME documents
    always come back in the SAME order, which is what the determinism tests
    actually check.
    """
    if value is None:
        return (0, "")
    return (1, str(value))


class FakeMongoDatabase:
    """Mimics the subset of ``pymongo.database.Database`` inference uses."""

    def __init__(
        self,
        collections: Mapping[str, FakeMongoCollection] | None = None,
        collection_types: Mapping[str, str] | None = None,
        collection_options: Mapping[str, Mapping[str, Any]] | None = None,
        listing_order: Sequence[str] | None = None,
        failing_methods: Sequence[str] = (),
    ) -> None:
        self._collections = dict(collections or {})
        self._collection_types = dict(collection_types or {})
        self._collection_options = dict(collection_options or {})
        self._listing_order = list(listing_order or self._collections)
        self._failing = set(failing_methods)

    def list_collections(self) -> Iterable[dict[str, Any]]:
        if "list_collections" in self._failing:
            raise RuntimeError("simulated list_collections failure")

        return [
            {
                "name": name,
                "type": self._collection_types.get(name, "collection"),
                "options": dict(self._collection_options.get(name, {})),
            }
            for name in self._listing_order
        ]

    def __getitem__(self, name: str) -> FakeMongoCollection:
        if name not in self._collections:
            self._collections[name] = FakeMongoCollection()
        return self._collections[name]


class FakeMongoConnector(MongoDBConnector):
    """A real ``MongoDBConnector`` wired to a ``FakeMongoDatabase``."""

    def __init__(
        self,
        database_handle: FakeMongoDatabase,
        source_system_id: str = "fake_mongo",
        database: str = "fake_mongo_db",
        handle_error: Exception | None = None,
    ) -> None:
        settings = ConnectionSettings(
            source_system_id=source_system_id,
            source_type=SourceType.MONGODB,
            host="localhost",
            port=27017,
            database=database,
            username="app_user",
            password="fake-password",
        )
        super().__init__(settings)
        self._handle = database_handle
        self._handle_error = handle_error

    def create_database_handle(self):
        self._require_open()

        if self._handle_error is not None:
            raise self._handle_error

        return self._handle

    def close(self) -> None:
        # No real client was ever opened, so there is nothing to release.
        self._closed = True


def mongo_connector(
    documents_by_collection: Mapping[str, Sequence[Mapping[str, Any]]],
    **database_kwargs: Any,
) -> FakeMongoConnector:
    """Shorthand: build a connector over plain document lists."""
    collections = {
        name: FakeMongoCollection(documents)
        for name, documents in documents_by_collection.items()
    }
    return FakeMongoConnector(
        FakeMongoDatabase(collections, **database_kwargs)
    )
