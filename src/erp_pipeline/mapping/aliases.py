"""The explicit alias index.

Aliases are the only place in this engine where domain knowledge lives, and
they are kept here - declared on the canonical model, indexed by this module -
rather than buried inside scoring code (Step 8). That matters for three
reasons:

* a domain expert can review them without reading Python;
* they are versioned, so a mapping generated before ``cust_no`` was declared
  an alias is distinguishable from one generated after;
* an alias match is reportable as evidence: "the canonical field declares
  ``cust_no`` as an alias" is an explanation, whereas "similarity 0.95" is not.

An alias is a strong claim - it says two spellings mean the same business
concept - so the engine never invents one. Everything it knows, the canonical
model told it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from erp_pipeline.mapping.canonical_model import CanonicalTargetModel
from erp_pipeline.mapping.normalization import (
    NormalizationConfig,
    normalized_key,
)

#: Version of the alias INDEXING behaviour (how declared aliases are folded
#: into lookup keys), distinct from the canonical model's own version, which
#: covers the alias CONTENT.
ALIAS_INDEX_VERSION = "1.0"


@dataclass(frozen=True)
class AliasHit:
    """One alias lookup result."""

    entity_type: str
    field_name: str
    #: The alias exactly as the canonical model declared it, so the evidence
    #: quotes the reviewable source rather than a normalized derivative.
    declared_alias: str
    #: Every declared spelling that normalizes to the same key. Several
    #: spellings routinely collapse together (``cust_no`` and ``customer_no``
    #: both normalize to ``customer_number``), and reporting whichever happened
    #: to be declared first would show a reviewer an alias their source does
    #: not contain.
    declared_spellings: tuple[str, ...] = ()

    @property
    def qualified_target(self) -> str:
        return f"{self.entity_type}.{self.field_name}"

    def best_spelling_for(self, source_name: str) -> str:
        """The declared spelling closest to what the source actually wrote."""
        for spelling in self.declared_spellings:
            if spelling == source_name:
                return spelling

        lowered = source_name.lower()
        for spelling in self.declared_spellings:
            if spelling.lower() == lowered:
                return spelling

        return self.declared_alias


class AliasIndex:
    """Fast, deterministic lookup from a source name to canonical fields.

    Both the canonical field's own name and each declared alias are indexed
    under their NORMALIZED key, so ``customerId``, ``customer_id``,
    ``CUSTOMER_ID`` and ``customer-id`` all reach the same entry without the
    canonical model having to list every spelling.

    A single alias may legitimately point at several canonical fields -
    ``customer_id`` belongs to both ``invoice`` and ``customer`` - so lookups
    return every hit and let entity context break the tie. Silently picking one
    here would hide exactly the ambiguity the engine exists to surface.
    """

    def __init__(
        self,
        model: CanonicalTargetModel,
        normalization: NormalizationConfig | None = None,
    ) -> None:
        self._model = model
        self._normalization = normalization
        self._field_aliases: dict[str, list[AliasHit]] = {}
        self._entity_aliases: dict[str, list[str]] = {}
        self._build()

    @property
    def version(self) -> str:
        return ALIAS_INDEX_VERSION

    def _build(self) -> None:
        for entity in self._model.entities:
            # An entity is findable by its own type and by every declared alias.
            for spelling in (entity.entity_type, *entity.aliases):
                key = normalized_key(spelling, self._normalization)
                if key:
                    bucket = self._entity_aliases.setdefault(key, [])
                    if entity.entity_type not in bucket:
                        bucket.append(entity.entity_type)

            for canonical_field in entity.fields:
                spellings = (canonical_field.name, *canonical_field.aliases)
                for spelling in spellings:
                    key = normalized_key(spelling, self._normalization)
                    if not key:
                        continue

                    hits = self._field_aliases.setdefault(key, [])
                    existing = next(
                        (
                            index
                            for index, hit in enumerate(hits)
                            if hit.entity_type == entity.entity_type
                            and hit.field_name == canonical_field.name
                        ),
                        None,
                    )

                    if existing is None:
                        hits.append(
                            AliasHit(
                                entity_type=entity.entity_type,
                                field_name=canonical_field.name,
                                declared_alias=spelling,
                                declared_spellings=(spelling,),
                            )
                        )
                    else:
                        # Another declared spelling collapsing onto the same
                        # normalized key. Kept so the evidence can quote the
                        # one the source actually used.
                        previous = hits[existing]
                        hits[existing] = AliasHit(
                            entity_type=previous.entity_type,
                            field_name=previous.field_name,
                            declared_alias=previous.declared_alias,
                            declared_spellings=previous.declared_spellings
                            + (spelling,),
                        )

    def lookup_field(self, source_name: str) -> tuple[AliasHit, ...]:
        """Every canonical field a source field name could denote."""
        key = normalized_key(source_name, self._normalization)
        return tuple(self._field_aliases.get(key, ()))

    def lookup_field_in_entity(
        self, source_name: str, entity_type: str
    ) -> AliasHit | None:
        """The alias hit within one canonical entity, if any."""
        for hit in self.lookup_field(source_name):
            if hit.entity_type == entity_type:
                return hit
        return None

    def lookup_entity(self, source_entity_name: str) -> tuple[str, ...]:
        """Canonical entity types a source entity name could denote."""
        key = normalized_key(source_entity_name, self._normalization)
        return tuple(self._entity_aliases.get(key, ()))

    def declared_aliases_for(
        self, entity_type: str, field_name: str
    ) -> tuple[str, ...]:
        """The aliases a canonical field declares, for documentation output."""
        canonical_field = self._model.field(entity_type, field_name)
        return tuple(canonical_field.aliases) if canonical_field else ()

    def to_dict(self) -> dict[str, object]:
        """Structural summary - counts and keys, never a mapping decision."""
        return {
            "alias_index_version": self.version,
            "canonical_model": self._model.identity,
            "indexed_field_keys": len(self._field_aliases),
            "indexed_entity_keys": len(self._entity_aliases),
            "ambiguous_field_keys": sorted(
                key for key, hits in self._field_aliases.items() if len(hits) > 1
            ),
        }


def build_alias_index(
    model: CanonicalTargetModel,
    normalization: NormalizationConfig | None = None,
) -> AliasIndex:
    return AliasIndex(model, normalization)


__all__ = [
    "ALIAS_INDEX_VERSION",
    "AliasHit",
    "AliasIndex",
    "build_alias_index",
]
