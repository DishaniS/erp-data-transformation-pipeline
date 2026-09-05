"""Phase 4 - retrieval that can name the ERP record it found.

Everything here goes through the PRODUCTION write path: real representations
built by ``ai.representation`` / ``ai.attached_documents``, real embedding
metadata via ``EmbeddingService._record``'s carry rules, real payloads from
``_payload_for``, and ``HybridVectorStore.store``. A test that hand-wrote a
payload would prove only that the test could write a payload.

The test that matters most is
``test_two_employees_sharing_one_certificate_are_filtered_apart``: Phase 3
stopped the two from overwriting each other's vector, and this is where that
separation either becomes usable or turns out to have been pointless.
"""

from __future__ import annotations

import dataclasses
import io
import json

import pytest

from erp_pipeline.ai.attached_documents import (
    DocumentAttachment,
    attached_document_to_representations,
)
from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.ai.representation import canonical_record_to_representation
from erp_pipeline.ai.service import CARRIED_IDENTITY_KEYS, _carried_identity
from erp_pipeline.ingestion.binary_assets import extract_binary_asset
from erp_pipeline.schemas.enums import (
    ContentKind,
    EntityKind,
    FieldDataType,
    SensitivityLevel,
    SourceType,
)
from erp_pipeline.schemas.source_models import SourceEntity, SourceField
from erp_pipeline.storage.filters import (
    FILTERABLE_FIELDS,
    PROVENANCE_ONLY_FIELDS,
    InvalidFilterValueError,
    SearchFilters,
    UnknownFilterFieldError,
)
from erp_pipeline.storage.hybrid_store import HybridVectorStore
from erp_pipeline.storage.migration import TierSet, _payload_for
from erp_pipeline.storage.models import StorageRecordMetadata, StorageTier
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync.hashing import vector_id_for
from erp_pipeline.transformation.models import SourceRecord
from erp_pipeline.transformation.source_native import SourceNativeTransformer

VECTOR = [0.1, 0.2, 0.3, 0.4]


# ======================================================================
# Harness
# ======================================================================


class FilterAwareTier:
    """A tier that honours a Qdrant-style filter, like the real one does.

    Matches the semantics that matter for these tests: a condition on a key the
    payload does not carry EXCLUDES the point, exactly as Qdrant's ``must``
    does. That is what makes the old-vector compatibility test meaningful.
    """

    #: The store checks HOT and WARM agree before writing; a tier has to be
    #: able to answer for itself.
    dimension = len(VECTOR)

    def __init__(self) -> None:
        self.points: list[tuple[str, dict]] = []
        self.received_filter = None

    def upsert(self, record, payload=None):
        # The PRODUCTION vector id, not a made-up one: ``_merge`` looks the
        # state row up by it, and a mismatch would silently strip every hit of
        # the provenance these tests exist to check.
        self.points.append(
            (vector_id_for(record.representation_id), dict(payload or {}))
        )
        return True

    def add(self, vector_id: str, payload: dict) -> None:
        self.points.append((vector_id, dict(payload)))

    def get_vector(self, representation_id):
        return tuple(VECTOR)

    def exists(self, representation_id):
        return True

    def delete(self, representation_id):
        return True

    def count(self):
        return len(self.points)

    def search(self, vector, limit=5, query_filter=None):
        self.received_filter = query_filter
        results = []

        for vector_id, payload in self.points:
            if query_filter is not None and not all(
                payload.get(condition.key) == condition.match.value
                for condition in query_filter.must
            ):
                continue

            results.append((vector_id, 0.9))

        return results[:limit]


def _field(name, data_type, primary=False):
    return SourceField(
        source_name=name,
        normalized_name=name,
        source_data_type="X",
        normalized_data_type=data_type,
        is_primary_key=primary,
        nullable=not primary,
    )


def _pdf(text: str) -> bytes:
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    document.new_page().insert_text((72, 96), text)
    payload = document.tobytes()
    document.close()

    return payload


def _png() -> bytes:
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (200, 100), "white").save(buffer, "PNG")

    return buffer.getvalue()


_METADATA_FIELDS = {f.name for f in dataclasses.fields(StorageRecordMetadata)}
_INT_FIELDS = set(PROVENANCE_ONLY_FIELDS)


