"""Shared fixtures for the Phase 11 AI-ready knowledge and embedding tests."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import pytest

from erp_pipeline.ai import (
    DeterministicTestModel,
    EmbeddingService,
    canonical_record_to_representation,
)
from erp_pipeline.schemas.canonical_models import (
    CanonicalRecord,
    RecordProvenance,
    SourceReference,
)
from erp_pipeline.schemas.enums import SourceType

#: Planted in canonical records that legitimately carry them. They may reach an
#: embedding's TEXT - that is the job - but must never reach a log, a summary,
#: an error, a repr or a default serialization.
SECRET_CUSTOMER = "SECRET_CUSTOMER_93821"
SECRET_ACCOUNT = "SECRET_ACCOUNT_22118"
SECRET_EMAIL = "SECRET_EMAIL_44519"

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def make_record(
    entity_type: str = "invoice",
    key: str = "INV-001",
    source_system_id: str = "erp_a",
    source_type: SourceType = SourceType.POSTGRESQL,
    source_entity: str = "fin_invoice",
    **data: Any,
) -> CanonicalRecord:
    payload = data or {
        "invoice_id": key,
        "customer_id": "C001",
        "amount": Decimal("2500.50"),
        "currency": "LKR",
        "status": "approved",
    }

    return CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id=source_system_id,
            source_type=source_type,
            source_entity=source_entity,
        ),
        entity_type=entity_type,
        stable_source_key=key,
        normalized_data=payload,
        provenance=RecordProvenance(ingestion_method="batch_extract"),
        metadata={
            # Operational noise that must not reach the embedding text.
            "mapping_id": "p8.invoice",
            "transformation_engine_version": "1.0",
            "rules_applied": ["amount:trim"],
        },
    )


class FakePage:
    """Duck-typed stand-in for a Phase 6 ``ExtractedPage``."""

    def __init__(self, page_number: int, text: str) -> None:
        self.page_number = page_number
        self.text = text
        self.char_count = len(text)


class FakeFile:
    def __init__(self, content_hash: str, name: str = "doc.pdf") -> None:
        self.content_hash = content_hash
        self.file_id = content_hash
        self.original_filename = name


class FakeDocument:
    """Duck-typed stand-in for a Phase 6 ``ExtractedDocument``."""

    def __init__(self, pages: Sequence[FakePage], content_hash: str = "d" * 64) -> None:
        self.pages = tuple(pages)
        self.file = FakeFile(content_hash)
        self.page_count = len(pages)


@pytest.fixture()
def test_model() -> DeterministicTestModel:
    return DeterministicTestModel()


@pytest.fixture()
def service(test_model) -> EmbeddingService:
    return EmbeddingService(test_model)


@pytest.fixture()
def invoice_record() -> CanonicalRecord:
    return make_record()


@pytest.fixture()
def invoice_representation(invoice_record):
    return canonical_record_to_representation(invoice_record)


def model_is_cached() -> bool:
    """Whether the real MiniLM model is available locally, without a download."""
    hub = Path(os.path.expanduser("~")) / ".cache" / "huggingface" / "hub"
    return hub.exists() and any(
        hub.glob("models--sentence-transformers--all-MiniLM-L6-v2")
    )


requires_real_model = pytest.mark.skipif(
    not model_is_cached(),
    reason="the local all-MiniLM-L6-v2 model is not cached",
)
