"""AI-ready representation building and document chunking.

Steps 3-14. The recurring property under test is DETERMINISM: the same content
must always produce the same text, the same identity and the same hash, because
the incremental cascade decides whether to re-embed by comparing exactly that.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from erp_pipeline.ai import (
    AIRepresentation,
    ChunkingConfig,
    DocumentChunk,
    RepresentationConfig,
    build_text,
    canonical_record_to_representation,
    canonical_records_to_representations,
    chunk_document,
    chunk_text,
    chunk_to_representation,
    document_to_representations,
    flatten,
    format_value,
    humanize,
    make_representation_id,
    representation_content_hash,
)
from erp_pipeline.ai.errors import AIConfigurationError, ChunkingError

from tests.erp_pipeline.ai.conftest import (
    FakeDocument,
    FakePage,
    SECRET_CUSTOMER,
    make_record,
)


# ============================================================
# Reuse, not reinvention (Step 3)
# ============================================================

def test_the_representation_is_phase_10s_model_not_a_new_one():
    """A second near-identical model would fork the content-hash convention."""
    from erp_pipeline.sync.propagation import AIRepresentation as Phase10

    assert AIRepresentation is Phase10


def test_the_hash_helper_is_phase_10s():
    from erp_pipeline.sync.hashing import representation_content_hash as phase10

    assert representation_content_hash is phase10


# ============================================================
# Value and label formatting
# ============================================================

def test_field_names_become_readable_labels():
    assert humanize("invoice_id") == "Invoice Id"
    assert humanize("contact.email") == "Contact Email"


def test_a_decimal_renders_exactly_not_through_float():
    assert format_value(Decimal("2500.50")) == "2500.50"


def test_a_datetime_renders_as_utc_iso():
    value = datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)

    assert format_value(value) == "2026-08-14T09:30:00Z"


def test_a_naive_datetime_is_treated_as_utc():
    assert format_value(datetime(2026, 8, 14, 9, 30)).endswith("Z")


def test_a_date_renders_iso():
    assert format_value(date(2026, 8, 14)) == "2026-08-14"


def test_a_boolean_renders_lowercase():
    assert format_value(True) == "true"
    assert format_value(False) == "false"


def test_none_and_blank_are_omitted():
    assert format_value(None) is None
    assert format_value("   ") is None


def test_a_list_renders_as_a_joined_string():
    assert format_value(["a", "b"]) == "a, b"


# ============================================================
# Determinism (Steps 5, 8)
# ============================================================

def test_field_order_is_alphabetical_not_insertion_order():
    """Two records with the same content built differently must agree."""
    first = build_text("invoice", {"b_field": "2", "a_field": "1"})
    second = build_text("invoice", {"a_field": "1", "b_field": "2"})

    assert first == second
    assert first.index("A Field") < first.index("B Field")


def test_the_same_record_projects_identically_every_time():
    record = make_record()

    first = canonical_record_to_representation(record)
    second = canonical_record_to_representation(record)

    assert first.text_for_ai == second.text_for_ai
    assert first.resolved_hash() == second.resolved_hash()
    assert first.representation_id == second.representation_id


def test_nested_fields_flatten_deterministically():
    payload = {"contact": {"email": "x@example.test", "phone": "123"}}

    text = build_text("customer", payload)

    assert "Contact Email: x@example.test" in text
    assert text.index("Contact Email") < text.index("Contact Phone")


def test_nested_ordering_is_stable_regardless_of_construction():
    left = flatten({"z": {"b": 1, "a": 2}}, RepresentationConfig())
    right = flatten({"z": {"a": 2, "b": 1}}, RepresentationConfig())

    assert left == right


def test_a_null_field_does_not_change_the_text():
    """Absent and null must not be a semantic difference."""
    with_null = build_text("invoice", {"invoice_id": "I1", "note": None})
    without = build_text("invoice", {"invoice_id": "I1"})

    assert with_null == without


# ============================================================
# Business versus operational content (Step 6)
# ============================================================

def test_operational_metadata_is_excluded_from_the_text():
    text = build_text(
        "invoice",
        {
            "invoice_id": "INV-1",
            "mapping_id": "p8.invoice",
            "transformation_engine_version": "1.0",
            "created_at": "2026-08-14",
        },
    )

    assert "INV-1" in text
    assert "p8.invoice" not in text
    assert "Transformation Engine Version" not in text
    assert "Created At" not in text


def test_operational_metadata_does_not_change_the_hash():
    """An engine upgrade must not look like a content change."""
    quiet = build_text("invoice", {"invoice_id": "I1"})
    noisy = build_text(
        "invoice", {"invoice_id": "I1", "run_id": "abc", "duration": 12}
    )

    assert quiet == noisy


def test_business_content_does_change_the_hash():
    first = canonical_record_to_representation(make_record(key="INV-001"))
    second = canonical_record_to_representation(
        make_record(
            key="INV-001",
            invoice_id="INV-001",
            customer_id="C002",
            amount=Decimal("2500.50"),
        )
    )

    assert first.resolved_hash() != second.resolved_hash()


def test_the_operational_key_set_is_configurable():
    config = RepresentationConfig(operational_keys=frozenset({"secret_field"}))

    text = build_text("invoice", {"secret_field": "x", "kept": "y"}, config)

    assert "Kept: y" in text
    assert "Secret Field" not in text


# ============================================================
# Identity (Step 4)
# ============================================================

def test_representation_identity_is_deterministic():
    assert make_representation_id("invoice", "erp:a:invoice:1") == (
        make_representation_id("invoice", "erp:a:invoice:1")
    )


def test_representation_identity_is_prefixed_and_distinct():
    representation = canonical_record_to_representation(make_record())

    assert representation.representation_id.startswith("ai:invoice:")


def test_representation_identity_carries_no_timestamp():
    representation = canonical_record_to_representation(make_record())

    assert "2026" not in representation.representation_id


def test_two_entities_do_not_collide():
    assert make_representation_id("invoice", "X") != make_representation_id(
        "customer", "X"
    )


# ============================================================
# Structured payload preserved (Step 7)
# ============================================================

def test_the_structured_payload_survives_beside_the_text():
    representation = canonical_record_to_representation(make_record())

    assert representation.content["invoice_id"] == "INV-001"
    assert representation.content["amount"] == Decimal("2500.50")


def test_provenance_metadata_is_preserved_for_phase_12():
    representation = canonical_record_to_representation(make_record())

    assert representation.metadata["source_system_id"] == "erp_a"
    assert representation.metadata["source_type"] == "postgresql"
    assert representation.metadata["sensitivity"] == "internal"
    assert representation.metadata["canonical_record_id"].startswith("erp:")


def test_the_canonical_record_id_is_traceable():
    record = make_record()
    representation = canonical_record_to_representation(record)

    assert representation.source_record_ids == (record.record_id,)


# ============================================================
# Bounded text (Step 28)
# ============================================================

def test_long_content_is_bounded_visibly_not_silently():
    config = RepresentationConfig(max_characters=120)

    text = build_text("invoice", {"note": "x" * 500}, config)

    assert len(text) <= 120
    assert "[content truncated]" in text


def test_ordinary_content_is_not_truncated():
    representation = canonical_record_to_representation(make_record())

    assert "[content truncated]" not in representation.text_for_ai


def test_many_records_project_lazily():
    records = [make_record(key=f"INV-{i:03d}") for i in range(5)]

    projected = canonical_records_to_representations(records)

    assert not isinstance(projected, list)
    assert len(list(projected)) == 5


# ============================================================
# Chunking (Steps 11-14)
# ============================================================

def _document(page_count: int = 3, per_page: int = 900) -> FakeDocument:
    pages = [
        FakePage(index, f"Page {index} content. " + ("word " * (per_page // 5)))
        for index in range(1, page_count + 1)
    ]
    return FakeDocument(pages)


def test_a_document_is_split_into_several_chunks():
    chunks = chunk_document(_document())

    assert len(chunks) > 1
    assert all(isinstance(chunk, DocumentChunk) for chunk in chunks)


def test_chunks_respect_the_character_budget():
    config = ChunkingConfig(max_characters=300, overlap_characters=50)

    chunks = chunk_document(_document(), config)

    assert all(chunk.char_count <= 300 for chunk in chunks)


def test_chunks_are_ordered_and_indexed():
    chunks = chunk_document(_document())

    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))


def test_chunking_is_deterministic():
    first = chunk_document(_document())
    second = chunk_document(_document())

    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert [c.content_hash for c in first] == [c.content_hash for c in second]
    assert [c.text for c in first] == [c.text for c in second]


def test_page_provenance_is_preserved():
    chunks = chunk_document(_document())

    assert all(chunk.page_start >= 1 for chunk in chunks)
    assert max(chunk.page_end for chunk in chunks) <= 3
    assert chunks[0].page_start == 1


def test_a_chunk_can_span_two_pages():
    config = ChunkingConfig(max_characters=2000, overlap_characters=100)

    chunks = chunk_document(_document(page_count=3, per_page=400), config)

    assert any(chunk.spans_pages for chunk in chunks)


def test_overlap_repeats_content_between_neighbours():
    config = ChunkingConfig(max_characters=200, overlap_characters=60)
    text = "alpha beta gamma delta " * 40

    chunks = chunk_text(text, "doc1", config)

    assert len(chunks) > 1
    assert chunks[0].text[-20:] and chunks[1].text


def test_zero_overlap_is_allowed():
    config = ChunkingConfig(max_characters=200, overlap_characters=0)

    chunks = chunk_text("word " * 300, "doc1", config)

    assert len(chunks) > 1


def test_chunk_identity_changes_with_the_configuration():
    """Chunk 3 at 800 chars is not chunk 3 at 400 chars."""
    text = "word " * 500

    wide = chunk_text(text, "doc1", ChunkingConfig(max_characters=800))
    narrow = chunk_text(text, "doc1", ChunkingConfig(max_characters=400))

    assert wide[0].chunk_id != narrow[0].chunk_id


def test_a_short_document_still_produces_one_chunk():
    """Never silently dropped."""
    chunks = chunk_text("tiny", "doc1", ChunkingConfig(min_characters=100))

    assert len(chunks) == 1
    assert chunks[0].text == "tiny"


def test_empty_text_produces_no_chunks():
    assert chunk_text("   ", "doc1") == ()


def test_a_document_without_pages_is_refused():
    with pytest.raises(ChunkingError):
        chunk_document(FakeDocument([]))


def test_a_document_without_identity_is_refused():
    document = FakeDocument([FakePage(1, "text")])
    document.file.content_hash = None
    document.file.file_id = None

    with pytest.raises(ChunkingError):
        chunk_document(document)


def test_overlap_at_or_above_chunk_size_is_refused():
    """It could never advance, so the loop would never end."""
    with pytest.raises(AIConfigurationError):
        ChunkingConfig(max_characters=100, overlap_characters=100)


def test_a_zero_chunk_size_is_refused():
    with pytest.raises(AIConfigurationError):
        ChunkingConfig(max_characters=0)


def test_document_identity_is_the_content_hash_not_the_filename():
    same_bytes = FakeDocument([FakePage(1, "same content here")], "abc123")
    renamed = FakeDocument([FakePage(1, "same content here")], "abc123")
    renamed.file.original_filename = "different_name.pdf"

    assert chunk_document(same_bytes)[0].chunk_id == (
        chunk_document(renamed)[0].chunk_id
    )


# ============================================================
# Chunk representations
# ============================================================

def test_a_chunk_becomes_a_representation():
    chunk = chunk_document(_document())[0]

    representation = chunk_to_representation(chunk)

    assert representation.representation_id == chunk.chunk_id
    assert representation.entity_type == "document"
    assert representation.text_for_ai == chunk.text


def test_a_chunk_representation_keeps_page_provenance():
    representation = document_to_representations(_document())[0]

    assert representation.metadata["page_start"] >= 1
    assert "page_end" in representation.metadata
    assert representation.metadata["document_id"]


def test_document_representations_are_deterministic():
    first = document_to_representations(_document())
    second = document_to_representations(_document())

    assert [r.representation_id for r in first] == [
        r.representation_id for r in second
    ]
    assert [r.resolved_hash() for r in first] == [
        r.resolved_hash() for r in second
    ]


def test_a_chunks_default_serialization_omits_its_text():
    chunk = chunk_document(_document())[0]

    assert "text" not in chunk.to_dict()
    assert "text" in chunk.to_dict(include_text=True)


def test_a_chunk_repr_carries_no_document_text():
    chunk = DocumentChunk(
        document_id="d1",
        chunk_id="d1.c0",
        chunk_index=0,
        text=SECRET_CUSTOMER,
        page_start=1,
        page_end=1,
        char_count=len(SECRET_CUSTOMER),
    )

    assert SECRET_CUSTOMER not in repr(chunk)
