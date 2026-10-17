"""Opt-in real-server checks; never run against a shared/cloud Qdrant."""
import os
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from erp_pipeline.document_workspace import LibraryVectors
from .test_document_workspace import workspace, schema, scope, upload, request_for


def test_strict_server_revision_filter_and_existing_collection_repair(workspace, schema):
    url = os.getenv("PIPELINE_STRICT_QDRANT_TEST_URL", "")
    if not url:
        pytest.skip("Disposable strict-mode Qdrant test server is not configured")
    parsed = urlparse(url)
    assert parsed.scheme == "http" and parsed.hostname.startswith("lcwe-azure-strict-index-qa-")
    client = QdrantClient(url=url, timeout=30)
    class Embedder:
        def embed_one(self, representation):
            return SimpleNamespace(vector=[0.5, 0.4, 0.3])
    workspace.vectors = LibraryVectors(client, Embedder(), "isolated_strict_test")
    try:
        staged = upload(workspace, "strict-original")
        first = workspace.commit(scope(), staged["id"], request_for(staged, schema))
        assert first["status"] == "READY"
        collection = first["vector_collection"]
        client.update_collection(collection, strict_mode_config=models.StrictModeConfig(enabled=True, unindexed_filtering_retrieve=False))
        assert client.get_collection(collection).payload_schema["index_revision"].data_type == models.PayloadSchemaType.INTEGER
        assert workspace.search(scope(), "Member")["count"] == 1

        # Reproduce the old cloud failure, only in this disposable server.
        client.delete_payload_index(collection, "index_revision", wait=True)
        with pytest.raises(UnexpectedResponse) as missing:
            workspace.search(scope(), "Member")
        assert missing.value.status_code == 400

        # A reviewed replacement repairs index metadata before promoting it;
        # old points remain intact and only the current version is returned.
        old = workspace.store.get(scope()["workspaceId"], first["document_id"])
        staged = upload(workspace, "strict-replacement", b"Replacement test registration for Member")
        request = request_for(staged, schema)
        request.update(mode="version", document_id=old["id"], document_revision=old["revision"])
        second = workspace.commit(scope(), staged["id"], request)
        assert second["status"] == "READY" and second["version"] == 2
        assert client.get_collection(collection).payload_schema["index_revision"].data_type == models.PayloadSchemaType.INTEGER
        assert client.count(collection).count == 2
        found = workspace.search(scope(), "Member")
        assert found["count"] == 1 and found["items"][0]["upload_id"] == second["id"]
    finally:
        client.close()
