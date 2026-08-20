"""Domain error hierarchy for the mapping engine.

Follows the pattern every prior phase established: a mapping failure surfaces
as one of these rather than a raw ``KeyError`` or ``ValueError``.

PRIVACY RULE, enforced by test
------------------------------
Phase 8 works on SCHEMAS, not data, so no message here can leak a business
value simply because the engine never receives one. What a message may still
name is structure - a field name, an entity name, a type, a canonical target -
and that is exactly what makes an error actionable.
"""

from __future__ import annotations


class MappingEngineError(Exception):
    """Base class for every mapping-engine error."""


class CanonicalTargetNotFoundError(MappingEngineError):
    """Raised when a named canonical entity or field does not exist.

    Most often hit by a manual override naming a target that the configured
    canonical model does not declare - which is a real mistake worth failing
    on, rather than silently dropping the override.
    """

    def __init__(self, message: str, target: str | None = None) -> None:
        super().__init__(message)
        self.target = target


class SourceFieldNotFoundError(MappingEngineError):
    """Raised when an override names a source field the schema does not have."""

    def __init__(self, message: str, source_field: str | None = None) -> None:
        super().__init__(message)
        self.source_field = source_field


class InvalidMappingOverrideError(MappingEngineError):
    """Raised when a human override cannot be honoured as written.

    An override is a deliberate human decision and is trusted over the
    engine's own suggestion - but it is still validated. Accepting an override
    that maps an OBJECT to a DECIMAL would make the profile unusable in
    Phase 9, and failing at mapping time is far cheaper than failing during a
    transformation run.
    """

    def __init__(
        self,
        message: str,
        source_field: str | None = None,
        target_field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.source_field = source_field
        self.target_field = target_field


class MappingValidationError(MappingEngineError):
    """Raised when a generated or supplied profile fails strict validation.

    Validation is non-fatal by default: findings are returned so a reviewer can
    see everything wrong at once. This is raised only when a caller explicitly
    asks for strict enforcement.
    """

    def __init__(self, message: str, findings: tuple = ()) -> None:
        super().__init__(message)
        self.findings = findings


class MappingConfigurationError(MappingEngineError):
    """Raised when the engine's own configuration is contradictory.

    For example a high threshold below the medium threshold, or scoring
    weights that do not sum to 1.0 - both of which would make every score
    meaningless in a way that is much better caught at construction.
    """


__all__ = [
    "MappingEngineError",
    "CanonicalTargetNotFoundError",
    "SourceFieldNotFoundError",
    "InvalidMappingOverrideError",
    "MappingValidationError",
    "MappingConfigurationError",
]
