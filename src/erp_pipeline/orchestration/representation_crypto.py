"""Application-layer encryption for sensitive representation text (Phase 10).

THE GAP PHASE 5 LEFT OPEN, DELIBERATELY
---------------------------------------
Phase 5 recorded plainly that representation text is NOT inside the encrypted
COLD archive: it lives in ``erp_runtime.ai_representations`` and inherits the
database's protection rather than the archive's. That was the honest statement
at the time and it stayed a limitation through Phase 9.

It matters more now, because Phase 10 lets a caller declare a birth certificate
RESTRICTED. A classification that changes routing metadata but leaves the
extracted text sitting in a plaintext column has done half a job.

WHAT THIS DOES AND DOES NOT CHANGE
----------------------------------
Encryption is a STORAGE concern. The API returns the same text whether the row
is encrypted or not, ``content_hash`` still describes the plaintext, and Qdrant
still receives no text at all. Nothing about search changes, because search
never touched this text in the first place.

WHY A DEDICATED KEY
-------------------
``ERP_REPRESENTATION_ENCRYPTION_KEY``, separate from the cold-archive key.
Reusing one key across two purposes means rotating it for either reason forces
both, and a compromise of one context hands over the other. The key FORMAT and
the provider pattern are reused from the cold tier - the same base64 AES-256
validation, the same ``__repr__`` suppression so a key cannot reach a traceback.

FAIL CLOSED
-----------
If a classification requires encryption and no key is configured, persistence
FAILS. It does not fall back to plaintext. Because Phase 5 persists before
embedding, that failure also means the vector never becomes searchable - the
document is absent rather than exposed, which is the correct direction for a
security control to fail in.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.schemas.sensitivity import coerce, rank

#: Dedicated key. Never the cold-archive key.
REPRESENTATION_KEY_ENV = "ERP_REPRESENTATION_ENCRYPTION_KEY"

#: AES-256.
KEY_BYTES = 32
#: 96 bits, the size AES-GCM is specified for.
NONCE_BYTES = 12

ENCRYPTION_ALGORITHM = "AES-256-GCM"
#: Stamped on every ciphertext so a future format change can be recognised
#: rather than guessed at.
ENCRYPTION_VERSION = 1

#: Classifications whose representation text must not sit in the database in
#: plaintext. RESTRICTED and CONFIDENTIAL: both describe content whose
#: disclosure is a problem, and the cost of encrypting the smaller of the two
#: populations is low.
#:
#: PUBLIC and INTERNAL are deliberately excluded. Encrypting the entire corpus
#: would make every existing row unreadable without a key that no current
#: deployment has, turn a missing key into a total outage rather than a
#: contained refusal, and buy nothing for content already handled as
#: non-sensitive.
ENCRYPT_AT_OR_ABOVE = SensitivityLevel.CONFIDENTIAL


class RepresentationEncryptionError(RuntimeError):
    """Encryption or decryption of representation text failed."""


class EncryptionKeyUnavailableError(RepresentationEncryptionError):
    """A classification requires encryption and no usable key is configured."""


def requires_encryption(sensitivity: Any) -> bool:
    """Whether this classification's text must be encrypted at rest."""
    level = coerce(sensitivity)

    if level is None:
        return False

    return rank(level) >= rank(ENCRYPT_AT_OR_ABOVE)


@dataclass
class EnvironmentRepresentationKeyProvider:
    """Reads a base64 AES-256 key from the environment.

    ``__repr__`` is overridden for the same reason the cold tier overrides it:
    a provider printed into a traceback, a log line or a debugger transcript
    must not carry the key with it.
    """

    variable: str = REPRESENTATION_KEY_ENV

    def is_available(self) -> bool:
        return bool(os.environ.get(self.variable))

    def get_key(self) -> bytes:
        raw = os.environ.get(self.variable)

        if not raw:
            raise EncryptionKeyUnavailableError(
                f"no representation encryption key in {self.variable!r}. "
                "Sensitive representation text is not written in plaintext as "
                "a fallback."
            )

        try:
            key = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001 - the value, never the reason
            raise EncryptionKeyUnavailableError(
                f"the value in {self.variable!r} is not valid base64"
            ) from exc

        if len(key) != KEY_BYTES:
            raise EncryptionKeyUnavailableError(
                f"the representation key must be {KEY_BYTES} bytes (AES-256), "
                f"got {len(key)}"
            )

        return key

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"EnvironmentRepresentationKeyProvider(variable={self.variable!r})"
        )


