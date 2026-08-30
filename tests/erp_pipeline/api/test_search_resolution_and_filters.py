"""ACCEPTANCE: a search hit must resolve to the record it came from.

THE DOWNSTREAM INTEGRATION PROOF
--------------------------------
This is the contract every downstream consumer depends on:

    CanonicalRecord
        -> AIRepresentation
        -> Embedding
        -> Storage
        -> POST /v1/search
        -> SearchHit.canonical_record_id
        -> GET /v1/records/{canonical_record_id}
        -> the SAME canonical record

Before this fix the chain broke at the last two steps: a hit carried an ``ai:``
id that ``GET /v1/records`` could not accept, and nothing in storage held the
``erp:`` id it would have needed. A governance or RAG consumer got a score and
an identifier that resolved to nothing.

The second half of this module proves filters actually narrow the result, which
was published in the API contract and silently ignored.
"""

from __future__ import annotations

import pytest

from erp_pipeline.ai.embedding import DeterministicTestModel
from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    OrchestrationService,
    PipelineServices,
)
from erp_pipeline.schemas.canonical_models import CanonicalRecord, SourceReference
from erp_pipeline.schemas.enums import SensitivityLevel, SourceType
from erp_pipeline.storage.migration import TierSet
from erp_pipeline.storage.service import StorageService
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync.propagation import InMemoryCanonicalStore

DIMENSION = 16

#: Shared across the test suite so any two fixtures that both configure it
#: tokenize the same value to the same token - a real secret would too, but
#: this one is fixed so a test can predict/recompute a token to assert
#: against, which a randomly generated one could not do reproducibly.
TEST_FILTER_TOKEN_SECRET = "test-only-filter-token-secret-never-a-real-key"


class InProcessTier:
    """A tier that stores vectors in a dict and honours a Qdrant-style filter."""

    dimension = DIMENSION

    def __init__(self) -> None:
        self.vectors: dict[str, tuple] = {}
        self.payloads: dict[str, dict] = {}
        self.by_vector_id: dict[str, str] = {}

    def upsert(self, record, payload=None):
        self.vectors[record.representation_id] = record.vector
        self.payloads[record.representation_id] = dict(payload or {})
        vector_id = (payload or {}).get("vector_id") or record.representation_id
        self.by_vector_id[vector_id] = record.representation_id
        return True

    def get_vector(self, representation_id):
        return self.vectors.get(representation_id)

    def exists(self, representation_id):
        return representation_id in self.vectors

    def delete(self, representation_id):
        return self.vectors.pop(representation_id, None) is not None

    def count(self):
        return len(self.vectors)

    def search(self, vector, limit=5, query_filter=None):
        """Cosine-free stand-in: rank by dot product, honour the filter."""
        scored = []

        for representation_id, stored in self.vectors.items():
            payload = self.payloads[representation_id]

            if query_filter is not None:
                if not all(
                    payload.get(condition.key) == condition.match.value
                    for condition in query_filter.must
                ):
                    continue

            score = sum(a * b for a, b in zip(vector, stored))
            scored.append((payload["vector_id"], score))

        scored.sort(key=lambda pair: -pair[1])

        return scored[:limit]

    def fetch(self, query_filter=None, limit=100):
        """Filter-only match, no vector: the ``scroll`` stand-in for tests."""
        matched = []

        for payload in self.payloads.values():
            if query_filter is not None:
                if not all(
                    payload.get(condition.key) == condition.match.value
                    for condition in query_filter.must
                ):
                    continue

            matched.append((payload["vector_id"], dict(payload)))

        return matched[:limit]


def canonical_record(
    key: str,
    entity: str = "invoice",
    system: str = "finance_erp",
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
) -> CanonicalRecord:
    return CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id=system,
            source_type=SourceType.POSTGRESQL,
            source_entity=f"fin_{entity}",
            source_record_key=key,
        ),
        entity_type=entity,
        stable_source_key=key,
        normalized_data={f"{entity}_id": key, "amount": 100.0},
        sensitivity=sensitivity,
    )