def embedding_for(representation) -> EmbeddingRecord:
    """An embedding record carrying exactly what the real service carries."""
    return EmbeddingRecord(
        embedding_id=f"emb.{representation.representation_id}",
        representation_id=representation.representation_id,
        entity_type=representation.entity_type,
        content_hash=representation.content_hash or "h",
        model_id="test-model",
        dimension=len(VECTOR),
        status=EmbeddingStatus.GENERATED,
        vector=tuple(VECTOR),
        metadata=_carried_identity(representation, filter_token_secret=None),
    )


@pytest.fixture
def employees_entity() -> SourceEntity:
    return SourceEntity(
        entity_id="hr.employees",
        source_name="employees",
        normalized_name="employees",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("employee_id",),
        fields=(
            _field("employee_id", FieldDataType.STRING, primary=True),
            _field("full_name", FieldDataType.STRING),
            _field("department", FieldDataType.STRING),
            _field("birth_certificate", FieldDataType.BINARY),
            _field("employment_contract", FieldDataType.BINARY),
            _field("profile_photo", FieldDataType.BINARY),
        ),
    )


class Corpus:
    """Builds and indexes the employee corpus through the production path."""

    def __init__(self, entity: SourceEntity, tiers: str = "hot") -> None:
        self.entity = entity
        self.state = InMemoryTierStateStore()
        self.hot = FilterAwareTier() if tiers in ("hot", "both") else None
        self.warm = FilterAwareTier() if tiers in ("warm", "both") else None
        self.store = HybridVectorStore(
            TierSet(hot=self.hot, warm=self.warm), self.state
        )
        self.representations: dict[str, object] = {}

    def add_employee(self, employee_id: str, name: str, **blobs) -> None:
        rows = [
            SourceRecord.from_mapping(
                {
                    "employee_id": employee_id,
                    "full_name": name,
                    "department": "Finance",
                    **blobs,
                }
            )
        ]
        canonical = SourceNativeTransformer().transform_records(
            rows, self.entity, "legacy_hr", SourceType.POSTGRESQL
        ).records[0]

        self._index(canonical_record_to_representation(canonical))

        for field_name, payload in blobs.items():
            if payload is None:
                continue

            asset = extract_binary_asset(payload, field_name)

            if not asset.succeeded:
                continue

            attachment = DocumentAttachment(
                parent_record_id=canonical.record_id,
                source_system_id="legacy_hr",
                source_entity="employees",
                source_field=field_name,
                document_id=asset.document_id or "",
                business_key_name="employee_id",
                business_key_value=employee_id,
                document_type=field_name,
                media_type=asset.media_type,
            )

            for representation in attached_document_to_representations(
                asset.document, attachment
            ):
                self._index(representation)

    def _index(self, representation) -> None:
        self.representations[representation.representation_id] = representation
        # A warm-only corpus has no HOT backend, so the routing decision is
        # overridden rather than left to a policy that would pick HOT.
        override = (
            StorageTier.WARM if self.hot is None and self.warm is not None else None
        )
        self.store.store(
            embedding_for(representation),
            sensitivity=SensitivityLevel.INTERNAL,
            override=override,
            override_reason="tier pinned by the test harness" if override else None,
        )

    def find(self, **filters):
        return self.store.search(
            VECTOR, limit=50, filters=SearchFilters.from_mapping(filters)
        ).hits

    def keys(self, hits) -> set[str]:
        """(business key, content kind, document type) for each hit."""
        return {
            (
                h.state.business_key_value,
                h.state.content_kind,
                h.state.document_type,
            )
            for h in hits
        }


@pytest.fixture
def corpus(employees_entity) -> Corpus:
    """EMP001/2/3, with EMP002 and EMP003 sharing certificate bytes."""
    shared = _pdf("BIRTH CERTIFICATE Registrar General")
    built = Corpus(employees_entity)

    built.add_employee(
        "EMP001", "Sunil Bandara", birth_certificate=_pdf("BIRTH CERTIFICATE One")
    )
    built.add_employee(
        "EMP002",
        "Nimal Silva",
        birth_certificate=shared,
        employment_contract=_pdf("EMPLOYMENT CONTRACT Accountant"),
        profile_photo=_png(),
    )
    built.add_employee("EMP003", "Amal Perera", birth_certificate=shared)

    return built


