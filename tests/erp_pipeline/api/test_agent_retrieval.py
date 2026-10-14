"""Agent retrieval is scoped, bounded, and resolves a batch in one read."""
from unittest.mock import patch

import pytest

from tests.erp_pipeline.api.test_automatic_document_indexing import (
    Harness, _pdf, upload,
)


@pytest.fixture
def corpus(tmp_path):
    from fastapi.testclient import TestClient
    built = Harness(tmp_path)
    with TestClient(built.app) as client:
        for key, name in [("EMP-001", "Lakshan Test"), ("EMP-002", "Different Person")]:
            response = upload(client, _pdf(["TEST CERTIFICATE", name, "Reference: QA-7382"]),
                source_system_id="qa_hr", source_entity="employees",
                business_key_name="employee_id", business_key_value=key,
                document_type="test_certificate")
            assert response.status_code == 201
        yield client, built


def test_exact_retrieval_bypasses_embedding_and_returns_only_selected_person(corpus):
    client, built = corpus
    with patch.object(built.services.embedding.model, "encode", side_effect=AssertionError("no ANN")), \
         patch.object(built.representations, "get_many", wraps=built.representations.get_many) as batch:
        response = client.get("/v1/retrieve", params={"source_system_id": "qa_hr",
            "source_entity": "employees", "record_key": "EMP-001"})
    assert response.status_code == 200
    assert batch.call_count == 1
    data = response.json()
    assert data["retrieval_mode"] == "exact"
    assert data["hits"] and all(hit["record_key"] == "EMP-001" for hit in data["hits"])
    assert "Lakshan Test" in data["hits"][0]["text"]
    assert "Different Person" not in str(data)


def test_text_budget_and_missing_scope_are_enforced(corpus):
    client, _ = corpus
    assert client.get("/v1/retrieve", params={"q": "certificate"}).status_code == 422
    assert client.get("/v1/retrieve", params={"source_system_id": "qa_hr",
        "source_entity": "employees"}).status_code == 422
    response = client.get("/v1/retrieve", params={"q": "certificate",
        "source_system_id": "qa_hr", "source_entity": "employees",
        "record_key": "EMP-001", "max_text_chars": 20, "min_score": -1})
    assert response.status_code == 200
    data = response.json()
    assert data["text_chars_returned"] <= 20
    assert data["hits"][0]["text_truncated"] is True
    assert all("vector" not in hit for hit in data["hits"])


def test_no_results_is_explicit_and_wrong_scope_returns_nothing(corpus):
    client, _ = corpus
    response = client.get("/v1/retrieve", params={"source_system_id": "other_hr",
        "source_entity": "employees", "record_key": "EMP-001"})
    assert response.status_code == 200
    assert response.json()["hits"] == []
