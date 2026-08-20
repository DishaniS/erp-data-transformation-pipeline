"""The cold tier: real compression, real authenticated encryption, real keys."""

from __future__ import annotations

import gzip
import os
from pathlib import Path

import pytest

from erp_pipeline.ai.models import EmbeddingStatus
from erp_pipeline.storage.cold_tier import (
    COLD_FORMAT_VERSION,
    COLD_KEY_ENV,
    COMPRESSION_ALGORITHM,
    ENCRYPTION_ALGORITHM,
    KEY_BYTES,
    NONCE_BYTES,
    ColdArchiveTier,
    EnvironmentKeyProvider,
    StaticKeyProvider,
    generate_key,
)
from erp_pipeline.storage.errors import (
    ColdArchiveIntegrityError,
    ColdArchiveNotFoundError,
    EncryptionKeyUnavailableError,
)

from .conftest import SECRET_PAYLOAD_VALUE, make_embedding

def test_archive_and_rehydrate_is_bit_exact(cold_tier: ColdArchiveTier):
    """Cold is an archive, not an approximation. Lossy here would be a defect."""
    record = make_embedding(seed=7)
    cold_tier.archive(record)

    restored = cold_tier.rehydrate(record.representation_id)

    assert restored.vector == record.vector
    assert restored.representation_id == record.representation_id
    assert restored.embedding_id == record.embedding_id
    assert restored.content_hash == record.content_hash
    assert restored.dimension == record.dimension


def test_payload_survives_the_round_trip(cold_tier: ColdArchiveTier):
    record = make_embedding()
    cold_tier.archive(record, {"text": "invoice text", "secret": SECRET_PAYLOAD_VALUE})

    assert cold_tier.stored_payload(record.representation_id)["text"] == "invoice text"


def test_compression_actually_shrinks_the_data(cold_tier: ColdArchiveTier):
    """Claiming compression without measuring it would be an unverified claim."""
    record = make_embedding(dimension=384, vector=[0.01 * (i % 50) for i in range(384)])
    envelope = cold_tier.archive(record)

    assert envelope.compressed_bytes < envelope.plaintext_bytes
    assert 0.0 < envelope.compression_ratio < 1.0


def test_archive_bytes_are_not_readable_plaintext(cold_tier: ColdArchiveTier):
    """The whole point of encrypting: the secret must not be greppable on disk."""
    record = make_embedding()
    cold_tier.archive(record, {"secret": SECRET_PAYLOAD_VALUE})

    raw = cold_tier.path_for(record.representation_id).read_bytes()

    # The payload - the part that carries business data - must be unreadable.
    assert SECRET_PAYLOAD_VALUE.encode() not in raw

    # The vector components must not be recoverable from the file either.
    for component in record.vector:
        assert repr(component).encode() not in raw

    # It must not be merely compressed - gzip is not a cipher.
    with pytest.raises(Exception):
        gzip.decompress(raw)


def test_the_cleartext_header_is_a_deliberate_and_bounded_disclosure(
    cold_tier: ColdArchiveTier,
):
    """The header is intentionally readable without the key, so archives can be
    inventoried, audited and garbage-collected without decrypting every file.

    That is a real disclosure and worth stating plainly: the identifier, entity
    type, model id and sizes ARE on disk in cleartext. The test pins that
    boundary so the header cannot quietly grow to include business data.
    """
    record = make_embedding()
    cold_tier.archive(record, {"secret": SECRET_PAYLOAD_VALUE})

    raw = cold_tier.path_for(record.representation_id).read_bytes()
    header = cold_tier.read_header(record.representation_id)

    # Disclosed by design.
    assert record.representation_id.encode() in raw

    # The header's keys are a closed set - nothing business-bearing.
    assert set(header) <= {
        "archived_at",
        "compressed_bytes",
        "compression",
        "content_hash",
        "dimension",
        "embedding_id",
        "encryption",
        "entity_type",
        "format_version",
        "model_id",
        "plaintext_bytes",
        "representation_id",
        "vector_id",
        "ciphertext_bytes",
    }


def test_tampering_is_detected_and_never_silently_accepted(cold_tier: ColdArchiveTier):
    """AES-GCM authenticates. A flipped byte must raise, not decode to garbage."""
    record = make_embedding()
    cold_tier.archive(record)
    path = cold_tier.path_for(record.representation_id)

    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0x01
    path.write_bytes(bytes(raw))

    with pytest.raises(ColdArchiveIntegrityError):
        cold_tier.rehydrate(record.representation_id)