@dataclass
class StaticRepresentationKeyProvider:
    """An explicitly supplied key, for tests and for a deployment that manages
    its own secret delivery."""

    key: bytes

    def is_available(self) -> bool:
        return len(self.key) == KEY_BYTES

    def get_key(self) -> bytes:
        if len(self.key) != KEY_BYTES:
            raise EncryptionKeyUnavailableError(
                f"the representation key must be {KEY_BYTES} bytes (AES-256)"
            )

        return self.key

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "StaticRepresentationKeyProvider(key=<redacted>)"


class RepresentationCipher:
    """Encrypts and decrypts representation text with AES-256-GCM.

    A fresh random nonce per encryption, so the same certificate stored twice
    produces different ciphertext. GCM is authenticated, so a tampered or
    truncated row fails to decrypt rather than yielding plausible garbage.
    """

    def __init__(self, key_provider: Any = None) -> None:
        self._keys = key_provider or EnvironmentRepresentationKeyProvider()

    @property
    def available(self) -> bool:
        try:
            return bool(self._keys.is_available())
        except Exception:  # noqa: BLE001 - unavailable is unavailable
            return False

    def encrypt(self, text: str) -> str:
        """Plaintext -> a self-describing encoded envelope."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = self._keys.get_key()
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)

        envelope = {
            "v": ENCRYPTION_VERSION,
            "alg": ENCRYPTION_ALGORITHM,
            "n": base64.b64encode(nonce).decode("ascii"),
            "c": base64.b64encode(ciphertext).decode("ascii"),
        }

        return ENVELOPE_PREFIX + json.dumps(envelope, separators=(",", ":"))

    def decrypt(self, stored: str) -> str:
        """An envelope -> plaintext. Raises rather than returning garbage."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not is_encrypted(stored):
            # A plaintext row from before this phase, or a non-sensitive one.
            return stored

        try:
            envelope = json.loads(stored[len(ENVELOPE_PREFIX):])
            nonce = base64.b64decode(envelope["n"])
            ciphertext = base64.b64decode(envelope["c"])
        except Exception as exc:  # noqa: BLE001 - never echo the stored value
            raise RepresentationEncryptionError(
                "the stored representation envelope is malformed"
            ) from exc

        key = self._keys.get_key()

        try:
            return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - a wrong key and a tampered
            # row are the same answer: this did not authenticate.
            raise RepresentationEncryptionError(
                "the representation could not be decrypted; the key is wrong "
                "or the stored value was altered"
            ) from exc


#: Marks an encrypted column value. A prefix rather than a separate column, so
#: an existing plaintext row needs no migration to remain readable.
ENVELOPE_PREFIX = "encv1:"


def is_encrypted(stored: Any) -> bool:
    return isinstance(stored, str) and stored.startswith(ENVELOPE_PREFIX)


def encryption_metadata(stored: Any) -> Mapping[str, Any]:
    """Operational facts about a stored value. Never the key, never the text."""
    if not is_encrypted(stored):
        return {"encrypted": False}

    try:
        envelope = json.loads(stored[len(ENVELOPE_PREFIX):])
    except Exception:  # noqa: BLE001 - defensive
        return {"encrypted": True}

    return {
        "encrypted": True,
        "encryption_version": envelope.get("v"),
        "algorithm": envelope.get("alg"),
    }


__all__ = [
    "ENCRYPTION_ALGORITHM",
    "ENCRYPTION_VERSION",
    "ENCRYPT_AT_OR_ABOVE",
    "ENVELOPE_PREFIX",
    "EncryptionKeyUnavailableError",
    "EnvironmentRepresentationKeyProvider",
    "REPRESENTATION_KEY_ENV",
    "RepresentationCipher",
    "RepresentationEncryptionError",
    "StaticRepresentationKeyProvider",
    "encryption_metadata",
    "is_encrypted",
    "requires_encryption",
]