# ======================================================================
# TEST A - the headline scenario
# ======================================================================


def test_only_emp002_certificate_chunks_come_back(corpus):
    """The EMP002 query, end to end. No EMP001 or EMP003 hit is acceptable."""
    hits = corpus.find(
        business_key_name="employee_id",
        business_key_value="EMP002",
        document_type="birth_certificate",
        content_kind="document_chunk",
    )

    assert hits
    assert {h.state.business_key_value for h in hits} == {"EMP002"}
    assert {h.state.document_type for h in hits} == {"birth_certificate"}
    assert {h.state.content_kind for h in hits} == {ContentKind.DOCUMENT_CHUNK.value}
    assert {h.state.parent_record_id for h in hits} == {
        "erp:legacy_hr:employees:emp002"
    }


def test_the_identity_filter_is_pushed_into_the_tier(corpus):
    """Server-side, not trimmed in Python afterwards."""
    corpus.find(business_key_value="EMP002")
    sent = corpus.hot.received_filter

    assert sent is not None
    assert {condition.key for condition in sent.must} == {"business_key_value"}
    assert sent.must[0].match.value == "EMP002"


# ======================================================================
# TEST B - THE CRITICAL ONE
# ======================================================================


def test_two_employees_sharing_one_certificate_are_filtered_apart(corpus):
    """Identical certificate bytes, two employees, one document_id.

    Phase 3 kept their vectors from overwriting each other. If the filter
    cannot tell them apart, that separation bought nothing: a query for EMP002
    would hand back a chunk that belongs to EMP003.
    """
    second = corpus.find(
        business_key_value="EMP002", content_kind="document_chunk"
    )
    third = corpus.find(
        business_key_value="EMP003", content_kind="document_chunk"
    )

    assert {h.state.parent_record_id for h in second} == {
        "erp:legacy_hr:employees:emp002"
    }
    assert {h.state.parent_record_id for h in third} == {
        "erp:legacy_hr:employees:emp003"
    }
    assert not {h.vector_id for h in second} & {h.vector_id for h in third}

    # They really are the same document - that is the whole difficulty.
    shared = {
        h.state.document_id
        for h in second + third
        if h.state.document_type == "birth_certificate"
    }
    assert len(shared) == 1


def test_filtering_by_the_shared_document_id_returns_both_attachments(corpus):
    """``document_id`` is content identity, so it legitimately spans employees."""
    certificate = next(
        h.state.document_id
        for h in corpus.find(
            business_key_value="EMP002", document_type="birth_certificate"
        )
    )
    hits = corpus.find(document_id=certificate)

    assert {h.state.business_key_value for h in hits} == {"EMP002", "EMP003"}


# ======================================================================
# TEST C / D - structured versus document
# ======================================================================


def test_the_structured_record_is_reachable_on_its_own(corpus):
    hits = corpus.find(
        content_kind="structured_record", business_key_value="EMP002"
    )

    assert len(hits) == 1
    assert hits[0].entity_type == "employees"
    assert hits[0].state.canonical_record_id == "erp:legacy_hr:employees:emp002"
    assert hits[0].state.document_type is None


def test_content_kind_separates_the_two_completely(corpus):
    structured = corpus.find(
        content_kind="structured_record", business_key_value="EMP002"
    )
    documents = corpus.find(
        content_kind="document_chunk", business_key_value="EMP002"
    )

    assert {h.state.content_kind for h in structured} == {"structured_record"}
    assert {h.state.content_kind for h in documents} == {"document_chunk"}
    assert not {h.vector_id for h in structured} & {h.vector_id for h in documents}


def test_an_unfiltered_query_still_returns_both_kinds(corpus):
    """The separation is a filter, not a partition of the index."""
    kinds = {
        h.state.content_kind for h in corpus.find(business_key_value="EMP002")
    }

    assert kinds == {"structured_record", "document_chunk"}


# ======================================================================
# TEST E / F - document type and source field
# ======================================================================


@pytest.mark.parametrize(
    "document_type", ["birth_certificate", "employment_contract"]
)
def test_document_type_selects_one_attachment(corpus, document_type):
    hits = corpus.find(business_key_value="EMP002", document_type=document_type)

    assert hits
    assert {h.state.document_type for h in hits} == {document_type}


