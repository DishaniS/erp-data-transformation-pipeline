"""Post-audit remediation: tier locations and payload-index durability.

FINDING 1 — the constraint enforced against a stale map
------------------------------------------------------
``StoragePolicy`` restricts RESTRICTED data to on-premises tiers and the router
genuinely enforces it. But the location map was a code-level constant declaring
all three tiers on-premises, written when that was true. Once HOT and WARM moved
to managed Qdrant the control kept passing while excluding nothing.

A compliance control reading a stale constant is worse than no control: it
reports success while delivering nothing. These tests pin the corrected
behaviour - locations come from configuration, and a deployment with no
on-premises tier REFUSES restricted data instead of quietly writing it to cloud.

FINDING 2 — payload indexes that only existed by hand
-----------------------------------------------------
Managed Qdrant refuses a filtered search on an unindexed field with a 400.
Nothing in the code created the indexes, so recreating a collection would have
broken every filtered search. These tests pin that both tiers now ensure them,
idempotently, from the same ``FILTERABLE_FIELDS`` the filter builder uses.
"""

from __future__ import annotations

import pytest

from erp_pipeline.runtime.settings import (
    ConfigurationError,
    QdrantSettings,
    StorageLocationSettings,
)
from erp_pipeline.schemas.enums import SensitivityLevel
from erp_pipeline.storage.filters import FILTERABLE_FIELDS
from erp_pipeline.storage.models import StorageLocation, StorageTier
from erp_pipeline.storage.payload_indexes import (
    ensure_payload_indexes,
    required_payload_indexes,
)
from erp_pipeline.storage.storage_policy import StoragePolicy

ALL_CLOUD = {
    StorageTier.HOT: StorageLocation.EXTERNAL,
    StorageTier.WARM: StorageLocation.EXTERNAL,
    StorageTier.COLD: StorageLocation.EXTERNAL,
}
ALL_ON_PREM = {
    StorageTier.HOT: StorageLocation.ON_PREMISES,
    StorageTier.WARM: StorageLocation.ON_PREMISES,
    StorageTier.COLD: StorageLocation.ON_PREMISES,
}


# ======================================================================
# FINDING 1 - configuration
# ======================================================================


