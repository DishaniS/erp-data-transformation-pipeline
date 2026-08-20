"""Controlled errors raised by the hybrid tiered vector storage layer.

PRIVACY
-------
No exception here carries a vector, AI-ready text, a business value or an
encryption key. Messages name identities, tiers, policy versions, sizes and
algorithm names. A traceback from a storage failure must be safe to paste into
a ticket.

FAIL SAFE, NOT FAIL QUIET
-------------------------
Every error in this module exists because the alternative was a silent
correctness or security failure: an unencrypted archive written because a key
was missing, a source copy deleted after a failed destination write, a
restricted record placed in a prohibited location. Those are the failures that
matter here, so they are loud.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base class for every error this package raises."""


class StorageConfigurationError(StorageError):
    """The storage layer was configured in a way that cannot work."""


class PolicyViolationError(StorageError):
    """A placement would breach a HARD policy constraint.

    Distinct from "this tier scored badly". A score can be overridden by a
    better score; a compliance constraint cannot be overridden by anything,
    including a manual administrator request.
    """

    def __init__(
        self,
        message: str,
        sensitivity: str | None = None,
        requested_tier: str | None = None,
    ) -> None:
        super().__init__(message)
        self.sensitivity = sensitivity
        self.requested_tier = requested_tier


class TierUnavailableError(StorageError):
    """A tier cannot currently accept or serve records.

    Raised rather than silently falling back, because a fallback that nobody
    noticed is how a record ends up somewhere policy did not intend.
    """

    def __init__(self, message: str, tier: str | None = None) -> None:
        super().__init__(message)
        self.tier = tier


class MigrationError(StorageError):
    """A tier migration failed.

    Carries the stage so a caller knows whether the source copy is still
    intact - which it always should be, because the destination is written and
    verified before the source is retired.
    """

    def __init__(
        self,
        message: str,
        stage: str | None = None,
        source_intact: bool = True,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.source_intact = source_intact


class ConcurrencyConflictError(StorageError):
    """Another worker changed this record's tier state concurrently.

    Detected by an optimistic version check. The losing migration aborts rather
    than overwriting: two workers moving one vector to different destinations
    is exactly how a vector ends up in two tiers or none.
    """

    def __init__(
        self,
        message: str,
        expected_version: int | None = None,
        actual_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.expected_version = expected_version
        self.actual_version = actual_version


class EncryptionKeyUnavailableError(StorageError):
    """No cold-archive encryption key is available.

    A COLD write MUST fail here. Writing an unencrypted archive as a
    convenience fallback would silently downgrade the security guarantee the
    cold tier exists to provide.
    """


class ColdArchiveIntegrityError(StorageError):
    """An archive failed authentication or structural validation.

    AES-GCM authentication failing means the ciphertext was altered. The
    archive is NOT deleted automatically - a tampered or corrupted archive is
    evidence, and destroying it on detection would remove the only copy of
    whatever went wrong.
    """

    def __init__(self, message: str, archive_id: str | None = None) -> None:
        super().__init__(message)
        self.archive_id = archive_id


class ColdArchiveNotFoundError(StorageError):
    """No archive exists for the requested identity."""


class RetentionProtectedError(StorageError):
    """Deletion is refused because a retention requirement still applies."""

    def __init__(self, message: str, retention_until: object | None = None) -> None:
        super().__init__(message)
        self.retention_until = retention_until


class VectorIdentityMismatchError(StorageError):
    """A restored vector does not match the identity or shape it should have.

    Raised during rehydration verification. A cold archive that silently
    restored a different vector would corrupt retrieval in a way nothing
    downstream could detect.
    """


__all__ = [
    "StorageError",
    "StorageConfigurationError",
    "PolicyViolationError",
    "TierUnavailableError",
    "MigrationError",
    "ConcurrencyConflictError",
    "EncryptionKeyUnavailableError",
    "ColdArchiveIntegrityError",
    "ColdArchiveNotFoundError",
    "RetentionProtectedError",
    "VectorIdentityMismatchError",
]