def test_source_field_filters_independently_of_document_type(corpus):
    """They hold the same value today and are still two separate filters."""
    by_field = corpus.find(
        business_key_value="EMP002", source_field="birth_certificate"
    )
    by_type = corpus.find(
        business_key_value="EMP002", document_type="birth_certificate"
    )

    assert by_field
    assert {h.vector_id for h in by_field} == {h.vector_id for h in by_type}
    assert {h.state.source_field for h in by_field} == {"birth_certificate"}


def test_source_field_and_document_type_are_stored_separately(corpus):
    """Nothing derives one from the other at read time."""
    hit = corpus.find(
        business_key_value="EMP002", document_type="employment_contract"
    )[0]
    payload = _payload_for(hit.state)

    assert payload["source_field"] == "employment_contract"
    assert payload["document_type"] == "employment_contract"
    assert "source_field" in payload and "document_type" in payload


# ======================================================================
# TEST G - page and chunk provenance
# ======================================================================


def test_a_document_hit_reports_where_in_the_document_it_came_from(corpus):
    hit = corpus.find(
        business_key_value="EMP002", document_type="birth_certificate"
    )[0]

    assert hit.state.page_start == 1
    assert hit.state.page_end == 1
    assert hit.state.chunk_index == 0


def test_multi_page_chunks_keep_distinct_ordinals(employees_entity):
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()

    for page_number in range(3):
        page = document.new_page()

        for line in range(30):
            page.insert_text(
                (56, 60 + line * 24),
                f"CONTRACT PAGE {page_number + 1} CLAUSE {line + 1} "
                "the parties agree to the terms set out herein",
                fontsize=9,
            )

    payload = document.tobytes()
    document.close()

    built = Corpus(employees_entity)
    built.add_employee("EMP009", "Multi Page", employment_contract=payload)
    hits = built.find(
        business_key_value="EMP009", content_kind="document_chunk"
    )

    assert len(hits) > 1
    indexes = sorted(h.state.chunk_index for h in hits)
    assert indexes == list(range(len(hits)))
    assert all(h.state.page_start >= 1 for h in hits)
    assert all(h.state.page_end >= h.state.page_start for h in hits)


def test_a_structured_record_does_not_pretend_to_have_pages(corpus):
    hit = corpus.find(
        content_kind="structured_record", business_key_value="EMP002"
    )[0]

    assert hit.state.page_start is None
    assert hit.state.page_end is None
    assert hit.state.chunk_index is None
    assert "page_start" not in _payload_for(hit.state)


def test_page_ordinals_are_stored_as_integers(corpus):
    """Not stringified for the convenience of a filter that ignores them."""
    hit = corpus.find(
        business_key_value="EMP002", document_type="birth_certificate"
    )[0]
    payload = _payload_for(hit.state)

    assert isinstance(payload["page_start"], int)
    assert isinstance(payload["chunk_index"], int)


# ======================================================================
# TEST H - the allow-list stays closed
# ======================================================================


@pytest.mark.parametrize(
    "field", ["employee_ssn", "salary", "text_for_ai", "super_secret_field"]
)
def test_an_unregistered_filter_is_still_refused(field):
    with pytest.raises(UnknownFilterFieldError) as raised:
        SearchFilters.from_mapping({field: "x"})

    assert raised.value.unknown == (field,)


def test_provenance_fields_are_not_filterable(corpus):
    """Deliberate: they are returned with every hit and cannot be matched on."""
    for field in PROVENANCE_ONLY_FIELDS:
        assert field not in FILTERABLE_FIELDS

        with pytest.raises(UnknownFilterFieldError):
            SearchFilters.from_mapping({field: 1})


def test_content_kind_rejects_a_kind_that_does_not_exist():
    """The vocabulary is closed to the kinds that actually exist.

    ``schema`` was refused here until Phase 7 built schema representations, and
    is accepted now for the same reason it was refused then: the filter admits
    exactly what the system can return.
    """
    for absent in ("magic_schema", "nonsense", "uploaded_document"):
        with pytest.raises(InvalidFilterValueError):
            SearchFilters.from_mapping({"content_kind": absent})


def test_every_declared_content_kind_is_accepted():
    for kind in ContentKind:
        assert SearchFilters.from_mapping({"content_kind": kind.value})