class PatchedStorage(StorageService):
    """StorageService whose tier learns each record's vector id.

    The in-process tier needs the vector id in its payload to answer a search
    the way a real Qdrant point would; the real payload builder does not carry
    it because Qdrant uses it as the point id rather than payload data.
    """

    def store(self, record, profile=None, **kwargs):
        metadata, decision = super().store(record, profile=profile, **kwargs)

        tier = self.tiers.get(metadata.current_tier)
        tier.payloads[metadata.representation_id]["vector_id"] = metadata.vector_id
        tier.by_vector_id[metadata.vector_id] = metadata.representation_id

        return metadata, decision


@pytest.fixture
def corpus():
    """Three records: two invoices from different ERPs, one customer."""
    return [
        canonical_record("INV-001", "invoice", "finance_erp"),
        canonical_record("INV-002", "invoice", "other_erp"),
        canonical_record(
            "CUS-044",
            "customer",
            "finance_erp",
            sensitivity=SensitivityLevel.CONFIDENTIAL,
        ),
    ]


@pytest.fixture
def client(corpus, tmp_path):
    from fastapi.testclient import TestClient

    records = InMemoryCanonicalStore()
    storage = PatchedStorage(
        hot=InProcessTier(), state_store=InMemoryTierStateStore()
    )
    embedding = EmbeddingService(DeterministicTestModel(dimension=DIMENSION))

    for record in corpus:
        records.upsert(record)
        representation = canonical_record_to_representation(record)
        storage.store(embedding.embed_one(representation))

    services = PipelineServices(
        records=records, storage=storage, embedding=embedding
    )
    orchestration = OrchestrationService(
        services=services,
        job_store=InMemoryJobStore(),
        executor=InlineJobExecutor(),
    )
    app = create_app(
        settings=ApiSettings(upload_dir=tmp_path / "uploads"),
        orchestration=orchestration,
    )

    with TestClient(app) as test_client:
        yield test_client


def search(client, **body):
    body.setdefault("query", "invoice")
    body.setdefault("top_k", 10)

    return client.post("/v1/search", json=body)


# ============================================================
# ACCEPTANCE: search -> canonical record
# ============================================================


def test_a_search_hit_carries_a_canonical_record_id(client):
    response = search(client)

    assert response.status_code == 200
    hits = response.json()["hits"]
    assert hits

    for hit in hits:
        assert hit["canonical_record_id"], hit
        assert hit["canonical_record_id"].startswith("erp:")


def test_the_canonical_id_resolves_through_the_record_endpoint(client):
    """THE integration proof."""
    hit = search(client).json()["hits"][0]

    record = client.get(f"/v1/records/{hit['canonical_record_id']}")

    assert record.status_code == 200
    assert record.json()["record_id"] == hit["canonical_record_id"]


def test_the_resolved_record_is_the_one_the_vector_came_from(client, corpus):
    hit = search(client).json()["hits"][0]
    payload = client.get(f"/v1/records/{hit['canonical_record_id']}").json()

    expected = next(
        record for record in corpus
        if record.record_id == hit["canonical_record_id"]
    )

    assert payload["entity_type"] == expected.entity_type
    assert payload["data"] == expected.normalized_data


def test_every_hit_resolves_not_merely_the_first(client):
    for hit in search(client).json()["hits"]:
        response = client.get(f"/v1/records/{hit['canonical_record_id']}")

        assert response.status_code == 200, hit


def test_the_canonical_id_is_not_derived_from_the_representation_id(client):
    """Normalization is lossy, so a derived id would be wrong."""
    hit = search(client).json()["hits"][0]

    assert hit["representation_id"].startswith("ai:")
    assert hit["canonical_record_id"] != hit["representation_id"]
    assert ":" in hit["canonical_record_id"].split(":", 2)[2]


def test_record_id_mirrors_canonical_record_id_for_older_consumers(client):
    """Additive change: the previously-published field keeps working, and now
    actually resolves."""
    hit = search(client).json()["hits"][0]

    assert hit["record_id"] == hit["canonical_record_id"]


def test_a_hit_still_carries_no_vector(client):
    hit = search(client).json()["hits"][0]

    assert "vector" not in hit
    assert "embedding" not in hit


