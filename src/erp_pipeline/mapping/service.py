"""The public entry point: schema in, ``MappingProfile`` out.

    result = generate_mapping(source_schema)

    result.profiles     # Phase 1 MappingProfile objects - the real output
    result.decisions    # every field, its candidates and the reasoning
    result.coverage     # source, per-entity and required-target coverage
    result.ambiguities  # what the engine refused to decide
    result.validation

WHAT IS PRODUCED, AND WHAT IS NOT
---------------------------------
Produced: mapping profiles saying which source field becomes which canonical
field, with what confidence and why.

Not produced, and not producible by anything in this package: a
``CanonicalRecord``, a transformed value, an executed ``TransformationRule``.
Phase 8 decides the mapping; Phase 9 applies it. A static test asserts this
package contains no transformation execution.

PROFILE SCOPE
-------------
``MappingProfile`` is defined as one source entity -> one canonical entity
type, so one schema with three entities yields up to three profiles. That is
the contract's own scoping decision, not an invention here.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from erp_pipeline.mapping.canonical_model import (
    CanonicalTargetModel,
    DEFAULT_CANONICAL_MODEL,
)
from erp_pipeline.mapping.coverage import compute_coverage
from erp_pipeline.mapping.engine import MappingEngine, find_source_field
from erp_pipeline.mapping.errors import MappingValidationError
from erp_pipeline.mapping.models import (
    DEFAULT_OPTIONS,
    MAPPING_ENGINE_VERSION,
    FieldDecision,
    FieldOutcome,
    MappingCoverage,
    MappingOptions,
    MappingOverride,
    MappingResult,
    RejectedCandidate,
    ValidationReport,
)
from erp_pipeline.mapping.normalization import NormalizationConfig
from erp_pipeline.mapping.validation import validate_profile
from erp_pipeline.schemas.enums import MappingStatus
from erp_pipeline.schemas.identity import hash_json_payload, normalize_identifier
from erp_pipeline.schemas.mapping_models import FieldMapping, MappingProfile
from erp_pipeline.schemas.source_models import SourceSchema


class MappingService:
    """Generates, validates and persists mapping profiles."""

    def __init__(
        self,
        canonical_model: CanonicalTargetModel | None = None,
        options: MappingOptions | None = None,
        normalization: NormalizationConfig | None = None,
    ) -> None:
        self._model = canonical_model or DEFAULT_CANONICAL_MODEL
        self._options = options or DEFAULT_OPTIONS
        self._engine = MappingEngine(self._model, self._options, normalization)

    @property
    def engine(self) -> MappingEngine:
        return self._engine

    @property
    def canonical_model(self) -> CanonicalTargetModel:
        return self._model

    # ------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------

    def generate(
        self,
        schema: SourceSchema,
        overrides: Sequence[MappingOverride] = (),
        rejected: Sequence[RejectedCandidate] = (),
        validate: bool = True,
        strict: bool = False,
    ) -> MappingResult:
        """Map one ``SourceSchema`` toward the canonical model.

        ``strict`` turns validation errors into a raised
        ``MappingValidationError``. It is off by default because a profile
        with problems is exactly what a reviewer needs to see.
        """
        self._require_valid_overrides(schema, overrides)

        decisions, entity_matches, collisions = self._engine.decide_schema(
            schema, overrides=overrides, rejected=rejected
        )

        profiles = self._build_profiles(schema, decisions, entity_matches)

        coverage = compute_coverage(
            decisions=decisions,
            entity_matches=entity_matches,
            canonical_model=self._model,
            source_entity_names={
                entity.normalized_name: entity.source_name
                for entity in schema.entities
            },
        )

        report: ValidationReport | None = None
        if validate:
            report = self._validate_all(profiles, schema)
            if strict and not report.is_valid:
                raise MappingValidationError(
                    f"{len(report.errors)} validation error(s) in the generated "
                    f"mapping for schema {schema.schema_id!r}.",
                    findings=report.errors,
                )

        return MappingResult(
            source_system_id=schema.source_system_id,
            source_schema_id=schema.schema_id,
            canonical_model_identity=self._model.identity,
            engine_version=MAPPING_ENGINE_VERSION,
            config_fingerprint=self._engine.config_fingerprint,
            profiles=profiles,
            decisions=decisions,
            coverage=coverage,
            ambiguities=tuple(
                decision.ambiguity
                for decision in decisions
                if decision.ambiguity is not None
            ),
            collisions=collisions,
            validation=report,
            unmatched_entities=tuple(
                sorted(
                    name for name, matched in entity_matches.items()
                    if matched is None
                )
            ),
        )

    # ------------------------------------------------------------
    # Phase 1 contract assembly (Step 27)
    # ------------------------------------------------------------

    def _build_profiles(
        self,
        schema: SourceSchema,
        decisions: Sequence[FieldDecision],
        entity_matches: Mapping[str, str | None],
    ) -> tuple[MappingProfile, ...]:
        """Build one ``MappingProfile`` per (source entity -> canonical entity).

        Only SELECTED decisions become ``FieldMapping`` objects. An ambiguous
        or unmapped field is deliberately absent from the profile rather than
        present with a low confidence: a profile is a set of instructions, and
        an instruction nobody has decided on is not one. The full picture,
        including everything not selected, lives in ``MappingResult.decisions``.
        """
        by_entity: dict[str, list[FieldDecision]] = {}
        for decision in decisions:
            by_entity.setdefault(decision.source_entity, []).append(decision)

        profiles: list[MappingProfile] = []

        for entity in schema.entities:
            target_entity_type = entity_matches.get(entity.normalized_name)
            if target_entity_type is None:
                continue

            entity_decisions = by_entity.get(entity.source_name, [])
            field_mappings = tuple(
                self._build_field_mapping(decision)
                for decision in entity_decisions
                if decision.selected is not None
                and decision.selected.target_entity_type == target_entity_type
            )

            if not field_mappings:
                # A profile with no instructions would be noise in the catalog.
                continue

            profiles.append(
                self._build_profile(
                    schema=schema,
                    source_entity=entity.source_name,
                    target_entity_type=target_entity_type,
                    field_mappings=field_mappings,
                )
            )

        return tuple(profiles)

    def _build_field_mapping(self, decision: FieldDecision) -> FieldMapping:
        """One selected decision as a Phase 1 ``FieldMapping``.

        ``status`` reuses the existing ``MappingStatus`` vocabulary rather than
        inventing one: an engine-selected mapping is ``AUTO_ACCEPTED``, a human
        decision is ``APPROVED``. ``confidence`` carries the numeric score,
        which the contract already bounds to [0, 1].

        ``metadata`` carries a compact evidence summary so a profile reloaded
        from the catalog still explains itself without the supplemental
        objects.
        """
        selected = decision.selected
        assert selected is not None

        status = (
            MappingStatus.APPROVED
            if decision.outcome is FieldOutcome.MANUAL_OVERRIDE
            else MappingStatus.AUTO_ACCEPTED
        )

        return FieldMapping(
            source_field=selected.source_field,
            target_field=selected.target_field,
            source_type=selected.source_data_type,
            target_type=selected.target_data_type,
            # Phase 8 declares no transformations. Deciding that a value needs
            # a date parse is a mapping decision; choosing the format is a
            # transformation decision, and that belongs to Phase 9.
            transformations=(),
            confidence=selected.score.total,
            status=status,
            reason=selected.explain(),
            metadata={
                "selection": decision.outcome.value,
                "confidence_level": selected.confidence.value,
                "target_entity_type": selected.target_entity_type,
                "qualified_target": selected.qualified_target,
                "engine_version": MAPPING_ENGINE_VERSION,
                "evidence": selected.evidence.summary(),
                "score_components": selected.score.to_dict()["components"],
            },
        )

    def _build_profile(
        self,
        schema: SourceSchema,
        source_entity: str,
        target_entity_type: str,
        field_mappings: Sequence[FieldMapping],
    ) -> MappingProfile:
        """Assemble the profile with a deterministic identity (Step 28)."""
        mapping_id = self._build_mapping_id(
            schema=schema,
            source_entity=source_entity,
            target_entity_type=target_entity_type,
        )

        return MappingProfile(
            mapping_id=mapping_id,
            source_system_id=schema.source_system_id,
            source_entity=source_entity,
            target_entity_type=target_entity_type,
            source_schema_id=schema.schema_id,
            # Every generated mapping is a SUGGESTION until a human approves
            # the profile as a whole, even when individual fields were
            # auto-accepted on strong evidence.
            status=MappingStatus.SUGGESTED,
            field_mappings=tuple(field_mappings),
            metadata={
                "canonical_model_id": self._model.model_id,
                "canonical_model_version": self._model.version,
                "canonical_model_identity": self._model.identity,
                "mapping_engine_version": MAPPING_ENGINE_VERSION,
                "config_fingerprint": self._engine.config_fingerprint,
                "alias_index_version": self._engine.alias_index.version,
                "source_schema_hash": schema.schema_hash,
                "generated_by": "erp_pipeline.mapping",
                # Stated as data so a stored profile cannot be mistaken for
                # something that has already been applied to records.
                "applied_to_data": False,
            },
        )

    def _build_mapping_id(
        self,
        schema: SourceSchema,
        source_entity: str,
        target_entity_type: str,
    ) -> str:
        """Deterministic profile identity.

        Derived from what genuinely determines the mapping's content: the
        source system, the source schema snapshot, the source entity, the
        canonical target, and the engine configuration that produced it.

        Explicitly NOT derived from a timestamp, a UUID or a path - a mapping
        regenerated tomorrow from the same inputs must be recognizable as the
        same mapping, and a mapping generated under different thresholds must
        not silently overwrite one generated under the old ones.
        """
        fingerprint = hash_json_payload(
            {
                "source_system_id": schema.source_system_id,
                "source_schema_id": schema.schema_id,
                "source_schema_hash": schema.schema_hash,
                "source_entity": source_entity,
                "target_entity_type": target_entity_type,
                "canonical_model": self._model.identity,
                "engine_version": MAPPING_ENGINE_VERSION,
                "config": self._engine.config_fingerprint,
            }
        )

        return normalize_identifier(
            f"{schema.source_system_id}.{source_entity}.{target_entity_type}."
            f"{fingerprint[:12]}"
        )

    # ------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------

    def _validate_all(
        self, profiles: Sequence[MappingProfile], schema: SourceSchema
    ) -> ValidationReport:
        findings = []
        for profile in profiles:
            findings.extend(validate_profile(profile, schema, self._model).findings)
        return ValidationReport(findings=tuple(findings))

    def validate(
        self, profile: MappingProfile, schema: SourceSchema
    ) -> ValidationReport:
        """Validate an externally supplied or edited profile."""
        return validate_profile(profile, schema, self._model)

    def _require_valid_overrides(
        self, schema: SourceSchema, overrides: Sequence[MappingOverride]
    ) -> None:
        """Fail fast on an override naming a field that does not exist.

        Raised rather than ignored: a silently dropped override means a
        reviewer's decision was discarded without telling them.
        """
        for override in overrides:
            find_source_field(schema, override.source_field)

    # ------------------------------------------------------------
    # Persistence (Step 34)
    # ------------------------------------------------------------

    def publish(
        self, result: MappingResult, catalog_service: Any
    ) -> tuple[MappingProfile, ...]:
        """Persist every generated profile through the Phase 2 catalog.

        Uses the existing ``save_mapping_profile``; no second mapping store is
        introduced. The catalog owns the upsert semantics, and this method adds
        no versioning logic of its own.
        """
        return tuple(
            catalog_service.save_mapping_profile(profile)
            for profile in result.profiles
        )

    @staticmethod
    def load(catalog_service: Any, mapping_id: str) -> MappingProfile:
        """Reload a persisted profile through the Phase 2 catalog."""
        return catalog_service.get_mapping_profile(mapping_id)


def generate_mapping(
    schema: SourceSchema,
    canonical_model: CanonicalTargetModel | None = None,
    options: MappingOptions | None = None,
    overrides: Sequence[MappingOverride] = (),
    rejected: Sequence[RejectedCandidate] = (),
) -> MappingResult:
    """Module-level convenience: map one schema with the default configuration."""
    return MappingService(canonical_model, options).generate(
        schema, overrides=overrides, rejected=rejected
    )


__all__ = [
    "MappingService",
    "generate_mapping",
]