# ======================================================================
# TEST I - HOT / WARM parity
# ======================================================================


def test_hot_and_warm_answer_a_filter_identically(employees_entity):
    certificate = _pdf("BIRTH CERTIFICATE Parity")
    hot_only = Corpus(employees_entity, tiers="hot")
    hot_only.add_employee("EMP002", "N", birth_certificate=certificate)
    hot_only.add_employee("EMP003", "A", birth_certificate=certificate)

    warm_only = Corpus(employees_entity, tiers="warm")
    warm_only.add_employee("EMP002", "N", birth_certificate=certificate)
    warm_only.add_employee("EMP003", "A", birth_certificate=certificate)

    query = {"business_key_value": "EMP002", "content_kind": "document_chunk"}

    assert hot_only.keys(hot_only.find(**query)) == warm_only.keys(
        warm_only.find(**query)
    )


def test_both_tiers_receive_the_same_filter(employees_entity):
    both = Corpus(employees_entity, tiers="both")
    both.add_employee("EMP002", "N", birth_certificate=_pdf("CERT"))
    # add_employee's default routing sends everything to HOT, which would
    # leave WARM provably empty - and a provably empty tier is legitimately
    # skipped rather than queried (see HybridVectorStore._tier_is_empty), so
    # it would never receive a filter to record. An unrelated point placed
    # directly on WARM keeps this test about what it says it is about: a
    # tier that COULD match gets the SAME filter HOT does, not "every
    # configured tier is queried regardless of whether it holds anything".
    both.warm.add("warm-unrelated-vector", {"business_key_value": "EMP999"})
    both.find(business_key_value="EMP002")

    hot_keys = {c.key for c in both.hot.received_filter.must}
    warm_keys = {c.key for c in both.warm.received_filter.must}

    assert hot_keys == warm_keys == {"business_key_value"}


# ======================================================================
# TEST J - the five original filters are untouched
# ======================================================================


def test_the_original_filters_still_behave(corpus):
    assert corpus.find(entity_type="employees")
    assert corpus.find(source_system_id="legacy_hr")
    assert corpus.find(source_entity="employees")
    assert corpus.find(sensitivity="internal")
    assert not corpus.find(source_system_id="a_system_that_does_not_exist")


def test_entity_type_is_not_singularized(corpus):
    """Phase 2 chose ``employees``. Phase 4 does not quietly change it."""
    hits = corpus.find(content_kind="structured_record")

    assert {h.entity_type for h in hits} == {"employees"}
    assert not corpus.find(entity_type="employee")


def test_document_chunks_keep_the_document_entity_type(corpus):
    hits = corpus.find(content_kind="document_chunk")

    assert {h.entity_type for h in hits} == {"document"}


# ======================================================================
# TEST K - no content in the payload
# ======================================================================


def test_no_extracted_document_text_or_bytes_reaches_the_vector_payload(corpus):
    """Scalar filter attributes are allowed; extracted documents/bytes are not."""
    surface = json.dumps(
        [_payload_for(state) for state in corpus.state.list_all()], default=str
    )

    assert "BIRTH CERTIFICATE" not in surface
    assert "EMPLOYMENT CONTRACT" not in surface
    assert "JVBERi0x" not in surface
    assert "iVBORw0KGgo" not in surface
    assert "%PDF" not in surface

    for payload in (_payload_for(s) for s in corpus.state.list_all()):
        assert "text_for_ai" not in payload
        assert "text" not in payload
        assert "content" not in payload


def test_the_payload_holds_only_declared_or_schema_driven_keys(corpus):
    """Dynamic keys must be exactly the canonical record's filter attributes."""
    allowed = {
        "representation_id", "embedding_id", "content_hash", "model_id",
        "dimension", "entity_type", "sensitivity", "canonical_record_id",
        *FILTERABLE_FIELDS,
        *PROVENANCE_ONLY_FIELDS,
    }

    for state in corpus.state.list_all():
        assert set(_payload_for(state)) <= allowed | set(state.filter_attributes)


# ======================================================================
# TEST L - composite business keys
# ======================================================================