def test_hit_metadata_carries_provenance(client):
    hit = search(client).json()["hits"][0]

    assert hit["metadata"]["source_system_id"]
    assert hit["metadata"]["source_entity"]
    assert hit["metadata"]["sensitivity"]


# ============================================================
# ACCEPTANCE: filters
# ============================================================


def test_an_unfiltered_search_returns_every_entity_type(client):
    entity_types = {hit["entity_type"] for hit in search(client).json()["hits"]}

    assert entity_types == {"invoice", "customer"}


def test_filtering_by_entity_type_returns_only_that_type(client):
    body = search(client, filters={"entity_type": "invoice"}).json()

    assert body["hits"]
    assert {hit["entity_type"] for hit in body["hits"]} == {"invoice"}


def test_filtering_actually_reduces_the_result_set(client):
    unfiltered = len(search(client).json()["hits"])
    filtered = len(search(client, filters={"entity_type": "invoice"}).json()["hits"])

    assert filtered < unfiltered


def test_filtering_by_source_system(client):
    body = search(client, filters={"source_system_id": "finance_erp"}).json()

    assert body["hits"]
    for hit in body["hits"]:
        assert hit["metadata"]["source_system_id"] == "finance_erp"


def test_filtering_by_source_entity(client):
    body = search(client, filters={"source_entity": "fin_customer"}).json()

    assert len(body["hits"]) == 1
    assert body["hits"][0]["entity_type"] == "customer"


def test_filtering_by_sensitivity(client):
    body = search(client, filters={"sensitivity": "confidential"}).json()

    assert len(body["hits"]) == 1
    assert body["hits"][0]["metadata"]["sensitivity"] == "confidential"


def test_two_filters_intersect(client):
    body = search(
        client,
        filters={"entity_type": "invoice", "source_system_id": "finance_erp"},
    ).json()

    assert len(body["hits"]) == 1
    assert body["hits"][0]["canonical_record_id"] == "erp:finance_erp:invoice:inv-001"


def test_a_filter_matching_nothing_returns_no_hits(client):
    body = search(client, filters={"entity_type": "purchase_order"}).json()

    assert body["hits"] == []


def test_the_applied_filters_are_echoed_back(client):
    body = search(client, filters={"entity_type": "invoice"}).json()

    assert body["filters_applied"] == {"entity_type": "invoice"}


def test_no_filters_echoes_an_empty_mapping(client):
    assert search(client).json()["filters_applied"] == {}


# ============================================================
# ACCEPTANCE: an unsupported filter is refused
# ============================================================


def test_an_unknown_filter_field_is_rejected(client):
    """Silently ignoring it would return a plausible-looking wrong answer."""
    response = search(client, filters={"colour": "red"})

    assert response.status_code == 422


def test_the_rejection_names_the_offending_field_and_the_supported_set(client):
    body = search(client, filters={"colour": "red"}).json()

    message = body["error"]["message"]
    assert "colour" in message
    assert "entity_type" in message
    assert body["error"]["detail"]["supported_filters"]


def test_an_invalid_enum_value_is_rejected(client):
    response = search(client, filters={"sensitivity": "top-secret"})

    assert response.status_code == 422


def test_an_empty_filter_value_is_rejected(client):
    assert search(client, filters={"entity_type": ""}).status_code == 422


def test_a_rejected_filter_returns_no_results_at_all(client):
    """It must fail, not quietly fall back to an unfiltered search."""
    body = search(client, filters={"colour": "red"}).json()

    assert "hits" not in body
    assert body["success"] is False


def test_a_valid_filter_alongside_an_unknown_one_is_still_rejected(client):
    response = search(
        client, filters={"entity_type": "invoice", "colour": "red"}
    )

    assert response.status_code == 422


# ============================================================
# Filtered hits still resolve
# ============================================================


def test_a_filtered_hit_also_resolves_to_its_record(client):
    hit = search(client, filters={"entity_type": "customer"}).json()["hits"][0]

    response = client.get(f"/v1/records/{hit['canonical_record_id']}")

    assert response.status_code == 200
    assert response.json()["entity_type"] == "customer"
