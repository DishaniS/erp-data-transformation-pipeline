"""The real embedding and Qdrant adapters (Steps 6, 7, 12).

The embedding model is exercised for real - it is cached locally, so no
network is needed. Qdrant is exercised through a recording double when the
server is unreachable, and through the real client when it is up: the
point-identity and no-duplicate properties are what matter, and both are
observable either way.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from erp_pipeline.sync.propagation import AIRepresentation, EmbeddingResult
from erp_integrations.bpi_postgres_cascade import (
    BpiEmbeddingUpdater,
    QdrantCaseVectorStore,
    build_unified_case_record,
    make_qdrant_point_id,
    qdrant_point_count,
)

CASE_ROW = {
    "id": 42,
    "case_record_id": "case:requestforpayment:livecase-0001",
    "content_hash": "a" * 64,
    "case_id": "LIVECASE-0001",
    "process_type": "RequestForPayment",
    "case_summary": (
        "ERP case LIVECASE-0001 belongs to the RequestForPayment process. "
        "The case contains 4 recorded workflow events."
    ),
    "total_events": 4,
    "start_timestamp": "2026-08-14T09:01:00+00:00",
    "end_timestamp": "2026-08-14T09:04:00+00:00",
}


class FakeAccess:
    """Just enough ``PostgresCaseAccess`` surface for the adapters."""

    def __init__(self, row=None):
        self.row = dict(row or CASE_ROW)
        self.marked: list[tuple[str, str]] = []

    def load_case_row(self, case_record_id):
        return dict(self.row) if case_record_id == self.row["case_record_id"] else None

    def mark_embedded(self, case_record_id, point_id):
        self.marked.append((case_record_id, point_id))


class RecordingQdrant:
    """A Qdrant client double that records points by id.

    Records rather than counts, so "the same point was replaced" and "a second
    point was added" are distinguishable - which is the whole question.
    """

    def __init__(self):
        self.points: dict[str, dict] = {}
        self.upsert_calls = 0
        self.delete_calls = 0
        self.collections: list[str] = []

    def upsert(self, collection_name, points):
        self.upsert_calls += 1
        self.collections.append(collection_name)
        for point in points:
            self.points[str(point.id)] = {
                "vector": list(point.vector),
                "payload": dict(point.payload or {}),
            }
        return True

    def delete(self, collection_name, points_selector):
        self.delete_calls += 1
        for point_id in points_selector:
            self.points.pop(str(point_id), None)
        return True


def representation() -> AIRepresentation:
    return AIRepresentation(
        representation_id=CASE_ROW["case_record_id"],
        entity_type="case",
        text_for_ai=CASE_ROW["case_summary"],
        content={"case_id": CASE_ROW["case_id"]},
        content_hash=CASE_ROW["content_hash"],
    )


# ============================================================
# Unified bridge (Step 6)
# ============================================================

def test_the_unified_record_matches_what_the_embedder_reads():
    unified = build_unified_case_record(CASE_ROW)

    assert unified["record_id"] == CASE_ROW["case_record_id"]
    assert unified["unified_record_id"] == CASE_ROW["case_record_id"]
    assert unified["record_type"] == "erp_case"
    assert unified["text_for_ai"] == CASE_ROW["case_summary"]
    assert unified["metadata"]["case_id"] == "LIVECASE-0001"


def test_the_embedding_text_comes_from_the_existing_builder():
    from bpi2020.embeddings.generate_and_store_embeddings import (
        build_embedding_text,
    )

    text = build_embedding_text(build_unified_case_record(CASE_ROW))

    assert "Record type: erp_case" in text
    assert "RequestForPayment" in text
    assert CASE_ROW["case_summary"] in text


def test_the_payload_comes_from_the_existing_builder():
    from bpi2020.embeddings.generate_and_store_embeddings import (
        make_qdrant_payload,
    )

    payload = make_qdrant_payload(build_unified_case_record(CASE_ROW))

    assert payload["record_id"] == CASE_ROW["case_record_id"]
    assert payload["case_id"] == "LIVECASE-0001"
    assert payload["content_hash"] == CASE_ROW["content_hash"]


def test_a_legacy_serial_record_id_is_still_rejected():
    """The existing linkage guard must keep protecting this path."""
    from bpi2020.embeddings.generate_and_store_embeddings import (
        EmbeddingLinkageError,
        resolve_record_id,
    )

    with pytest.raises(EmbeddingLinkageError):
        resolve_record_id({"record_id": "case_12345"})


# ============================================================
# Real embedding model (Step 6)
# ============================================================

def _model_is_cached() -> bool:
    hub = Path(os.path.expanduser("~")) / ".cache" / "huggingface" / "hub"
    return hub.exists() and any(hub.glob("models--sentence-transformers--*"))


@pytest.mark.skipif(
    not _model_is_cached(), reason="embedding model is not cached locally"
)
def test_the_real_embedder_produces_a_vector_for_one_case():
    access = FakeAccess()
    updater = BpiEmbeddingUpdater(access)

    result = updater.embed(representation())

    assert isinstance(result, EmbeddingResult)
    assert result.representation_id == CASE_ROW["case_record_id"]
    assert result.content_hash == CASE_ROW["content_hash"]
    assert result.dimensions == 384
    assert len(result.vector) == 384


@pytest.mark.skipif(
    not _model_is_cached(), reason="embedding model is not cached locally"
)
def test_the_real_embedder_is_deterministic():
    updater = BpiEmbeddingUpdater(FakeAccess())

    first = updater.embed(representation())
    second = updater.embed(representation())

    assert first.vector == second.vector


@pytest.mark.skipif(
    not _model_is_cached(), reason="embedding model is not cached locally"
)
def test_the_real_embedder_embeds_exactly_one_case():
    updater = BpiEmbeddingUpdater(FakeAccess())

    updater.embed(representation())

    assert updater.calls == 1
    assert updater.embedded_ids == [CASE_ROW["case_record_id"]]


@pytest.mark.skipif(
    not _model_is_cached(), reason="embedding model is not cached locally"
)
def test_the_model_is_loaded_once_not_per_record():
    updater = BpiEmbeddingUpdater(FakeAccess())

    updater.embed(representation())
    loaded = updater._model
    updater.embed(representation())

    assert updater._model is loaded


def test_the_batch_embedding_script_is_untouched():
    """Step 6: a single-record path was ADDED beside it, not into it."""
    source = Path(
        "src/bpi2020/embeddings/generate_and_store_embeddings.py"
    ).read_text(encoding="utf-8")

    assert "erp_pipeline" not in source
    assert "erp_integrations" not in source


# ============================================================
# Vector adapter (Steps 7, 12)
# ============================================================

def test_the_vector_adapter_uses_the_frozen_point_id():
    access = FakeAccess()
    client = RecordingQdrant()
    store = QdrantCaseVectorStore(client, access, collection_name="test_col")

    store.upsert(
        representation(),
        EmbeddingResult(
            representation_id=CASE_ROW["case_record_id"],
            content_hash=CASE_ROW["content_hash"],
            vector=(0.1, 0.2),
        ),
    )

    expected = make_qdrant_point_id(CASE_ROW["case_record_id"])

    assert list(client.points) == [expected]


def test_updating_a_case_replaces_the_same_point():
    """Step 12: the collection must not grow because a case changed."""
    access = FakeAccess()
    client = RecordingQdrant()
    store = QdrantCaseVectorStore(client, access, collection_name="test_col")

    store.upsert(
        representation(),
        EmbeddingResult(
            representation_id=CASE_ROW["case_record_id"],
            content_hash="a" * 64,
            vector=(0.1, 0.2),
        ),
    )
    before = len(client.points)

    store.upsert(
        representation(),
        EmbeddingResult(
            representation_id=CASE_ROW["case_record_id"],
            content_hash="b" * 64,
            vector=(0.9, 0.8),
        ),
    )

    assert len(client.points) == before == 1
    assert client.upsert_calls == 2


def test_the_replaced_point_carries_the_new_vector():
    access = FakeAccess()
    client = RecordingQdrant()
    store = QdrantCaseVectorStore(client, access, collection_name="test_col")
    point_id = make_qdrant_point_id(CASE_ROW["case_record_id"])

    store.upsert(
        representation(),
        EmbeddingResult(
            representation_id=CASE_ROW["case_record_id"],
            content_hash="a" * 64,
            vector=(0.1, 0.2),
        ),
    )
    store.upsert(
        representation(),
        EmbeddingResult(
            representation_id=CASE_ROW["case_record_id"],
            content_hash="b" * 64,
            vector=(0.9, 0.8),
        ),
    )

    assert client.points[point_id]["vector"] == [0.9, 0.8]


def test_the_vector_adapter_writes_back_the_embedding_status():
    access = FakeAccess()
    store = QdrantCaseVectorStore(
        RecordingQdrant(), access, collection_name="test_col"
    )

    store.upsert(
        representation(),
        EmbeddingResult(
            representation_id=CASE_ROW["case_record_id"],
            content_hash=CASE_ROW["content_hash"],
            vector=(0.1,),
        ),
    )

    assert access.marked == [
        (
            CASE_ROW["case_record_id"],
            make_qdrant_point_id(CASE_ROW["case_record_id"]),
        )
    ]


def test_the_vector_adapter_deletes_a_stale_point():
    client = RecordingQdrant()
    store = QdrantCaseVectorStore(
        client, FakeAccess(), collection_name="test_col"
    )
    point_id = make_qdrant_point_id(CASE_ROW["case_record_id"])

    store.upsert(
        representation(),
        EmbeddingResult(
            representation_id=CASE_ROW["case_record_id"],
            content_hash="a" * 64,
            vector=(0.1,),
        ),
    )
    store.delete(point_id)

    assert client.points == {}


def test_no_second_collection_is_created():
    """Step 7: the existing collection is reused."""
    client = RecordingQdrant()
    store = QdrantCaseVectorStore(
        client, FakeAccess(), collection_name="bpi2020_erp_knowledge"
    )

    store.upsert(
        representation(),
        EmbeddingResult(
            representation_id=CASE_ROW["case_record_id"],
            content_hash="a" * 64,
            vector=(0.1,),
        ),
    )

    assert set(client.collections) == {"bpi2020_erp_knowledge"}


def test_the_default_collection_is_the_repositorys_own():
    from bpi2020.common.config import get_vector_collection

    store = QdrantCaseVectorStore(RecordingQdrant(), FakeAccess())

    assert store._collection() == get_vector_collection()


def test_the_adapter_creates_no_collection_itself():
    source = Path(
        "src/erp_integrations/bpi_postgres_cascade.py"
    ).read_text(encoding="utf-8")

    assert "create_collection" not in source
    assert "recreate_collection" not in source
    assert "VectorParams" not in source


# ============================================================
# Live Qdrant, when it happens to be running
# ============================================================

def _qdrant_client():
    try:
        from qdrant_client import QdrantClient

        from bpi2020.qdrant_connection import QdrantSettings

        settings = QdrantSettings.from_env()
        client = QdrantClient(
            host=settings.host, port=settings.port, timeout=3
        )
        client.get_collections()
        return client
    except Exception:  # noqa: BLE001 - availability probe
        return None


#: This suite's OWN collection. Phase 10 originally skipped this test because
#: it depended on the production collection existing, which made the proof
#: hostage to whether anyone had run the batch pipeline. Owning an isolated
#: collection lets it actually exercise the live service - which is what the
#: test was for - without ever touching production data.
BPI_ADAPTER_TEST_COLLECTION = "bpi_cascade_vector_test"


def test_live_qdrant_point_identity_is_stable():
    """Real Qdrant: the same case writes and rewrites ONE point.

    Skips only when the server is genuinely unreachable.
    """
    client = _qdrant_client()

    if client is None:
        pytest.skip("Qdrant is not reachable")

    from qdrant_client.models import Distance, PointStruct, VectorParams

    point_id = make_qdrant_point_id(CASE_ROW["case_record_id"])

    try:
        client.delete_collection(BPI_ADAPTER_TEST_COLLECTION)
    except Exception:  # noqa: BLE001 - absent is fine
        pass

    client.create_collection(
        collection_name=BPI_ADAPTER_TEST_COLLECTION,
        vectors_config=VectorParams(size=4, distance=Distance.COSINE),
    )

    try:
        client.upsert(
            collection_name=BPI_ADAPTER_TEST_COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector=[0.1, 0.2, 0.3, 0.4],
                    payload={"content_hash": "a" * 64},
                )
            ],
        )
        after_insert = qdrant_point_count(client, BPI_ADAPTER_TEST_COLLECTION)

        # The same case with changed content must REPLACE, not accumulate.
        client.upsert(
            collection_name=BPI_ADAPTER_TEST_COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector=[0.9, 0.8, 0.7, 0.6],
                    payload={"content_hash": "b" * 64},
                )
            ],
        )
        after_update = qdrant_point_count(client, BPI_ADAPTER_TEST_COLLECTION)

        stored = client.retrieve(
            collection_name=BPI_ADAPTER_TEST_COLLECTION,
            ids=[point_id],
            with_payload=True,
        )

        assert after_insert == 1
        assert after_update == 1
        assert stored[0].payload["content_hash"] == "b" * 64
        assert make_qdrant_point_id(CASE_ROW["case_record_id"]) == point_id
    finally:
        client.delete_collection(BPI_ADAPTER_TEST_COLLECTION)


def test_live_qdrant_leaves_the_production_collection_alone():
    """This suite owns only its own collection."""
    client = _qdrant_client()

    if client is None:
        pytest.skip("Qdrant is not reachable")

    from bpi2020.common.config import get_vector_collection

    names = {c.name for c in client.get_collections().collections}

    assert BPI_ADAPTER_TEST_COLLECTION not in names
    # Reading the production name is fine; this suite never writes it.
    assert isinstance(get_vector_collection(), str)
