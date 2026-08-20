"""COLD tier: compressed, authenticated-encrypted vector archive.

    EmbeddingRecord
          ▼  serialize   JSON, deterministic key order
          ▼  compress    gzip level 9
          ▼  encrypt     AES-256-GCM, random 96-bit nonce
          ▼  write       one file per logical vector

ORDER MATTERS (Step 24)
-----------------------
Serialize, THEN compress, THEN encrypt. Encrypting first would produce
high-entropy bytes that gzip cannot shrink at all - the "compressed" archive
would be the same size as the plaintext, and the compression ratio reported in
the benchmark would be a fiction. A test asserts the ratio is genuinely < 1.

REAL AUTHENTICATED ENCRYPTION (Steps 21, 22)
--------------------------------------------
AES-256-GCM from ``cryptography``. Not XOR, not base64, not ECB, and nothing
home-grown. The nonce is 96 random bits per write, from ``os.urandom``.

Deriving the nonce from the record id or the content hash would make ciphertext
reproducible, which is tempting for tests and catastrophic in GCM: nonce reuse
under one key leaks the XOR of two plaintexts and destroys authentication. So
identity stays deterministic and CIPHERTEXT DOES NOT - a test asserts two writes
of identical content produce different bytes.

AUTHENTICATION IS THE POINT
---------------------------
GCM's tag means a single altered byte fails decryption rather than yielding a
subtly wrong vector. A corrupted archive is refused, and NOT deleted: it is the
only evidence of whatever went wrong.

KEYS NEVER TOUCH THE ARCHIVE (Step 23)
--------------------------------------
The key arrives from a provider backed by the environment. It is never written
beside the archive, never placed in metadata, never logged, and never included
in any ``to_dict()`` or ``repr``.
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from erp_pipeline.ai.hashing import vector_id_for
from erp_pipeline.ai.models import EmbeddingRecord, EmbeddingStatus
from erp_pipeline.schemas.identity import normalize_identifier
from erp_pipeline.storage.errors import (
    ColdArchiveIntegrityError,
    ColdArchiveNotFoundError,
    EncryptionKeyUnavailableError,
    VectorIdentityMismatchError,
)
from erp_pipeline.storage.models import (
    MeasurementKind,
    StorageFootprint,
    StorageTier,
    TierHealth,
)

#: Bumped whenever the envelope layout changes, so an old archive is recognized
#: rather than misparsed.
COLD_FORMAT_VERSION = "1.0"

#: AES-GCM standard nonce length. 96 bits is the size GCM is specified for.
NONCE_BYTES = 12
KEY_BYTES = 32          # AES-256
GZIP_LEVEL = 9
ENCRYPTION_ALGORITHM = "AES-256-GCM"
COMPRESSION_ALGORITHM = "gzip"
ARCHIVE_SUFFIX = ".erpcold"

#: Environment variable holding a base64 AES-256 key.
COLD_KEY_ENV = "ERP_COLD_ARCHIVE_KEY"


# ============================================================
# Key management (Step 23)
# ============================================================

@runtime_checkable
class ColdEncryptionKeyProvider(Protocol):
    """Supplies the archive encryption key at runtime."""

    def get_key(self) -> bytes:
        ...  # pragma: no cover - protocol declaration

    def is_available(self) -> bool:
        ...  # pragma: no cover - protocol declaration


@dataclass
class EnvironmentKeyProvider:
    """Reads a base64 AES-256 key from the environment.

    ``__repr__`` is overridden so the key cannot reach a traceback, a log line
    or a debugger transcript through an accidentally printed provider.
    """

    variable: str = COLD_KEY_ENV

    def is_available(self) -> bool:
        return bool(os.environ.get(self.variable))

    def get_key(self) -> bytes:
        import base64

        raw = os.environ.get(self.variable)

        if not raw:
            raise EncryptionKeyUnavailableError(
                f"no cold-archive key in {self.variable!r}. The cold tier "
                "refuses to write an unencrypted archive as a fallback."
            )

        try:
            key = base64.b64decode(raw)
        except Exception as exc:  # noqa: BLE001
            raise EncryptionKeyUnavailableError(
                f"the value in {self.variable!r} is not valid base64"
            ) from exc

        if len(key) != KEY_BYTES:
            raise EncryptionKeyUnavailableError(
                f"the cold-archive key must be {KEY_BYTES} bytes (AES-256), "
                f"got {len(key)}"
            )

        return key

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"EnvironmentKeyProvider(variable={self.variable!r})"


@dataclass
class StaticKeyProvider:
    """An in-memory key, for tests and ephemeral runs.

    Never persisted anywhere by this class. ``repr`` deliberately hides it.
    """

    key: bytes

    def __post_init__(self) -> None:
        if len(self.key) != KEY_BYTES:
            raise EncryptionKeyUnavailableError(
                f"the cold-archive key must be {KEY_BYTES} bytes, got "
                f"{len(self.key)}"
            )

    def is_available(self) -> bool:
        return True

    def get_key(self) -> bytes:
        return self.key

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "StaticKeyProvider(key=<redacted>)"


def generate_key() -> bytes:
    """A fresh AES-256 key from the OS CSPRNG."""
    return os.urandom(KEY_BYTES)


# ============================================================
# Archive envelope (Step 20)
# ============================================================

@dataclass(frozen=True)
class ColdArchiveEnvelope:
    """What is written to disk.

    Identity and model facts sit OUTSIDE the ciphertext so an archive can be
    located, inventoried and validated without holding the key. The vector and
    its payload sit inside.
    """

    format_version: str
    representation_id: str
    embedding_id: str
    vector_id: str
    content_hash: str
    model_id: str
    dimension: int
    entity_type: str | None
    compression: str
    encryption: str
    nonce: bytes
    ciphertext: bytes
    archived_at: str
    plaintext_bytes: int
    compressed_bytes: int

    @property
    def ciphertext_bytes(self) -> int:
        return len(self.ciphertext)

    @property
    def compression_ratio(self) -> float:
        if self.plaintext_bytes <= 0:
            return 0.0
        return round(self.compressed_bytes / self.plaintext_bytes, 6)

    def header(self) -> dict[str, Any]:
        """The unencrypted portion. Contains no vector and no key."""
        return {
            "format_version": self.format_version,
            "representation_id": self.representation_id,
            "embedding_id": self.embedding_id,
            "vector_id": self.vector_id,
            "content_hash": self.content_hash,
            "model_id": self.model_id,
            "dimension": self.dimension,
            "entity_type": self.entity_type,
            "compression": self.compression,
            "encryption": self.encryption,
            "archived_at": self.archived_at,
            "plaintext_bytes": self.plaintext_bytes,
            "compressed_bytes": self.compressed_bytes,
        }

    def to_bytes(self) -> bytes:
        """Serialize as ``header-length | header JSON | nonce | ciphertext``."""
        header = json.dumps(
            self.header(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

        return (
            len(header).to_bytes(4, "big") + header + self.nonce + self.ciphertext
        )

    def to_dict(self) -> dict[str, Any]:
        """Privacy-safe: the header plus sizes. Never nonce or ciphertext."""
        return {
            **self.header(),
            "ciphertext_bytes": self.ciphertext_bytes,
            "compression_ratio": self.compression_ratio,
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"ColdArchiveEnvelope(representation_id="
            f"{self.representation_id!r}, dimension={self.dimension}, "
            f"ciphertext_bytes={self.ciphertext_bytes})"
        )


def _parse_envelope(blob: bytes) -> tuple[dict[str, Any], bytes, bytes]:
    if len(blob) < 4:
        raise ColdArchiveIntegrityError("archive is truncated: no header length")

    header_length = int.from_bytes(blob[:4], "big")
    header_end = 4 + header_length

    if len(blob) < header_end + NONCE_BYTES:
        raise ColdArchiveIntegrityError("archive is truncated: header or nonce")

    try:
        header = json.loads(blob[4:header_end].decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ColdArchiveIntegrityError(
            "archive header is not readable JSON"
        ) from exc

    nonce = blob[header_end : header_end + NONCE_BYTES]
    ciphertext = blob[header_end + NONCE_BYTES :]

    if not ciphertext:
        raise ColdArchiveIntegrityError("archive contains no ciphertext")

    return header, nonce, ciphertext


# ============================================================
# The cold tier
# ============================================================

@dataclass
class ColdArchiveTier:
    """Filesystem archive of compressed, encrypted embedding records."""

    root: Path
    key_provider: ColdEncryptionKeyProvider
    write_calls: int = 0
    read_calls: int = 0
    delete_calls: int = 0

    tier: StorageTier = field(default=StorageTier.COLD, init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    # ------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------

    def ensure_ready(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def health(self) -> TierHealth:
        key_available = False

        try:
            key_available = self.key_provider.is_available()
        except Exception:  # noqa: BLE001 - availability probe
            key_available = False

        # A configured root that does not exist yet is still healthy: the first
        # archive creates it. Reporting it unavailable would be a false alarm
        # for every freshly configured tier, so the nearest existing ancestor is
        # probed instead. Nothing is created here - a health check must not have
        # side effects.
        probe = self.root

        while not probe.exists() and probe != probe.parent:
            probe = probe.parent

        writable = probe.exists() and os.access(probe, os.W_OK)

        return TierHealth(
            tier=StorageTier.COLD,
            available=bool(writable and key_available),
            detail=(
                None
                if writable and key_available
                else (
                    "archive directory not writable"
                    if not writable
                    else "encryption key unavailable"
                )
            ),
            record_count=self.count(),
            configuration={
                "root": str(self.root),
                "format_version": COLD_FORMAT_VERSION,
                "compression": COMPRESSION_ALGORITHM,
                "encryption": ENCRYPTION_ALGORITHM,
                "key_available": key_available,
            },
        )

    def path_for(self, representation_id: str) -> Path:
        return self.root / f"{normalize_identifier(representation_id)}{ARCHIVE_SUFFIX}"

    # ------------------------------------------------------------
    # Write (Steps 19-25)
    # ------------------------------------------------------------

    def archive(
        self, record: EmbeddingRecord, payload: Mapping[str, Any] | None = None
    ) -> ColdArchiveEnvelope:
        """Serialize, compress, encrypt and write one embedding."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if record.vector is None:
            raise VectorIdentityMismatchError(
                f"embedding {record.embedding_id!r} carries no vector to archive"
            )

        # Fails BEFORE anything is written. There is no unencrypted fallback.
        key = self.key_provider.get_key()

        plaintext = json.dumps(
            {
                "representation_id": record.representation_id,
                "embedding_id": record.embedding_id,
                "content_hash": record.content_hash,
                "model_id": record.model_id,
                "dimension": record.dimension,
                "entity_type": record.entity_type,
                "vector": list(record.vector),
                "payload": dict(payload or {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        # serialize -> compress -> encrypt. Reversing the last two would make
        # the compression a no-op on high-entropy ciphertext.
        compressed = gzip.compress(plaintext, compresslevel=GZIP_LEVEL)

        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(nonce, compressed, None)

        envelope = ColdArchiveEnvelope(
            format_version=COLD_FORMAT_VERSION,
            representation_id=record.representation_id,
            embedding_id=record.embedding_id,
            vector_id=vector_id_for(record.representation_id),
            content_hash=record.content_hash,
            model_id=record.model_id,
            dimension=record.dimension,
            entity_type=record.entity_type,
            compression=COMPRESSION_ALGORITHM,
            encryption=ENCRYPTION_ALGORITHM,
            nonce=nonce,
            ciphertext=ciphertext,
            archived_at=datetime.now(timezone.utc).isoformat(),
            plaintext_bytes=len(plaintext),
            compressed_bytes=len(compressed),
        )

        self.ensure_ready()
        path = self.path_for(record.representation_id)
        # Same representation overwrites its own archive: retrying an archive
        # must not accumulate duplicates (Step 52).
        path.write_bytes(envelope.to_bytes())
        self.write_calls += 1

        return envelope

    # ------------------------------------------------------------
    # Read and rehydrate (Steps 26, 27)
    # ------------------------------------------------------------

    def read_header(self, representation_id: str) -> dict[str, Any]:
        """Inventory an archive WITHOUT the key."""
        path = self.path_for(representation_id)

        if not path.exists():
            raise ColdArchiveNotFoundError(
                f"no cold archive for {representation_id!r}"
            )

        header, _, ciphertext = _parse_envelope(path.read_bytes())
        header["ciphertext_bytes"] = len(ciphertext)

        return header

    def rehydrate(self, representation_id: str) -> EmbeddingRecord:
        """Authenticate, decrypt, decompress and rebuild the embedding."""
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        path = self.path_for(representation_id)

        if not path.exists():
            raise ColdArchiveNotFoundError(
                f"no cold archive for {representation_id!r}"
            )

        header, nonce, ciphertext = _parse_envelope(path.read_bytes())
        self.read_calls += 1

        if header.get("format_version") != COLD_FORMAT_VERSION:
            raise ColdArchiveIntegrityError(
                f"archive format {header.get('format_version')!r} is not "
                f"{COLD_FORMAT_VERSION!r}",
                archive_id=representation_id,
            )

        key = self.key_provider.get_key()

        try:
            compressed = AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            # A single altered byte lands here. The archive is NOT removed.
            raise ColdArchiveIntegrityError(
                "cold archive failed authentication: the ciphertext was "
                "altered or the wrong key was supplied. The archive has been "
                "left in place.",
                archive_id=representation_id,
            ) from exc

        try:
            plaintext = gzip.decompress(compressed)
            body = json.loads(plaintext.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ColdArchiveIntegrityError(
                "cold archive decrypted but its contents are unreadable",
                archive_id=representation_id,
            ) from exc

        vector = tuple(float(value) for value in body["vector"])

        if len(vector) != int(header["dimension"]):
            raise VectorIdentityMismatchError(
                f"archived vector has {len(vector)} dimensions, header "
                f"declares {header['dimension']}"
            )

        if body["representation_id"] != header["representation_id"]:
            raise VectorIdentityMismatchError(
                "archive header and payload disagree about the representation"
            )

        return EmbeddingRecord(
            embedding_id=body["embedding_id"],
            representation_id=body["representation_id"],
            entity_type=body.get("entity_type"),
            content_hash=body["content_hash"],
            model_id=body["model_id"],
            dimension=int(body["dimension"]),
            status=EmbeddingStatus.GENERATED,
            vector=vector,
        )

    def stored_payload(self, representation_id: str) -> dict[str, Any]:
        """The payload stored alongside the vector, for re-upsert on promotion."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        path = self.path_for(representation_id)

        if not path.exists():
            raise ColdArchiveNotFoundError(
                f"no cold archive for {representation_id!r}"
            )

        _, nonce, ciphertext = _parse_envelope(path.read_bytes())
        compressed = AESGCM(self.key_provider.get_key()).decrypt(
            nonce, ciphertext, None
        )

        return json.loads(gzip.decompress(compressed).decode("utf-8")).get(
            "payload", {}
        )

    # ------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------

    def exists(self, representation_id: str) -> bool:
        return self.path_for(representation_id).exists()

    def delete(self, representation_id: str) -> bool:
        path = self.path_for(representation_id)

        if not path.exists():
            return False

        path.unlink()
        self.delete_calls += 1

        return True

    def count(self) -> int:
        if not self.root.exists():
            return 0

        return sum(1 for _ in self.root.glob(f"*{ARCHIVE_SUFFIX}"))

    def total_bytes(self) -> int:
        if not self.root.exists():
            return 0

        return sum(
            path.stat().st_size for path in self.root.glob(f"*{ARCHIVE_SUFFIX}")
        )

    # ------------------------------------------------------------
    # Footprint (Step 58)
    # ------------------------------------------------------------

    def footprint(self) -> StorageFootprint:
        """Actual bytes on disk.

        MEASURED, not proxied: these are real files and ``stat().st_size`` is
        exact. The encryption key is not counted, because it is not archive
        data and lives elsewhere entirely.
        """
        count = self.count()
        total = self.total_bytes()

        return StorageFootprint(
            tier=StorageTier.COLD,
            record_count=count,
            bytes_total=float(total),
            bytes_per_record=float(total / count) if count else 0.0,
            kind=MeasurementKind.MEASURED,
            method=(
                "sum of on-disk archive file sizes (stat.st_size); includes "
                "the plaintext header, nonce, GCM tag and payload metadata, "
                "excludes the encryption key which is not stored here"
            ),
            detail={
                "root": str(self.root),
                "compression": COMPRESSION_ALGORITHM,
                "encryption": ENCRYPTION_ALGORITHM,
                "format_version": COLD_FORMAT_VERSION,
            },
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"ColdArchiveTier(root={str(self.root)!r}, archives={self.count()})"


__all__ = [
    "COLD_FORMAT_VERSION",
    "COLD_KEY_ENV",
    "ENCRYPTION_ALGORITHM",
    "COMPRESSION_ALGORITHM",
    "NONCE_BYTES",
    "KEY_BYTES",
    "ARCHIVE_SUFFIX",
    "ColdEncryptionKeyProvider",
    "EnvironmentKeyProvider",
    "StaticKeyProvider",
    "generate_key",
    "ColdArchiveEnvelope",
    "ColdArchiveTier",
]
