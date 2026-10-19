"""Caller-scoped chat attachments, reviewed forms and private retrieval."""
from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from erp_pipeline.document_workspace import (
    DocumentWorkspace, LibraryVectors, MAX_FILE_BYTES, WorkspaceError,
    WorkspaceStore, verify_scope,
)
from erp_pipeline.orchestration.representation_crypto import RepresentationCipher

library_router = APIRouter(prefix="/v1/library", tags=["document-workspace"])
_construction_lock = threading.Lock()


def _workspace(request: Request) -> DocumentWorkspace:
    if getattr(request.app.state, "document_workspace", None) is not None:
        return request.app.state.document_workspace
    with _construction_lock:
        if getattr(request.app.state, "document_workspace", None) is not None:
            return request.app.state.document_workspace
        services = request.app.state.orchestration.services
        engine = getattr(services, "runtime_engine", None)
        if engine is None:
            raise WorkspaceError(503, "LIBRARY_DATABASE_UNAVAILABLE", "The durable document workspace database is not configured.")
        vectors = None
        runtime = getattr(services, "runtime_settings", None)
        if runtime is not None and runtime.qdrant.enabled and services.embedding is not None:
            from erp_pipeline.runtime.services import build_qdrant_client
            from erp_pipeline.storage.models import StorageTier
            locations = runtime.storage_locations.as_tier_map()
            location = locations.get(StorageTier.HOT)
            vectors = LibraryVectors(build_qdrant_client(runtime.qdrant), services.embedding,
                os.environ.get("ERP_LIBRARY_COLLECTION", "erp_documents_private"),
                restricted_allowed=getattr(location, "value", str(location)) == "on_premises",
                synthetic_workspaces=frozenset(value.strip() for value in os.environ.get("ERP_LIBRARY_SYNTHETIC_WORKSPACES", "").split(",") if value.strip()))
        result = DocumentWorkspace(WorkspaceStore(engine, RepresentationCipher()), vectors)
        request.app.state.document_workspace = result
        return result


def _scope(request: Request, token: str | None = None):
    key = request.app.state.settings.api_key
    return verify_scope(token or request.headers.get("X-Library-Scope", ""), key or "")


def _run(operation):
    try:
        return operation()
    except WorkspaceError as error:
        return JSONResponse(status_code=error.status, content={"error": {"code": error.code, "message": str(error)}})


@library_router.get("/catalog", operation_id="documentWorkspaceCatalog")
def catalog(request: Request):
    return _run(lambda: _workspace(request).catalog(_scope(request)))


@library_router.post("/migrate", operation_id="migrateDocumentCollection")
def migrate(request: Request, body: dict[str, Any]):
    return _run(lambda: _workspace(request).migrate_collection(_scope(request), body))


@library_router.post("/catalog", operation_id="importDocumentWorkspaceSchema")
def import_schema(request: Request, body: dict[str, Any]):
    return _run(lambda: _workspace(request).import_schema(_scope(request), body))


@library_router.get("/inventory", operation_id="documentWorkspaceInventory")
def inventory(request: Request):
    return _run(lambda: _workspace(request).inventory(_scope(request)))


@library_router.get("/uploads", operation_id="listChatDocumentUploads")
def list_uploads(request: Request, session_id: str):
    return _run(lambda: {"items": _workspace(request).list_uploads(_scope(request), session_id)})


@library_router.post("/uploads", status_code=201, operation_id="stageChatDocument")
def stage(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...), session_id: str = Form(...), idempotency_key: str = Form(...), defer_extraction: bool = Form(False)):
    def operation():
        scope = _scope(request)
        service = _workspace(request)
        service.writable(scope)
        data = file.file.read(MAX_FILE_BYTES + 1)
        result = service.stage(scope, file.filename or "attachment", data, session_id, idempotency_key, defer_extraction)
        if result["status"] == "EXTRACTING":
            background_tasks.add_task(service.complete_extraction, scope, result["id"])
        return result
    return _run(operation)


@library_router.get("/uploads/{upload_id}", operation_id="getChatDocumentUpload")
def get_upload(request: Request, upload_id: str, background_tasks: BackgroundTasks, include_text: bool = False):
    def operation():
        service, scope = _workspace(request), _scope(request)
        result = service.get_upload(scope, upload_id, include_text)
        if result["status"] == "EXTRACTING" and scope["canUpload"] and not scope["readOnly"]:
            background_tasks.add_task(service.complete_extraction, scope, upload_id)
        return result
    return _run(operation)


@library_router.get("/uploads/{upload_id}/source", operation_id="getPrivateModelDocumentSource")
def model_source(request: Request, upload_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _run(lambda: _workspace(request).model_source(_scope(request), upload_id))


@library_router.post("/uploads/{upload_id}/confirm", operation_id="confirmChatDocument")
def confirm(request: Request, upload_id: str, body: dict[str, Any]):
    return _run(lambda: _workspace(request).commit(_scope(request), upload_id, body))


@library_router.post("/uploads/{upload_id}/proposal", operation_id="saveChatDocumentProposal")
def propose(request: Request, upload_id: str, body: dict[str, Any]):
    return _run(lambda: _workspace(request).propose(_scope(request), upload_id, body.get("proposal", {}), body.get("expected_revision")))


@library_router.post("/uploads/{upload_id}/recover", operation_id="recoverInterruptedChatDocument")
def recover(request: Request, upload_id: str, body: dict[str, Any]):
    return _run(lambda: _workspace(request).recover(_scope(request), upload_id, body.get("expected_revision")))


class LibrarySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str = Field(default="", max_length=4000)
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=20)


class LibraryResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str = Field(default="", max_length=4000)
    document_id: str = Field(min_length=1, max_length=200)


class LibraryDiscover(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str = Field(default="", max_length=4000)


@library_router.post("/discover", operation_id="discoverPrivateDocumentCatalog")
def discover(request: Request, body: LibraryDiscover):
    def operation():
        service, scope = _workspace(request), _scope(request, body.access_token)
        return {**service.catalog(scope), "inventory": service.inventory(scope)}
    return _run(operation)


@library_router.post("/search", operation_id="searchPrivateDocuments")
def search(request: Request, body: LibrarySearch):
    return _run(lambda: _workspace(request).search(_scope(request, body.access_token), body.query, body.limit))


@library_router.post("/resolve", operation_id="resolvePrivateDocument")
def resolve(request: Request, body: LibraryResolve):
    return _run(lambda: _workspace(request).resolve(_scope(request, body.access_token), body.document_id))
