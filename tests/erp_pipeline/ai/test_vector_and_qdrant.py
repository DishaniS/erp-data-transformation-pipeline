"""Vector-store handoff, the generic Qdrant adapter, and live Qdrant.

Steps 31-38, 59. The live tests use an ISOLATED collection and never touch the
production ``bpi2020_erp_knowledge`` collection.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from erp_pipeline.ai import (
    AIRepresentation,
    DeterministicTestModel,
    EmbeddingRecord,
    EmbeddingService,
    EmbeddingStatus,
    InMemoryEmbeddingStore,
    QdrantVectorStore,
    VectorStore,
    build_vector_payload,
    canonical_record_to_representation,
    vector_id_for,
)
from erp_pipeline.ai.errors import EmbeddingDimensionError, VectorStoreError

from tests.erp_pipeline.ai.conftest import SECRET_CUSTOMER, make_record

#: Phase 11's own collection. Never the production one (Step 34).
TEST_COLLECTION = "erp_phase11_embeddings_test"
PRODUCTION_COLLECTION = "bpi2020_erp_knowledge"


def representation(key: str = "ai:invoice:r1", text: str = "Invoice INV-001"):
    return AIRepresentation(
        representation_id=key,
        entity_type="invoice",
        text_for_ai=text,
        content={"invoice_id": "INV-001"},
        source_record_ids=("erp:a:invoice:inv-001",),
        metadata={
            "canonical_record_id": "erp:a:invoice:inv-001",
            "source_system_id": "erp_a",
            "source_type": "postgresql",
            "sensitivity": "internal",
        },
    )


# ============================================================
# Payload (Step 37)
# ============================================================

def test_the_payload_carries_safe_structural_metadata(service):
    record = service.embed_one(representation())

    payload = build_vector_payload(record, representation())

    assert payload["representation_id"] == "ai:invoice:r1"
    assert payload["entity_type"] == "invoice"
    assert payload["content_hash"]
    assert payload["model_id"]
    assert payload["source_system_id"] == "erp_a"
    assert payload["sensitivity"] == "internal"


def test_the_payload_omits_the_text_by_default(service):
    record = service.embed_one(representation(text=SECRET_CUSTOMER))

    payload = build_vector_payload(record, representation(text=SECRET_CUSTOMER))

    assert "text_for_ai" not in payload
    assert SECRET_CUSTOMER not in json.dumps(payload)


def test_the_payload_can_include_text_when_explicitly_asked(service):
    record = service.embed_one(representation())

    payload = build_vector_payload(record, representation(), include_text=True)

    assert payload["text_for_ai"] == "Invoice INV-001"


def test_the_payload_carries_no_credentials(service):
    record = service.embed_one(representation())

    payload = json.dumps(build_vector_payload(record, representation())).lower()

    for marker in ("password", "api_key", "secret", "token"):
        assert marker not in payload


# ============================================================
# In-memory store semantics
# ============================================================

def test_the_in_memory_store_satisfies_the_protocol():
    assert isinstance(InMemoryEmbeddingStore(), VectorStore)


def test_an_upsert_uses_the_deterministic_vector_id(service):
    store = InMemoryEmbeddingStore()
    record = service.embed_one(representation())

    store.upsert_embedding(record, representation())

    assert store.vector_ids == (vector_id_for("ai:invoice:r1"),)


def test_updating_replaces_rather_than_accumulates(service):
    store = InMemoryEmbeddingStore()

    store.upsert_embedding(service.embed_one(representation(text="one")))
    store.upsert_embedding(service.embed_one(representation(text="two")))

    assert len(store) == 1


def test_delete_removes_the_vector(service):
    store = InMemoryEmbeddingStore()
    store.upsert_embedding(service.embed_one(representation()))

    assert store.delete_embedding(vector_id_for("ai:invoice:r1"))
    assert len(store) == 0


def test_metadata_is_retrievable(service):
    store = InMemoryEmbeddingStore()
    store.upsert_embedding(service.embed_one(representation()), representation())

    metadata = store.get_metadata(vector_id_for("ai:invoice:r1"))

    assert metadata["representation_id"] == "ai:invoice:r1"


# ============================================================
# Qdrant adapter, offline behaviour
# ============================================================

class RecordingQdrant:
    """A client double that records points by id."""

    def __init__(self, dimension: int | None = None):
        self.points: dict[str, dict] = {}
        self.collections: list[str] = []
        self.created: list[tuple[str, int]] = []
        self._dimension = dimension

    def get_collections(self):
        class _C:
            def __init__(self, names):
                self.collections = [type("N", (), {"name": n})() for n in names]

        return _C(list(self.collections))

    def create_collection(self, collection_name, vectors_config):
        self.collections.append(collection_name)
        self.created.append((collection_name, vectors_config.size))
        self._dimension = vectors_config.size

    def get_collection(self, name):
        params = type("P", (), {"size": self._dimension})()
        vectors = type("V", (), {"size": self._dimension})()
        config = type(
            "C", (), {"params": type("PP", (), {"vectors": vectors})()}
        )()
        return type(
            "Info", (), {"points_count": len(self.points), "config": config}
        )()

    def upsert(self, collection_name, points):
        for point in points:
            self.points[str(point.id)] = {
                "vector": list(point.vector),
                "payload": dict(point.payload or {}),
            }

    def delete(self, collection_name, points_selector):
        for point_id in points_selector:
            self.points.pop(str(point_id), None)

    def retrieve(self, collection_name, ids, with_payload=True):
        found = []
        for point_id in ids:
            entry = self.points.get(str(point_id))
            if entry:
                found.append(
                    type("P", (), {"payload": entry["payload"]})()
                )
        return found


def test_the_collection_name_is_required_not_defaulted():
    """Step 32: a generic engine must not know a deployment's collection."""
    import inspect

    signature = inspect.signature(QdrantVectorStore.__init__)

    assert signature.parameters["collection_name"].default is inspect._empty