class TestTierLocationsAreConfigurable:
    def test_locations_default_to_on_premises_without_cloud_qdrant(self, monkeypatch):
        """A local deployment keeps behaving exactly as it did."""
        for name in ("ERP_STORAGE_HOT_LOCATION", "ERP_STORAGE_WARM_LOCATION",
                     "ERP_STORAGE_COLD_LOCATION", "ERP_QDRANT_URL",
                     "ERP_QDRANT_API_KEY", "ERP_QDRANT_MODE"):
            monkeypatch.delenv(name, raising=False)

        settings = StorageLocationSettings.from_environment(
            QdrantSettings.from_environment()
        )

        assert settings.describe() == {
            "hot": "on_premises",
            "warm": "on_premises",
            "cold": "on_premises",
        }

    def test_hot_and_warm_are_inferred_external_from_cloud_qdrant(self, monkeypatch):
        """The inference that makes this control work without being remembered.

        A cluster addressed by URL with an API key is not on-premises. Requiring
        an operator to declare that a second time is how the map goes stale.
        """
        monkeypatch.setenv("ERP_QDRANT_MODE", "cloud")
        monkeypatch.setenv("ERP_QDRANT_URL", "https://cluster.example.test")
        monkeypatch.setenv("ERP_QDRANT_API_KEY", "unused-in-this-test")
        for name in ("ERP_STORAGE_HOT_LOCATION", "ERP_STORAGE_WARM_LOCATION"):
            monkeypatch.delenv(name, raising=False)

        settings = StorageLocationSettings.from_environment(
            QdrantSettings.from_environment()
        )

        assert settings.hot == "external"
        assert settings.warm == "external"

    def test_cold_must_be_declared_because_it_cannot_be_inferred(self, monkeypatch):
        """A mounted cloud share looks exactly like a local disk.

        Azure Files is cloud storage, and nothing in a filesystem path says so.
        """
        monkeypatch.setenv("ERP_QDRANT_MODE", "cloud")
        monkeypatch.setenv("ERP_QDRANT_URL", "https://cluster.example.test")
        monkeypatch.setenv("ERP_QDRANT_API_KEY", "unused-in-this-test")
        monkeypatch.delenv("ERP_STORAGE_COLD_LOCATION", raising=False)

        inferred = StorageLocationSettings.from_environment(
            QdrantSettings.from_environment()
        )

        assert inferred.cold == "on_premises"

        monkeypatch.setenv("ERP_STORAGE_COLD_LOCATION", "external")
        declared = StorageLocationSettings.from_environment(
            QdrantSettings.from_environment()
        )

        assert declared.cold == "external"

    def test_an_explicit_declaration_overrides_the_inference(self, monkeypatch):
        """Requirement: a local deployment can still say on_premises."""
        monkeypatch.setenv("ERP_QDRANT_MODE", "cloud")
        monkeypatch.setenv("ERP_QDRANT_URL", "https://cluster.example.test")
        monkeypatch.setenv("ERP_QDRANT_API_KEY", "unused-in-this-test")
        monkeypatch.setenv("ERP_STORAGE_HOT_LOCATION", "on_premises")

        settings = StorageLocationSettings.from_environment(
            QdrantSettings.from_environment()
        )

        assert settings.hot == "on_premises"

    def test_a_malformed_location_is_refused_not_guessed(self):
        """Guessing here means guessing whether restricted data may be stored."""
        with pytest.raises(ConfigurationError) as failure:
            StorageLocationSettings(hot="onprem").validate()

        assert "ERP_STORAGE_HOT_LOCATION" in str(failure.value)

    def test_the_tier_map_uses_the_real_enum(self):
        mapping = StorageLocationSettings(
            hot="external", warm="external", cold="on_premises"
        ).as_tier_map()

        assert mapping[StorageTier.HOT] is StorageLocation.EXTERNAL
        assert mapping[StorageTier.COLD] is StorageLocation.ON_PREMISES

    def test_an_incomplete_location_map_is_refused_at_construction(self):
        """'Did not say' is refused, not defaulted around.

        A tier with no declared location cannot be checked against the
        sensitivity constraints, so the policy will not build at all.
        """
        from erp_pipeline.storage.errors import StorageConfigurationError

        with pytest.raises(StorageConfigurationError) as failure:
            StoragePolicy(tier_locations={StorageTier.HOT: StorageLocation.ON_PREMISES})

        assert "does not declare a location" in str(failure.value)


# ======================================================================
# FINDING 1 - routing behaviour
# ======================================================================


def route(policy, sensitivity):
    """Route one record, returning the decision or raising PolicyViolationError."""
    from erp_pipeline.storage.vector_router import StoragePolicyRouter
    from erp_pipeline.storage.models import StorageRoutingContext

    router = StoragePolicyRouter(policy)

    return router.route(
        StorageRoutingContext(
            representation_id="ai:document:remediation_probe",
            sensitivity=sensitivity,
        )
    )