def test_a_composite_business_key_survives_and_filters():
    """Phase 2's composite representation, used as-is rather than redesigned."""
    stock = SourceEntity(
        entity_id="wms.warehouse_stock",
        source_name="warehouse_stock",
        normalized_name="warehouse_stock",
        entity_kind=EntityKind.TABLE,
        primary_key_fields=("warehouse_id", "product_id"),
        fields=(
            _field("warehouse_id", FieldDataType.STRING, primary=True),
            _field("product_id", FieldDataType.STRING, primary=True),
            _field("quantity", FieldDataType.INTEGER),
        ),
    )
    rows = [
        SourceRecord.from_mapping(
            {"warehouse_id": "WH-1", "product_id": "P-77", "quantity": "5"}
        ),
        SourceRecord.from_mapping(
            {"warehouse_id": "WH-2", "product_id": "P-77", "quantity": "9"}
        ),
    ]
    state = InMemoryTierStateStore()
    hot = FilterAwareTier()
    store = HybridVectorStore(TierSet(hot=hot), state)

    for canonical in SourceNativeTransformer().transform_records(
        rows, stock, "legacy_wms", SourceType.POSTGRESQL
    ).records:
        store.store(embedding_for(canonical_record_to_representation(canonical)))

    hits = store.search(
        VECTOR,
        limit=10,
        filters=SearchFilters.from_mapping({"business_key_value": "WH-1|P-77"}),
    ).hits

    assert len(hits) == 1
    assert hits[0].state.business_key_name == "warehouse_id|product_id"
    assert hits[0].state.business_key_value == "WH-1|P-77"
    assert hits[0].state.canonical_record_id == (
        "erp:legacy_wms:warehouse_stock:wh-1_p-77"
    )


# ======================================================================
# TEST M - canonical entities are not made to carry what they never had
# ======================================================================


def test_a_canonical_record_without_a_business_key_is_not_given_one():
    """No value is invented for the mapping path, and nothing breaks."""
    from erp_pipeline.schemas.canonical_models import CanonicalRecord, SourceReference

    record = CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id="finance_erp",
            source_entity="fin_invoice",
            source_type=SourceType.POSTGRESQL,
            source_record_key="INV-204",
        ),
        entity_type="invoice",
        stable_source_key="INV-204",
        normalized_data={"invoice_number": "INV-204", "total_amount": "45000.00"},
    )
    representation = canonical_record_to_representation(record)

    assert "business_key_name" not in representation.metadata
    assert "business_key_value" not in representation.metadata
    assert representation.metadata["content_kind"] == "structured_record"

    state = InMemoryTierStateStore()
    store = HybridVectorStore(TierSet(hot=FilterAwareTier()), state)
    store.store(embedding_for(representation))

    stored = state.list_all()[0]

    assert stored.business_key_value is None
    assert stored.canonical_record_id == record.record_id
    assert "business_key_value" not in _payload_for(stored)


def test_a_canonical_record_is_still_reachable_by_its_original_filters():
    from erp_pipeline.schemas.canonical_models import CanonicalRecord, SourceReference

    record = CanonicalRecord.from_source(
        source=SourceReference(
            source_system_id="finance_erp",
            source_entity="fin_invoice",
            source_type=SourceType.POSTGRESQL,
            source_record_key="INV-204",
        ),
        entity_type="invoice",
        stable_source_key="INV-204",
        normalized_data={"invoice_number": "INV-204"},
    )
    state = InMemoryTierStateStore()
    hot = FilterAwareTier()
    store = HybridVectorStore(TierSet(hot=hot), state)
    store.store(embedding_for(canonical_record_to_representation(record)))

    found = store.search(
        VECTOR,
        limit=5,
        filters=SearchFilters.from_mapping(
            {"entity_type": "invoice", "source_system_id": "finance_erp"}
        ),
    ).hits

    assert len(found) == 1
    assert found[0].entity_type == "invoice"


# ======================================================================
# TEST N - combinations are AND
# ======================================================================


def test_every_constraint_applies_together(corpus):
    hits = corpus.find(
        business_key_value="EMP002",
        document_type="birth_certificate",
        content_kind="document_chunk",
        source_system_id="legacy_hr",
    )

    assert hits
    for hit in hits:
        assert hit.state.business_key_value == "EMP002"
        assert hit.state.document_type == "birth_certificate"
        assert hit.state.content_kind == "document_chunk"
        assert hit.state.source_system_id == "legacy_hr"