def test_tampered_archive_is_preserved_for_investigation(cold_tier: ColdArchiveTier):
    """A corrupt archive is evidence. Deleting it would destroy the only copy."""
    record = make_embedding()
    cold_tier.archive(record)
    path = cold_tier.path_for(record.representation_id)

    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0x01
    path.write_bytes(bytes(raw))

    with pytest.raises(ColdArchiveIntegrityError):
        cold_tier.rehydrate(record.representation_id)

    assert path.exists()


def test_wrong_key_cannot_decrypt(tmp_path: Path):
    record = make_embedding()
    ColdArchiveTier(tmp_path, StaticKeyProvider(generate_key())).archive(record)

    stranger = ColdArchiveTier(tmp_path, StaticKeyProvider(generate_key()))

    with pytest.raises(ColdArchiveIntegrityError):
        stranger.rehydrate(record.representation_id)


def test_missing_key_raises_a_typed_error(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(COLD_KEY_ENV, raising=False)
    tier = ColdArchiveTier(tmp_path, EnvironmentKeyProvider())

    with pytest.raises(EncryptionKeyUnavailableError):
        tier.archive(make_embedding())


def test_missing_archive_raises_a_typed_error(cold_tier: ColdArchiveTier):
    with pytest.raises(ColdArchiveNotFoundError):
        cold_tier.rehydrate("ai:invoice:never-archived")


def test_key_is_never_written_beside_the_archive(cold_tier: ColdArchiveTier):
    """A key stored next to the ciphertext is not encryption, it is filing."""
    record = make_embedding()
    cold_tier.archive(record)
    key = cold_tier.key_provider.get_key()

    for path in Path(cold_tier.root).rglob("*"):
        if path.is_file():
            assert key not in path.read_bytes()


def test_key_provider_repr_redacts_the_key():
    """Keys leak through logs and tracebacks more often than through files."""
    provider = StaticKeyProvider(generate_key())

    assert "key" not in repr(provider) or provider.get_key().hex() not in repr(provider)
    assert provider.get_key().hex()[:8] not in repr(provider)


def test_header_carries_no_secret_material(cold_tier: ColdArchiveTier):
    """The header must be readable without the key - so it must hold nothing secret."""
    record = make_embedding()
    cold_tier.archive(record, {"secret": SECRET_PAYLOAD_VALUE})

    header = cold_tier.read_header(record.representation_id)
    rendered = repr(header)

    assert SECRET_PAYLOAD_VALUE not in rendered
    assert cold_tier.key_provider.get_key().hex() not in rendered
    assert header["format_version"] == COLD_FORMAT_VERSION
    assert header["encryption"] == ENCRYPTION_ALGORITHM
    assert header["compression"] == COMPRESSION_ALGORITHM


def test_generated_keys_are_the_right_size_and_not_constant():
    first, second = generate_key(), generate_key()

    assert len(first) == KEY_BYTES == 32
    assert first != second


def test_each_archive_uses_a_fresh_nonce(cold_tier: ColdArchiveTier):
    """Reusing a nonce with GCM is a catastrophic, silent break."""
    nonces = set()

    for index in range(12):
        record = make_embedding(representation_id=f"ai:invoice:n-{index}", seed=index)
        envelope = cold_tier.archive(record)
        nonces.add(bytes(envelope.nonce))

        assert len(envelope.nonce) == NONCE_BYTES

    assert len(nonces) == 12


def test_delete_removes_the_archive(cold_tier: ColdArchiveTier):
    record = make_embedding()
    cold_tier.archive(record)

    assert cold_tier.exists(record.representation_id)
    assert cold_tier.delete(record.representation_id) is True
    assert not cold_tier.exists(record.representation_id)


def test_footprint_is_labelled_as_measured(cold_tier: ColdArchiveTier):
    """Cold bytes are real files, so they must be reported as MEASURED."""
    from erp_pipeline.storage.models import MeasurementKind

    cold_tier.archive(make_embedding())
    footprint = cold_tier.footprint()

    assert footprint.kind is MeasurementKind.MEASURED
    assert footprint.bytes_total > 0