class TestRestrictedDataUnderCloudTiers:
    def test_restricted_is_rejected_when_every_tier_is_external(self):
        """The headline remediation.

        The Azure deployment has no on-premises tier. Restricted data must fail
        closed rather than land in cloud storage.
        """
        from erp_pipeline.storage.errors import PolicyViolationError

        with pytest.raises(PolicyViolationError) as failure:
            route(StoragePolicy(tier_locations=ALL_CLOUD), SensitivityLevel.RESTRICTED)

        message = str(failure.value)

        assert "prohibited" in message or "nowhere" in message

    @pytest.mark.parametrize("tier", [StorageTier.HOT, StorageTier.WARM, StorageTier.COLD])
    def test_restricted_never_lands_in_any_external_tier(self, tier):
        """Checked per tier so a partial regression cannot hide."""
        from erp_pipeline.storage.errors import PolicyViolationError

        try:
            decision = route(
                StoragePolicy(tier_locations=ALL_CLOUD), SensitivityLevel.RESTRICTED
            )
        except PolicyViolationError:
            return  # refused outright - the correct outcome

        assert decision.selected_tier is not tier, (
            f"restricted data was routed to {tier.value}, which is EXTERNAL"
        )

    def test_restricted_succeeds_when_an_on_premises_tier_exists(self):
        """The control must permit, not merely forbid."""
        decision = route(
            StoragePolicy(tier_locations=ALL_ON_PREM), SensitivityLevel.RESTRICTED
        )

        assert decision.selected_tier in {StorageTier.HOT, StorageTier.WARM, StorageTier.COLD}

    def test_restricted_routes_only_to_the_on_premises_tier_in_a_mixed_deployment(self):
        mixed = {
            StorageTier.HOT: StorageLocation.EXTERNAL,
            StorageTier.WARM: StorageLocation.ON_PREMISES,
            StorageTier.COLD: StorageLocation.EXTERNAL,
        }

        decision = route(StoragePolicy(tier_locations=mixed), SensitivityLevel.RESTRICTED)

        assert decision.selected_tier is StorageTier.WARM

    @pytest.mark.parametrize(
        "sensitivity",
        [SensitivityLevel.PUBLIC, SensitivityLevel.INTERNAL, SensitivityLevel.CONFIDENTIAL],
    )
    def test_lower_sensitivities_still_route_normally_on_cloud_tiers(self, sensitivity):
        """Non-restricted data is unaffected. The fix must not break the service."""
        decision = route(StoragePolicy(tier_locations=ALL_CLOUD), sensitivity)

        assert decision.selected_tier in {StorageTier.HOT, StorageTier.WARM, StorageTier.COLD}

    def test_the_restricted_policy_itself_is_unchanged(self):
        """RESTRICTED still means on-premises-only. The meaning was not softened."""
        policy = StoragePolicy()

        assert policy.requires_on_premises(SensitivityLevel.RESTRICTED) is True
        assert policy.requires_on_premises(SensitivityLevel.CONFIDENTIAL) is False


class TestStrictestWinsIsUnchanged:
    """The remediation must not have touched sensitivity resolution."""

    def test_strictest_wins_across_declarations(self):
        from erp_pipeline.schemas.sensitivity import resolve

        assert resolve(
            artifact=SensitivityLevel.PUBLIC, job=SensitivityLevel.RESTRICTED
        ) is SensitivityLevel.RESTRICTED

    def test_the_default_is_still_internal(self):
        from erp_pipeline.schemas.sensitivity import DEFAULT_SENSITIVITY, resolve

        assert resolve() is DEFAULT_SENSITIVITY is SensitivityLevel.INTERNAL


# ======================================================================
# FINDING 2 - payload indexes
# ======================================================================


class FakeQdrant:
    """Records what was asked of it, and can simulate partial index state."""

    def __init__(self, collections=("erp_vectors_hot",), indexed=(), fail_on=()):
        self._collections = list(collections)
        self._indexed = set(indexed)
        self._fail_on = set(fail_on)
        self.created: list[str] = []

    def get_collections(self):
        class R:
            pass

        r = R()
        r.collections = [type("C", (), {"name": n})() for n in self._collections]

        return r

    def get_collection(self, collection_name):
        return type("Info", (), {"payload_schema": {f: object() for f in self._indexed}})()

    def create_payload_index(self, collection_name, field_name, field_schema, wait=True):
        if field_name in self._fail_on:
            raise RuntimeError("simulated Qdrant failure")

        self.created.append(field_name)
        self._indexed.add(field_name)