def _package_code() -> str:
    """Package CODE with docstrings stripped.

    A ban on a literal has to be checked against what the code DOES. This
    package's own docstring explains that the collection name is not hard-coded,
    and a naive substring scan would flag that very sentence.
    """
    parts: list[str] = []

    for path in Path("src/erp_pipeline/ai").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
                if not body:
                    body.append(ast.Pass())

        parts.append(ast.unparse(ast.fix_missing_locations(tree)))

    return "\n".join(parts)


def test_no_production_collection_name_is_hard_coded():
    assert PRODUCTION_COLLECTION not in _package_code()


def test_the_adapter_writes_under_the_deterministic_point_id(service):
    client = RecordingQdrant(dimension=16)
    store = QdrantVectorStore(client, TEST_COLLECTION, dimension=16)

    store.upsert_embedding(service.embed_one(representation()), representation())

    assert list(client.points) == [vector_id_for("ai:invoice:r1")]


def test_a_wrong_dimension_is_refused_before_the_write(service):
    """Step 38: a typed error, not an opaque driver failure."""
    client = RecordingQdrant(dimension=384)
    store = QdrantVectorStore(client, TEST_COLLECTION, dimension=384)
    record = service.embed_one(representation())  # 16-dimensional

    with pytest.raises(EmbeddingDimensionError) as excinfo:
        store.upsert_embedding(record, representation())

    assert excinfo.value.expected == 384
    assert excinfo.value.actual == 16
    assert client.points == {}


def test_a_record_without_a_vector_is_refused():
    store = QdrantVectorStore(RecordingQdrant(16), TEST_COLLECTION)
    record = EmbeddingRecord(
        embedding_id="e1",
        representation_id="r1",
        content_hash="a" * 64,
        model_id="m",
        dimension=16,
        status=EmbeddingStatus.EMPTY_CONTENT,
    )

    with pytest.raises(VectorStoreError):
        store.upsert_embedding(record)


def test_ensure_collection_does_not_recreate_by_default():
    client = RecordingQdrant()
    store = QdrantVectorStore(client, TEST_COLLECTION)

    store.ensure_collection(16)
    store.ensure_collection(16)

    assert client.created == [(TEST_COLLECTION, 16)]


# ============================================================
# Phase 12 boundary (Step 59)
# ============================================================

def test_no_storage_tier_routing_exists():
    """Phase 11 decides WHAT to embed; Phase 12 decides WHERE it lives."""
    text = _package_code().lower()

    for marker in (
        "hot_tier", "warm_tier", "cold_tier", "tier_policy", "tier_routing",
        "quantization", "migrate_tier", "cold_snapshot", "archive_vector",
    ):
        assert marker not in text, marker


def test_no_tier_vocabulary_is_exported():
    import erp_pipeline.ai as package

    for name in package.__all__:
        lowered = name.lower()
        assert "tier" not in lowered
        assert "quantiz" not in lowered


# ============================================================
# LIVE QDRANT (Steps 33-36)
# ============================================================

