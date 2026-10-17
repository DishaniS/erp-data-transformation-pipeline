from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import io
import zipfile

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from erp_pipeline.document_workspace import DocumentWorkspace, WorkspaceError, WorkspaceStore, extract_content, verify_scope
from erp_pipeline.orchestration.representation_crypto import RepresentationCipher, StaticRepresentationKeyProvider

KEY = "isolated-library-scope-fixture-key"


def scope(actor="owner-a", workspace="company-a", **extra):
    return dict(profile="pipeline-library-scope/v1", workspaceId=workspace, actorId=actor, role="HR", canManage=True, canUpload=True, readOnly=False, iat=int(time.time()), exp=int(time.time()) + 120, **extra)


def token(value):
    raw = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(KEY.encode(), raw.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return raw + "." + signature


class Vectors:
    def __init__(self):
        self.items = []
        self.fail = False
        self.collection = "legacy_test"

    def index(self, caller, upload, document, text):
        if self.fail:
            raise RuntimeError("isolated embedding failure")
        assert text
        self.items.append(dict(document_id=document["id"], upload_id=upload["id"], index_revision=upload["revision"], chunk_index=0, score=0.9, vector_collection=document["vector_collection"]))

    def search(self, caller, query, limit, routes):
        return list(reversed(self.items))


@pytest.fixture
def workspace():
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    store = WorkspaceStore(engine, RepresentationCipher(StaticRepresentationKeyProvider(b"x" * 32)))
    result = DocumentWorkspace(store, Vectors())
    yield result
    engine.dispose()


@pytest.fixture
def schema(workspace):
    return workspace.import_schema(scope(), dict(source_system_id="hr", source_entity="applications", primary_key="application_id",
        fields=[dict(name="application_id", type="string", generated=True), dict(name="name", type="string", required=True), dict(name="age", type="integer")],
        document_types=["registration_form", "medical_certificate", "leave_note"], lookup_tool="hr-members", reader_roles=["HR"]))


def upload(workspace, key="request-one", content=b"Registration form\nName: Example Person\nAge: 21"):
    return workspace.stage(scope(), "form.txt", content, "chat_fixture", key)


def request_for(uploaded, schema, **changes):
    return dict(expected_revision=uploaded["revision"], schema_id=schema["id"], schema_revision=schema["revision"], document_type="registration_form", mode="new",
        create_record=True, form_fields={"name": "Example Person", "age": 21}, review_confirmed=True, **changes)


def test_signed_scope_checks_tamper_expiry_and_read_only():
    assert verify_scope(token(scope()), KEY)["actorId"] == "owner-a"
    with pytest.raises(WorkspaceError):
        verify_scope(token(scope()) + "bad", KEY)
    value = scope()
    value["exp"] = int(time.time()) - 1
    with pytest.raises(WorkspaceError):
        verify_scope(token(value), KEY)


def test_staged_upload_is_encrypted_and_never_searchable(workspace):
    uploaded = upload(workspace)
    assert uploaded["status"] == "REVIEW"
    assert "text" not in uploaded and "file_base64" not in uploaded
    assert workspace.inventory(scope())["searchable_documents"] == 0
    with workspace.store.engine.connect() as connection:
        rows = connection.execute(sa.select(workspace.store.table.c.payload)).scalars().all()
    assert len(rows) == 2
    assert all("Example Person" not in row and "Registration form" not in row for row in rows)


def test_upload_idempotency_and_changed_key_conflict(workspace):
    first = upload(workspace)
    assert upload(workspace)["id"] == first["id"]
    with pytest.raises(WorkspaceError) as error:
        upload(workspace, content=b"Different content")
    assert error.value.code == "UPLOAD_KEY_CONFLICT"


def test_owner_and_workspace_boundaries(workspace, schema):
    first = upload(workspace)
    for other in [scope("owner-b"), scope(workspace="company-b")]:
        with pytest.raises(WorkspaceError):
            workspace.get_upload(other, first["id"], True)
        assert workspace.list_uploads(other, "chat_fixture") == []


def test_read_scope_cannot_mutate(workspace):
    caller = scope()
    caller["readOnly"] = True
    with pytest.raises(WorkspaceError) as error:
        workspace.stage(caller, "file.txt", b"content", "chat_fixture", "request-read")
    assert error.value.status == 403


def test_confirm_creates_real_pipeline_record_and_vector_receipt(workspace, schema):
    first = upload(workspace)
    confirmed = workspace.commit(scope(), first["id"], request_for(first, schema))
    assert confirmed["status"] == "READY"
    assert confirmed["pipeline_record_id"].startswith("pipeline_record_")
    record = workspace.store.get(scope()["workspaceId"], confirmed["pipeline_record_id"])
    assert record["fields"]["name"] == "Example Person" and record["erp_created"] is False
    assert workspace.inventory(scope())["searchable_documents"] == 1
    resolved = workspace.resolve(scope(), confirmed["document_id"])
    assert resolved["form_fields"]["age"] == 21


def test_duplicate_confirm_recovers_same_receipt(workspace, schema):
    first = upload(workspace)
    request = request_for(first, schema)
    result = workspace.commit(scope(), first["id"], request)
    assert workspace.commit(scope(), first["id"], request)["id"] == result["id"]
    assert len(workspace.vectors.items) == 1


@pytest.mark.parametrize("fields", [{}, {"name": "A", "unknown": "x"}, {"name": "A", "age": "21"}, {"name": "A", "age": True}])
def test_form_schema_validation(workspace, schema, fields):
    first = upload(workspace)
    request = request_for(first, schema)
    request["form_fields"] = fields
    with pytest.raises(WorkspaceError) as error:
        workspace.commit(scope(), first["id"], request)
    assert error.value.code == "FORM_INVALID"
    assert not workspace.vectors.items


def test_explicit_review_required(workspace, schema):
    first = upload(workspace)
    request = request_for(first, schema)
    request["review_confirmed"] = False
    with pytest.raises(WorkspaceError):
        workspace.commit(scope(), first["id"], request)


def test_schema_drift_blocks_confirmation(workspace, schema):
    first = upload(workspace)
    raw = {key: value for key, value in schema.items() if key not in {"id", "kind", "owner_id", "created_at", "updated_at", "revision"}}
    workspace.import_schema(scope(), {**raw, "expected_revision": schema["revision"], "description": "New revision"})
    with pytest.raises(WorkspaceError) as error:
        workspace.commit(scope(), first["id"], request_for(first, schema))
    assert error.value.code == "SCHEMA_REVISION_CONFLICT"


def test_new_documents_never_replace_another_note(workspace, schema):
    results = []
    for key in ("first-document", "second-document"):
        first = upload(workspace, key)
        results.append(workspace.commit(scope(), first["id"], request_for(first, schema)))
    assert results[0]["document_id"] != results[1]["document_id"]
    assert workspace.inventory(scope())["searchable_documents"] == 2


def test_index_failure_is_not_searchable_and_can_be_explicitly_retried(workspace, schema):
    first = upload(workspace)
    workspace.vectors.fail = True
    failed = workspace.commit(scope(), first["id"], request_for(first, schema))
    assert failed["status"] == "INDEX_FAILED"
    assert workspace.inventory(scope())["searchable_documents"] == 0
    workspace.vectors.fail = False
    succeeded = workspace.commit(scope(), first["id"], request_for(failed, schema))
    assert succeeded["status"] == "READY"


def test_private_medical_documents_cannot_be_shared_by_role(workspace, schema):
    first = upload(workspace)
    request = request_for(first, schema)
    request.update(document_type="medical_certificate", share_with_schema_roles=True)
    done = workspace.commit(scope(), first["id"], request)
    document = workspace.store.get(scope()["workspaceId"], done["document_id"])
    assert document["sensitivity"] == "restricted" and document["reader_roles"] == []
    assert workspace.search(scope("owner-b"), "medical")["items"] == []


@pytest.mark.parametrize("name,content,code", [("file.exe", b"MZ", "FILE_UNSUPPORTED"), ("file.txt", b"", "FILE_EMPTY"), ("file.pdf", b"not a pdf", "EXTRACTION_FAILED"), ("file.json", b"not JSON", "EXTRACTION_FAILED")])
def test_invalid_files_are_refused(name, content, code):
    with pytest.raises(WorkspaceError) as error:
        extract_content(content, "." + name.rsplit(".", 1)[-1])
    assert error.value.code == code


def test_api_requires_signed_scope_even_with_api_key(workspace):
    from fastapi.testclient import TestClient
    from erp_pipeline.api import ApiSettings, create_app
    app = create_app(settings=ApiSettings(api_key=KEY))
    app.state.document_workspace = workspace
    with TestClient(app) as client:
        assert client.get("/v1/library/catalog", headers={"X-API-Key": KEY}).status_code == 401
        headers = {"X-API-Key": KEY, "X-Library-Scope": token(scope())}
        result = client.post("/v1/library/uploads", headers=headers, files={"file": ("form.txt", b"Reviewed form example", "text/plain")}, data={"session_id": "chat_fixture", "idempotency_key": "api-upload-key"})
        assert result.status_code == 201
        assert result.json()["status"] == "REVIEW"
        assert client.get("/v1/library/inventory", headers=headers).json()["searchable_documents"] == 0


def test_reviewed_batch_creates_all_members_atomically(workspace, schema):
    first = upload(workspace)
    request = request_for(first, schema)
    request.update(form_fields={}, form_rows=[{"application_id": "MEMBER-A", "name": "A"}, {"application_id": "MEMBER-B", "name": "B"}])
    done = workspace.commit(scope(), first["id"], request)
    assert done["status"] == "READY" and len(done["pipeline_record_ids"]) == 2
    assert len(workspace.store.list(scope()["workspaceId"], "record")) == 2
    assert len(workspace.resolve(scope(), done["document_id"])["form_rows"]) == 2
    assert "form_rows" not in workspace.inventory(scope())["documents"][0]


@pytest.mark.parametrize("rows,code", [([{"application_id": "A", "name": "One"}, {"application_id": "A", "name": "Two"}], "DUPLICATE_MEMBER_KEY"), ([{"name": "One"}, {"age": 20}], "FORM_INVALID"), ([], "FORM_INVALID"), ([{"name": "A"}] * 51, "FORM_INVALID")])
def test_invalid_batch_never_partially_creates_members(workspace, schema, rows, code):
    first = upload(workspace)
    request = request_for(first, schema)
    request.update(form_fields={}, form_rows=rows)
    with pytest.raises(WorkspaceError) as error:
        workspace.commit(scope(), first["id"], request)
    assert error.value.code == code
    assert not workspace.store.list(scope()["workspaceId"], "record")
    assert not workspace.vectors.items


def test_form_update_is_explicit_version_and_keeps_one_record(workspace, schema):
    first = upload(workspace)
    original_request = request_for(first, schema)
    original_request["form_fields"] = {"application_id": "MEMBER-A", "name": "Original"}
    original = workspace.commit(scope(), first["id"], original_request)
    second = upload(workspace, "second-version", b"Name: Reviewed Update")
    request = request_for(second, schema)
    request["form_fields"] = {"application_id": "MEMBER-A", "name": "Reviewed Update"}
    with pytest.raises(WorkspaceError) as error:
        workspace.commit(scope(), second["id"], request)
    assert error.value.code == "MEMBER_ALREADY_EXISTS"
    document = workspace.store.get(scope()["workspaceId"], original["document_id"])
    request.update(mode="version", document_id=document["id"], document_revision=document["revision"])
    updated = workspace.commit(scope(), second["id"], request)
    assert updated["status"] == "READY" and updated["version"] == 2
    assert updated["pipeline_record_id"] == original["pipeline_record_id"]
    assert len(workspace.store.list(scope()["workspaceId"], "record")) == 1
    assert workspace.store.get(scope()["workspaceId"], updated["pipeline_record_id"])["fields"]["name"] == "Reviewed Update"
    assert workspace.get_upload(scope(), original["id"])["status"] == "SUPERSEDED"
    assert workspace.search(scope(), "Update")["items"][0]["upload_id"] == second["id"]


@pytest.mark.parametrize("original_shared", [False, True])
def test_pending_or_failed_version_never_promotes_fields_or_sharing(workspace, schema, original_shared):
    first = upload(workspace)
    original_request = request_for(first, schema)
    original_request.update(form_fields={"application_id": "MEMBER-A", "name": "Original"}, share_with_schema_roles=original_shared)
    original = workspace.commit(scope(), first["id"], original_request)
    previous = workspace.store.get(scope()["workspaceId"], original["document_id"])
    second = upload(workspace, "pending-version", b"Updated reviewed form")
    request = request_for(second, schema)
    request.update(mode="version", document_id=previous["id"], document_revision=previous["revision"],
        form_fields={"application_id": "MEMBER-A", "name": "Updated"}, share_with_schema_roles=not original_shared)
    observations = {}
    real_index = workspace.vectors.index
    def interrupted(caller, staged, candidate, text):
        observations["owner"] = workspace.resolve(caller, previous["id"])
        try:
            observations["other"] = workspace.resolve(scope("owner-b"), previous["id"])
        except WorkspaceError:
            observations["other"] = None
        raise RuntimeError("Isolated indexing outage")
    workspace.vectors.index = interrupted
    failed = workspace.commit(scope(), second["id"], request)
    assert failed["status"] == "INDEX_FAILED"
    assert observations["owner"]["form_fields"]["name"] == "Original"
    assert (observations["other"] is not None) == original_shared
    assert workspace.resolve(scope(), previous["id"])["form_fields"]["name"] == "Original"
    unchanged = workspace.store.get(scope()["workspaceId"], previous["id"])
    assert unchanged["reader_roles"] == previous["reader_roles"]
    assert workspace.store.get(scope()["workspaceId"], original["pipeline_record_id"])["fields"]["name"] == "Original"
    workspace.vectors.index = real_index
    updated = workspace.commit(scope(), second["id"], {**request, "expected_revision": failed["revision"], "document_revision": unchanged["revision"]})
    assert updated["status"] == "READY" and updated["version"] == 2
    assert workspace.resolve(scope(), previous["id"])["form_fields"]["name"] == "Updated"
    promoted = workspace.store.get(scope()["workspaceId"], previous["id"])
    assert bool(promoted["reader_roles"]) == (not original_shared)


def test_crash_recovery_fences_a_late_attempt(workspace, schema):
    first = upload(workspace)
    original_index = workspace.vectors.index
    def interrupted(caller, upload, document, text):
        current = workspace.store.get(caller["workspaceId"], upload["id"])
        current = workspace.store.put(caller["workspaceId"], {**current, "index_started_at": time.time() - 400}, current["revision"])
        workspace.recover(caller, current["id"], current["revision"])
        original_index(caller, upload, document, text)
    workspace.vectors.index = interrupted
    result = workspace.commit(scope(), first["id"], request_for(first, schema))
    assert result["status"] == "INTERRUPTED"
    assert not workspace.search(scope(), "Registration")["items"]
    workspace.vectors.index = original_index
    result = workspace.commit(scope(), first["id"], request_for(result, schema))
    assert result["status"] == "READY"
    assert len(workspace.search(scope(), "Registration")["items"]) == 1


def test_text_pdf_and_excel_shared_strings_are_read(workspace):
    import pymupdf
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((50, 50), "Registration Example Member")
    text, warnings = extract_content(pdf.tobytes(), ".pdf")
    assert "Example Member" in text
    pdf.close()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>name</t></si><si><t>Example Member</t></si></sst>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row><row r="2"><c r="A2" t="s"><v>1</v></c></row></sheetData></worksheet>')
    text, warnings = extract_content(stream.getvalue(), ".xlsx")
    assert "name\r\nExample Member" in text and warnings


def test_real_qdrant_local_enforces_payload_scope_without_document_text(workspace, schema):
    from qdrant_client import QdrantClient
    from erp_pipeline.document_workspace import LibraryVectors
    from types import SimpleNamespace
    class Embedder:
        def embed_one(self, representation):
            return SimpleNamespace(vector=[0.5, 0.5, 0.5])
    client = QdrantClient(":memory:")
    workspace.vectors = LibraryVectors(client, Embedder(), "private_test")
    first = upload(workspace)
    done = workspace.commit(scope(), first["id"], request_for(first, schema))
    assert done["status"] == "READY"
    assert workspace.search(scope(), "Member")["count"] == 1
    assert workspace.search(scope("different-owner"), "Member")["count"] == 0
    assert workspace.search(scope(workspace="different-workspace"), "Member")["count"] == 0
    points, _ = client.scroll(done["vector_collection"], with_payload=True)
    assert "Example Person" not in json.dumps([point.payload for point in points])
    client.close()


def test_partial_first_sheet_still_warns_when_the_final_sheet_is_short():
    stream = io.BytesIO()
    def sheet(count):
        rows = "".join(f'<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>Member {n}</t></is></c></row>' for n in range(1, count + 1))
        return f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rows}</sheetData></worksheet>'
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet(502))
        archive.writestr("xl/worksheets/sheet2.xml", sheet(1))
    text, warnings = extract_content(stream.getvalue(), ".xlsx")
    assert "Member 501" in text and "Member 502" not in text
    assert any("partial" in warning for warning in warnings)