class TestPayloadIndexesAreEnsured:
    def test_the_field_list_is_the_canonical_one(self):
        """One list, not two. A duplicate would drift and 400 on a live filter."""
        assert required_payload_indexes() == tuple(FILTERABLE_FIELDS)
        assert len(required_payload_indexes()) == len(FILTERABLE_FIELDS)

    def test_all_indexes_are_created_on_a_bare_collection(self):
        client = FakeQdrant()

        report = ensure_payload_indexes(client, "erp_vectors_hot")

        assert sorted(report["created"]) == sorted(FILTERABLE_FIELDS)
        assert report["failed"] == {}

    def test_only_the_gaps_are_created_on_a_partially_indexed_collection(self):
        present = list(FILTERABLE_FIELDS)[:5]
        client = FakeQdrant(indexed=present)

        report = ensure_payload_indexes(client, "erp_vectors_hot")

        assert sorted(report["already_present"]) == sorted(present)
        assert set(report["created"]) == set(FILTERABLE_FIELDS) - set(present)

    def test_nothing_is_created_when_all_indexes_exist(self):
        client = FakeQdrant(indexed=FILTERABLE_FIELDS)

        report = ensure_payload_indexes(client, "erp_vectors_hot")

        assert report["created"] == []
        assert len(report["already_present"]) == len(FILTERABLE_FIELDS)

    def test_repeated_calls_are_idempotent(self):
        client = FakeQdrant()

        first = ensure_payload_indexes(client, "erp_vectors_hot")
        second = ensure_payload_indexes(client, "erp_vectors_hot")

        assert len(first["created"]) == len(FILTERABLE_FIELDS)
        assert second["created"] == []

    def test_a_missing_collection_is_reported_not_raised(self):
        client = FakeQdrant(collections=())

        report = ensure_payload_indexes(client, "erp_vectors_hot")

        assert report.get("missing_collection") is True
        assert report["created"] == []

    def test_an_index_failure_is_reported_and_does_not_stop_the_others(self):
        """A degraded filter beats a service that refuses to start."""
        client = FakeQdrant(fail_on={"sensitivity"})

        report = ensure_payload_indexes(client, "erp_vectors_hot")

        assert "sensitivity" in report["failed"]
        assert len(report["created"]) == len(FILTERABLE_FIELDS) - 1

    def test_an_already_exists_error_counts_as_present(self):
        class AlreadyExists(FakeQdrant):
            def create_payload_index(self, collection_name, field_name, field_schema, wait=True):
                raise RuntimeError("Index already exists for field")

        report = ensure_payload_indexes(AlreadyExists(), "erp_vectors_hot")

        assert report["failed"] == {}
        assert len(report["already_present"]) == len(FILTERABLE_FIELDS)

    def test_a_new_schema_field_can_be_indexed_without_a_code_list_change(self):
        client = FakeQdrant()

        report = ensure_payload_indexes(
            client,
            "erp_vectors_hot",
            field_names=("shift_code",),
        )

        assert report["created"] == ["shift_code"]


class TestTiersEnsureIndexesOnBothPaths:
    """The defect this guards against: the early return skipped indexing.

    An existing collection - which is the state of every already-deployed
    cluster - would never have been indexed.
    """

    @pytest.mark.parametrize("tier_module,cls_name,collection", [
        ("erp_pipeline.storage.hot_tier", "QdrantHotTier", "erp_vectors_hot"),
        ("erp_pipeline.storage.warm_tier", "QdrantWarmTier", "erp_vectors_warm"),
    ])
    def test_an_existing_collection_still_gets_indexes(self, tier_module, cls_name, collection):
        import importlib

        module = importlib.import_module(tier_module)
        tier_cls = getattr(module, cls_name)
        client = FakeQdrant(collections=(collection,))

        tier_cls(client, collection, 384).ensure_collection()

        assert sorted(client.created) == sorted(FILTERABLE_FIELDS), (
            "an existing collection was left without payload indexes"
        )

    def test_no_new_collection_name_is_ever_introduced(self):
        """Architecture guard: tiers are the only physical collections."""
        client = FakeQdrant(collections=("erp_vectors_hot",))

        ensure_payload_indexes(client, "erp_vectors_hot")

        assert client._collections == ["erp_vectors_hot"]
