"""Physical collection and non-destructive migration tests using real Qdrant local."""
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models

from erp_pipeline.document_workspace import LibraryVectors, WorkspaceError, collection_route
from .test_document_workspace import workspace, schema, scope, upload, request_for


@pytest.fixture
def routed(workspace):
    class Embedder:
        def embed_one(self, representation):
            return SimpleNamespace(vector=[0.5, 0.4, 0.3])
    client = QdrantClient(":memory:")
    workspace.vectors = LibraryVectors(client, Embedder(), "legacy_test")
    yield workspace
    client.close()


def confirm(service, schema, key, **changes):
    staged = upload(service, key)
    request = request_for(staged, schema)
    request.update(changes)
    receipt = service.commit(scope(), staged["id"], request)
    assert receipt["status"] == "READY"
    return receipt


def as_legacy(service, receipt):
    client = service.vectors.client
    points, _ = client.scroll(receipt["vector_collection"], with_vectors=True)
    client.create_collection("legacy_test", vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
    client.upsert("legacy_test", [models.PointStruct(id=point.id, vector=point.vector, payload=point.payload) for point in points])
    doc = service.store.get(scope()["workspaceId"], receipt["document_id"])
    doc.pop("vector_collection")
    return service.store.put(scope()["workspaceId"], doc, doc["revision"])


def test_schema_routes_exist_before_vectors_and_stay_stable_across_schema_revisions(routed, schema):
    catalog = routed.catalog(scope())
    routes = catalog["entities"][0]["collection_routes"]
    assert len(routes) == 2 and routes[0]["searchable_documents"] == 0
    assert "medical_certificate" in routes[1]["document_types"]
    assert routed.vectors.client.get_collections().collections == []
    assert collection_route(scope(), schema, False) == collection_route(scope(), {**schema, "revision": 99}, False)
    assert collection_route(scope(), schema, False) != collection_route(scope(workspace="other"), schema, False)
    assert not catalog["summary_contains_document_content"]


def test_entity_restricted_and_workspace_physical_separation(routed, schema):
    normal = confirm(routed, schema, "normal-one", share_with_schema_roles=True)
    restricted = confirm(routed, schema, "restricted-one", document_type="medical_certificate", share_with_schema_roles=True)
    second_schema = routed.import_schema(scope(), dict(source_system_id="crm", source_entity="customers", primary_key="name", fields=[dict(name="name", type="string", required=True)], document_types=["registration_form"]))
    customer = confirm(routed, second_schema, "customer-one", form_fields={"name": "Example Customer"})
    assert len({normal["vector_collection"], restricted["vector_collection"], customer["vector_collection"]}) == 3
    assert routed.search(scope(), "Member")["count"] == 3
    visible = routed.search(scope("another-owner"), "Member")["items"]
    assert [item["document_id"] for item in visible] == [normal["document_id"]]
    assert routed.search(scope(workspace="other"), "Member")["count"] == 0
    summary = routed.catalog(scope("another-owner"))
    assert sum(route["searchable_documents"] for route in summary["collections"]) == 1
    assert "Example Customer" not in str(summary) and "form_fields" not in str(summary)


def test_restricted_schema_cannot_offer_or_use_standard_destination(routed, schema):
    restricted_schema = {**schema, "minimum_sensitivity": "restricted"}
    raw = {key: value for key, value in restricted_schema.items() if key not in {"id", "kind", "owner_id", "created_at", "updated_at", "revision"}}
    saved = routed.import_schema(scope(), {**raw, "expected_revision": schema["revision"]})
    receipt = confirm(routed, saved, "restricted-schema", share_with_schema_roles=True)
    assert "_restricted_" in receipt["vector_collection"]
    assert routed.catalog(scope())["entities"][0]["collection_routes"][0]["accepts_uploads"] is False
    assert routed.search(scope("another-owner"), "Member")["count"] == 0


def test_arbitrary_collection_inputs_rejected(routed, schema):
    staged = upload(routed)
    with pytest.raises(WorkspaceError):
        routed.commit(scope(), staged["id"], {**request_for(staged, schema), "vector_collection": "erp_vectors_hot"})
    with pytest.raises(WorkspaceError):
        routed.migrate_collection(scope(), {"document_id": "x", "review_confirmed": True, "collection": "erp_vectors_hot"})


def test_synthetic_restricted_cloud_storage_requires_server_workspace_and_explicit_source_review(routed, schema):
    routed.vectors.restricted_allowed = False
    staged = upload(routed, "synthetic-medical")
    body = {**request_for(staged, schema), "document_type": "medical_certificate", "synthetic_source_confirmed": True}
    denied = routed.commit(scope(), staged["id"], body)
    assert denied["status"] == "INDEX_FAILED" and denied["error_code"] == "STORAGE_POLICY_DENIED"
    assert routed.search(scope(), "Member")["count"] == 0
    routed.vectors.synthetic_workspaces = frozenset({scope()["workspaceId"]})
    route = routed.catalog(scope())["entities"][0]["collection_routes"][1]
    assert route["requires_synthetic_confirmation"] is True
    with pytest.raises(WorkspaceError) as missing:
        routed.commit(scope(), staged["id"], {**body, "expected_revision": denied["revision"], "synthetic_source_confirmed": False})
    assert missing.value.code == "SYNTHETIC_SOURCE_CONFIRMATION_REQUIRED"
    with pytest.raises(WorkspaceError):
        routed.commit(scope(), staged["id"], {**body, "expected_revision": denied["revision"], "synthetic_source_confirmed": "true"})
    done = routed.commit(scope(), staged["id"], {**body, "expected_revision": denied["revision"]})
    assert done["status"] == "READY" and "_restricted_" in done["vector_collection"]
    assert routed.resolve(scope(), done["document_id"])["synthetic_source_confirmed"] is True
    assert routed.search(scope("another-owner"), "Member")["count"] == 0
    assert routed.search(scope(workspace="another-workspace"), "Member")["count"] == 0


def test_restricted_storage_default_remains_on_premises_only(routed, schema):
    routed.vectors.restricted_allowed = False
    staged = upload(routed, "real-policy-default")
    receipt = routed.commit(scope(), staged["id"], {**request_for(staged, schema), "document_type": "medical_certificate"})
    assert receipt["status"] == "INDEX_FAILED" and receipt["error_code"] == "STORAGE_POLICY_DENIED"
    assert routed.catalog(scope())["entities"][0]["collection_routes"][1]["requires_synthetic_confirmation"] is False


def test_legacy_search_migration_retains_points_version_and_records(routed, schema):
    receipt = confirm(routed, schema, "migration-good")
    document = as_legacy(routed, receipt)
    assert routed.search(scope(), "Member")["count"] == 1
    assert routed.catalog(scope())["legacy_current_documents"] == 1
    migrated = routed.migrate_collection(scope(), {"document_id": document["id"], "expected_revision": document["revision"], "review_confirmed": True})
    assert migrated["status"] == "MIGRATED" and migrated["version"] == 1 and migrated["deleted_vectors"] == 0
    assert routed.vectors.client.count("legacy_test").count == 1
    assert routed.search(scope(), "Member")["items"][0]["vector_collection"] == receipt["vector_collection"]
    assert routed.resolve(scope(), document["id"])["form_fields"]["age"] == 21
    assert routed.catalog(scope())["legacy_current_documents"] == 0
    assert sum(item["searchable_documents"] for item in routed.catalog(scope())["collections"]) == 1
    current = routed.store.get(scope()["workspaceId"], document["id"])
    assert routed.migrate_collection(scope(), {"document_id": document["id"], "expected_revision": current["revision"], "review_confirmed": True})["status"] == "ALREADY_ROUTED"


def test_failed_migration_never_promotes_or_deletes_legacy(routed, schema):
    receipt = confirm(routed, schema, "migration-fail")
    document = as_legacy(routed, receipt)
    index = routed.vectors.index
    routed.vectors.index = lambda *args: (_ for _ in ()).throw(RuntimeError("fixture failure"))
    with pytest.raises(WorkspaceError) as error:
        routed.migrate_collection(scope(), {"document_id": document["id"], "expected_revision": document["revision"], "review_confirmed": True})
    assert error.value.code == "COLLECTION_MIGRATION_FAILED"
    assert routed.store.get(scope()["workspaceId"], document["id"])["revision"] == document["revision"]
    assert routed.search(scope(), "Member")["count"] == 1
    routed.vectors.index = index


def test_migration_requires_owner_admin_review_and_current_revision(routed, schema):
    receipt = confirm(routed, schema, "migration-permissions")
    doc = as_legacy(routed, receipt)
    request = {"document_id": doc["id"], "expected_revision": doc["revision"], "review_confirmed": True}
    cases = [(scope("other"), request), ({**scope(), "canManage": False}, request), ({**scope(), "readOnly": True}, request), (scope(), {**request, "expected_revision": 0}), (scope(), {**request, "review_confirmed": False})]
    for caller, body in cases:
        with pytest.raises(WorkspaceError):
            routed.migrate_collection(caller, body)


def test_concurrent_document_change_fences_migration_promotion(routed, schema):
    receipt = confirm(routed, schema, "migration-conflict")
    doc = as_legacy(routed, receipt)
    original = routed.vectors.index
    def change(*args):
        original(*args)
        routed.store.put(scope()["workspaceId"], {**doc, "pending_upload_id": "concurrent-upload"}, doc["revision"])
    routed.vectors.index = change
    with pytest.raises(WorkspaceError):
        routed.migrate_collection(scope(), {"document_id": doc["id"], "expected_revision": doc["revision"], "review_confirmed": True})
    current = routed.store.get(scope()["workspaceId"], doc["id"])
    assert "vector_collection" not in current and current["pending_upload_id"] == "concurrent-upload"
    assert routed.search(scope(), "Member")["count"] == 1


def test_shared_standard_version_becomes_restricted_only_after_successful_index(routed, schema):
    receipt = confirm(routed, schema, "shared-old-version", share_with_schema_roles=True)
    old = routed.store.get(scope()["workspaceId"], receipt["document_id"])
    assert routed.search(scope("other-owner"), "Member")["count"] == 1
    raw = {key: value for key, value in schema.items() if key not in {"id", "kind", "owner_id", "created_at", "updated_at", "revision"}}
    restricted = routed.import_schema(scope(), {**raw, "minimum_sensitivity": "restricted", "expected_revision": schema["revision"]})
    staged = upload(routed, "restricted-new-version", b"Updated confidential form")
    request = request_for(staged, restricted)
    request.update(mode="version", document_id=old["id"], document_revision=old["revision"], share_with_schema_roles=True)
    index = routed.vectors.index
    def fail_after_read(caller, upload, candidate, text):
        # New privacy/fields must not replace the old receipt while pending.
        assert routed.resolve(scope("other-owner"), old["id"])["version"] == 1
        raise RuntimeError("isolated failed replacement")
    routed.vectors.index = fail_after_read
    failed = routed.commit(scope(), staged["id"], request)
    assert failed["status"] == "INDEX_FAILED"
    assert routed.search(scope("other-owner"), "Member")["count"] == 1
    catalog = routed.catalog(scope())
    assert catalog["collections"][0]["searchable_documents"] == 1
    assert catalog["collections"][1]["searchable_documents"] == 0
    current = routed.store.get(scope()["workspaceId"], old["id"])
    routed.vectors.index = index
    done = routed.commit(scope(), staged["id"], {**request, "expected_revision": failed["revision"], "document_revision": current["revision"]})
    assert done["status"] == "READY" and done["version"] == 2
    assert done["vector_collection"] != receipt["vector_collection"]
    assert routed.search(scope("other-owner"), "Member")["count"] == 0
    with pytest.raises(WorkspaceError):
        routed.resolve(scope("other-owner"), old["id"])
    assert routed.search(scope(), "Member")["items"][0]["vector_collection"] == done["vector_collection"]
    assert routed.vectors.client.count(receipt["vector_collection"]).count == 1
    assert [route["searchable_documents"] for route in routed.catalog(scope())["collections"]] == [0, 1]


def test_embedding_dimension_change_does_not_promote_new_version(routed, schema):
    receipt = confirm(routed, schema, "embedding-old-version")
    old = routed.store.get(scope()["workspaceId"], receipt["document_id"])
    staged = upload(routed, "embedding-new-version", b"A new file must not replace the current index on dimension mismatch")
    previous_embed = routed.vectors._embed
    routed.vectors._embed = lambda *args: [0.5, 0.4]
    request = request_for(staged, schema)
    request.update(mode="version", document_id=old["id"], document_revision=old["revision"])
    failed = routed.commit(scope(), staged["id"], request)
    assert failed["status"] == "INDEX_FAILED" and failed["error_code"] == "EMBEDDING_DIMENSION_CHANGED"
    routed.vectors._embed = previous_embed
    assert routed.resolve(scope(), old["id"])["version"] == 1
    assert routed.search(scope(), "Member")["items"][0]["upload_id"] == receipt["id"]


def test_missing_collection_reports_incomplete_retrieval_not_zero_matches(routed, schema):
    receipt = confirm(routed, schema, "missing-collection")
    exists = routed.vectors.client.collection_exists
    routed.vectors.client.collection_exists = lambda name: False if name == receipt["vector_collection"] else exists(name)
    with pytest.raises(WorkspaceError) as error:
        routed.search(scope(), "Member")
    assert error.value.code == "COLLECTION_UNAVAILABLE" and error.value.status == 503


def test_unknown_stored_collection_locator_fails_closed(routed, schema):
    receipt = confirm(routed, schema, "unknown-locator")
    doc = routed.store.get(scope()["workspaceId"], receipt["document_id"])
    routed.store.put(scope()["workspaceId"], {**doc, "vector_collection": "erp_vectors_hot"}, doc["revision"])
    with pytest.raises(WorkspaceError) as error:
        routed.search(scope(), "Member")
    assert error.value.code == "COLLECTION_ROUTE_INVALID"


def test_revision_filter_gets_integer_index_before_upsert(routed, schema, monkeypatch):
    events = []
    create_index = routed.vectors.client.create_payload_index
    upsert = routed.vectors.client.upsert
    def indexed(collection, field, kind, **kwargs):
        events.append((field, kind, kwargs.get("wait")))
        return create_index(collection, field, kind, **kwargs)
    def stored(*args, **kwargs):
        assert ("index_revision", models.PayloadSchemaType.INTEGER, True) in events
        return upsert(*args, **kwargs)
    monkeypatch.setattr(routed.vectors.client, "create_payload_index", indexed)
    monkeypatch.setattr(routed.vectors.client, "upsert", stored)
    confirm(routed, schema, "integer-revision-index")


def test_existing_collection_missing_revision_index_is_repaired(routed, schema, monkeypatch):
    first = confirm(routed, schema, "before-revision-index-repair")
    calls = []
    create_index = routed.vectors.client.create_payload_index
    def indexed(collection, field, kind, **kwargs):
        calls.append((collection, field, kind, kwargs.get("wait")))
        return create_index(collection, field, kind, **kwargs)
    monkeypatch.setattr(routed.vectors.client, "create_payload_index", indexed)
    second = confirm(routed, schema, "after-revision-index-repair")
    assert first["vector_collection"] == second["vector_collection"]
    assert (first["vector_collection"], "index_revision", models.PayloadSchemaType.INTEGER, True) in calls
    assert routed.vectors.client.count(first["vector_collection"]).count == 2


def test_incompatible_revision_index_is_not_silently_replaced(routed, schema, monkeypatch):
    first = confirm(routed, schema, "before-incompatible-revision-index")
    get_collection = routed.vectors.client.get_collection
    def incompatible(collection):
        info = get_collection(collection)
        return SimpleNamespace(config=info.config, payload_schema={"index_revision": SimpleNamespace(data_type=models.PayloadSchemaType.KEYWORD)})
    monkeypatch.setattr(routed.vectors.client, "get_collection", incompatible)
    staged = upload(routed, "incompatible-revision-index")
    failed = routed.commit(scope(), staged["id"], request_for(staged, schema))
    assert failed["status"] == "INDEX_FAILED" and failed["error_code"] == "COLLECTION_INDEX_SCHEMA_CHANGED"
    assert routed.vectors.client.count(first["vector_collection"]).count == 1