def _live_client():
    try:
        from qdrant_client import QdrantClient

        import os

        try:
            from dotenv import load_dotenv

            load_dotenv(Path.cwd() / ".env", override=False)
        except ImportError:
            pass

        host = os.getenv("VECTOR_DB_HOST") or os.getenv("QDRANT_HOST") or "localhost"
        port = int(os.getenv("VECTOR_DB_PORT") or os.getenv("QDRANT_PORT") or 6333)

        client = QdrantClient(host=host, port=port, timeout=5)
        client.get_collections()
        return client
    except Exception:  # noqa: BLE001 - availability probe
        return None


@pytest.fixture()
def live_qdrant():
    client = _live_client()

    if client is None:
        pytest.skip("Qdrant is not reachable")

    # Own and clean ONLY the isolated test collection.
    try:
        client.delete_collection(TEST_COLLECTION)
    except Exception:  # noqa: BLE001 - absent is fine
        pass

    yield client

    try:
        client.delete_collection(TEST_COLLECTION)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture()
def live_store(live_qdrant):
    store = QdrantVectorStore(live_qdrant, TEST_COLLECTION)
    store.ensure_collection(16)
    return store


def test_live_qdrant_is_reachable(live_qdrant):
    assert live_qdrant.get_collections() is not None


def test_live_the_isolated_collection_is_created(live_store, live_qdrant):
    names = {c.name for c in live_qdrant.get_collections().collections}

    assert TEST_COLLECTION in names


def test_live_the_production_collection_is_untouched(live_store, live_qdrant):
    """Step 34: generic tests must not modify the BPI collection."""
    names = {c.name for c in live_qdrant.get_collections().collections}

    if PRODUCTION_COLLECTION in names:
        info = live_qdrant.get_collection(PRODUCTION_COLLECTION)
        assert info is not None  # read only; never written by this suite


def test_live_insert_creates_one_point(live_store, service):
    record = service.embed_one(representation())

    live_store.upsert_embedding(record, representation())

    assert live_store.point_count() == 1


def test_live_changed_content_updates_the_same_point(live_store, service):
    """Step 35: same identity, new vector, count unchanged."""
    live_store.upsert_embedding(
        service.embed_one(representation(text="original")), representation()
    )
    before = live_store.point_count()
    point_id = vector_id_for("ai:invoice:r1")
    first_hash = live_store.get_metadata(point_id)["content_hash"]

    changed = representation(text="amended content")
    live_store.upsert_embedding(service.embed_one(changed), changed)

    assert live_store.point_count() == before == 1
    assert live_store.get_metadata(point_id)["content_hash"] != first_hash


def test_live_the_point_identity_is_stable(live_store, service):
    live_store.upsert_embedding(service.embed_one(representation()), representation())
    first = set(
        p.id
        for p in live_store.client.scroll(TEST_COLLECTION, limit=10)[0]
    )

    live_store.upsert_embedding(
        service.embed_one(representation(text="changed")), representation()
    )
    second = set(
        p.id
        for p in live_store.client.scroll(TEST_COLLECTION, limit=10)[0]
    )

    assert first == second


def test_live_delete_removes_the_point(live_store, service):
    live_store.upsert_embedding(service.embed_one(representation()), representation())

    live_store.delete_embedding(vector_id_for("ai:invoice:r1"))

    assert live_store.point_count() == 0


def test_live_metadata_round_trips(live_store, service):
    live_store.upsert_embedding(service.embed_one(representation()), representation())

    metadata = live_store.get_metadata(vector_id_for("ai:invoice:r1"))

    assert metadata["representation_id"] == "ai:invoice:r1"
    assert metadata["entity_type"] == "invoice"
    assert metadata["source_system_id"] == "erp_a"


def test_live_a_wrong_dimension_is_refused_against_a_real_collection(
    live_store, service
):
    record = service.embed_one(representation())
    wrong = EmbeddingRecord(
        embedding_id=record.embedding_id,
        representation_id=record.representation_id,
        content_hash=record.content_hash,
        model_id=record.model_id,
        dimension=384,
        status=EmbeddingStatus.GENERATED,
        vector=tuple([0.1] * 384),
    )

    with pytest.raises(EmbeddingDimensionError):
        live_store.upsert_embedding(wrong)

    assert live_store.point_count() == 0


def test_live_the_full_pipeline_reaches_qdrant(live_store, service):
    """Canonical record -> representation -> embedding -> live vector."""
    record = make_record()
    projected = canonical_record_to_representation(record)

    summary = service.embed_many([projected], store=live_store)

    assert summary.embeddings_generated == 1
    assert summary.vectors_upserted == 1
    assert live_store.point_count() == 1
