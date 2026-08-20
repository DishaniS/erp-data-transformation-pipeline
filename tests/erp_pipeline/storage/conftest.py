"""Shared fixtures for the Phase 12 hybrid tiered vector storage tests."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.storage.cold_tier import (
    ColdArchiveTier,
    StaticKeyProvider,
    generate_key,
)
from erp_pipeline.storage.models import (
    BusinessCriticality,
    LatencyRequirement,
    StorageRecordMetadata,
    StorageRoutingContext,
    StorageTier,
)

#: Planted in payloads. An encrypted archive must never leak it to anything a
#: developer would casually look at: a repr, a header, or the raw file bytes.
SECRET_PAYLOAD_VALUE = "SECRET_STORAGE_PAYLOAD_71624"

#: Isolated collection prefix. Phase 12 tests must never open the production
#: BPI collection, so every live collection they create starts with this.
TEST_COLLECTION_PREFIX = "erp_phase12_test_"

DIMENSION = 8


def make_vector(seed: int, dimension: int = DIMENSION) -> tuple[float, ...]:
    """A deterministic unit-ish vector. No RNG, so failures reproduce."""
    raw = [((seed * (index + 3)) % 17) / 17.0 + 0.01 for index in range(dimension)]
    norm = sum(value * value for value in raw) ** 0.5

    return tuple(value / norm for value in raw)


def make_embedding(
    representation_id: str = "ai:invoice:erp_a_invoice_inv-001",
    seed: int = 1,
    dimension: int = DIMENSION,
    entity_type: str = "invoice",
    content_hash: str = "hash-1",
    status: EmbeddingStatus = EmbeddingStatus.GENERATED,
    vector: Sequence[float] | None = None,
) -> EmbeddingRecord:
    return EmbeddingRecord(
        embedding_id=f"emb:{representation_id}",
        representation_id=representation_id,
        content_hash=content_hash,
        model_id="test-model",
        dimension=dimension,
        status=status,
        entity_type=entity_type,
        vector=(
            tuple(vector)
            if vector is not None
            else (make_vector(seed, dimension) if status is EmbeddingStatus.GENERATED else None)
        ),
    )


def make_metadata(
    representation_id: str = "ai:invoice:erp_a_invoice_inv-001",
    tier: StorageTier = StorageTier.HOT,
    age_days: float = 1.0,
    dormancy_days: float = 1.0,
    access_count: int = 5,
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
    business_criticality: BusinessCriticality = BusinessCriticality.NORMAL,
    latency_requirement: LatencyRequirement = LatencyRequirement.STANDARD,
    retention_until: datetime | None = None,
    legal_hold: bool = False,
    now: datetime | None = None,
    **extra: Any,
) -> StorageRecordMetadata:
    moment = now or datetime.now(timezone.utc)

    return StorageRecordMetadata(
        representation_id=representation_id,
        embedding_id=f"emb:{representation_id}",
        vector_id="00000000-0000-0000-0000-000000000001",
        entity_type="invoice",
        dimension=DIMENSION,
        model_id="test-model",
        content_hash="hash-1",
        current_tier=tier,
        created_at=moment - timedelta(days=age_days),
        last_accessed_at=moment - timedelta(days=dormancy_days),
        access_count=access_count,
        sensitivity=sensitivity,
        business_criticality=business_criticality,
        latency_requirement=latency_requirement,
        retention_until=retention_until,
        legal_hold=legal_hold,
        tier_since=moment - timedelta(days=age_days),
        **extra,
    )


def make_context(**kwargs: Any) -> StorageRoutingContext:
    now = kwargs.pop("now", None) or datetime.now(timezone.utc)

    return make_metadata(now=now, **kwargs).to_context(now=now)


@pytest.fixture
def cold_tier(tmp_path: Path) -> ColdArchiveTier:
    """A cold tier with an injected key. The key never touches the archive dir."""
    return ColdArchiveTier(tmp_path / "archives", StaticKeyProvider(generate_key()))


@pytest.fixture(scope="session")
def qdrant_client():
    """A live Qdrant client, or a skip with a REASON that names the cause.

    Phase 12 must not report a silent pass, so a skip here is loud and states
    exactly what was unreachable.
    """
    pytest.importorskip("qdrant_client", reason="qdrant-client is not installed")

    from qdrant_client import QdrantClient

    host = os.environ.get("QDRANT_HOST", "localhost")
    port = int(os.environ.get("QDRANT_PORT", "6333"))

    try:
        client = QdrantClient(host=host, port=port, timeout=30)
        client.get_collections()
    except Exception as error:  # pragma: no cover - environment dependent
        pytest.skip(f"live Qdrant unreachable at {host}:{port}: {error!r}")

    return client