def test_one_wrong_constraint_empties_the_result(corpus):
    """AND, not OR: a contradiction returns nothing rather than the union."""
    assert corpus.find(business_key_value="EMP002", document_type="passport") == ()
    assert (
        corpus.find(
            business_key_value="EMP002",
            document_type="birth_certificate",
            source_system_id="a_different_erp",
        )
        == ()
    )


def test_filters_do_not_widen_each_other(corpus):
    """EMP002 + certificate must be a subset of EMP002 alone."""
    narrow = {
        h.vector_id
        for h in corpus.find(
            business_key_value="EMP002", document_type="birth_certificate"
        )
    }
    wide = {h.vector_id for h in corpus.find(business_key_value="EMP002")}

    assert narrow < wide


# ======================================================================
# Old vectors indexed before Phase 4
# ======================================================================


def test_a_vector_stored_before_phase_4_does_not_break_search(employees_entity):
    """It is excluded by the new filters and returned by the old ones.

    That is the honest outcome: the vector genuinely has no ``content_kind``,
    and inventing one would put it in a result set it does not belong to.
    """
    built = Corpus(employees_entity)
    built.add_employee("EMP002", "N", birth_certificate=_pdf("CERT"))

    legacy = StorageRecordMetadata(
        representation_id="ai:employees:legacy",
        embedding_id="emb.legacy",
        vector_id=vector_id_for("ai:employees:legacy"),
        current_tier=StorageTier.HOT,
        content_hash="h",
        model_id="test-model",
        dimension=len(VECTOR),
        entity_type="employees",
        source_system_id="legacy_hr",
        source_entity="employees",
    )
    built.state.save(legacy)
    built.hot.add(vector_id_for("ai:employees:legacy"), _payload_for(legacy))

    assert legacy.content_kind is None
    assert legacy.business_key_value is None

    # Old filters still find it.
    legacy_vector = vector_id_for("ai:employees:legacy")

    assert legacy_vector in {
        h.vector_id for h in built.find(entity_type="employees")
    }
    # New filters do not claim it.
    assert legacy_vector not in {
        h.vector_id for h in built.find(content_kind="structured_record")
    }
    # And it does not crash an unfiltered search.
    assert legacy_vector in {h.vector_id for h in built.find()}


def test_a_missing_column_reads_back_as_absent():
    """A database not re-bootstrapped since Phase 4 must not crash on read."""
    from erp_pipeline.storage.state import _row_to_metadata

    row = {
        "representation_id": "ai:x:1", "embedding_id": "e", "vector_id": "v",
        "current_tier": "hot", "content_hash": "h", "model_id": "m",
        "dimension": 4, "sensitivity": "internal",
        "business_criticality": "normal", "latency_requirement": "standard",
        "entity_type": "invoice", "access_count": 0, "recent_access_count": 0,
        "last_accessed_at": None, "created_at": None, "content_updated_at": None,
        "retention_until": None, "legal_hold": False, "tier_since": None,
        "policy_id": None, "policy_version": None, "state_version": 0,
        "updated_at": None,
    }
    metadata = _row_to_metadata(row)

    assert metadata.content_kind is None
    assert metadata.business_key_value is None
    assert metadata.page_start is None


# ======================================================================
# Structural agreement between the layers
# ======================================================================


def test_every_filterable_field_exists_on_the_state_row():
    """Otherwise ``_merge``'s backstop rejects every hit it should pass.

    ``_merge`` re-checks filters against storage state. A field that lives only
    in the vector payload would read as ``None`` there, compare unequal, and
    silently drop results the tier had already matched correctly.
    """
    for field in FILTERABLE_FIELDS:
        assert field in _METADATA_FIELDS, field


def test_every_filterable_field_is_carried_from_the_representation():
    """A field the embedding step drops can never reach the payload."""
    for field in FILTERABLE_FIELDS:
        if field in {"entity_type", "source_type"}:
            continue  # carried as a first-class column, not via metadata

        assert field in CARRIED_IDENTITY_KEYS, field


def test_provenance_fields_are_carried_and_stored_but_not_filterable():
    for field in PROVENANCE_ONLY_FIELDS:
        assert field in CARRIED_IDENTITY_KEYS
        assert field in _METADATA_FIELDS
        assert field not in FILTERABLE_FIELDS
