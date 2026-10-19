"""Private, reviewed document/form workspace. Never writes to an original ERP.

The platform signs caller scope; files, extracted content and form fields stay
encrypted. Qdrant holds opaque references, not document text. Server-derived
workspace/entity collections separate standard and restricted documents from
legacy unscoped search. SQL remains the current-version/access authority.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import math
import re
import threading
import time
import uuid
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from erp_pipeline.orchestration.representation_crypto import RepresentationCipher

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TEXT = 120_000
SUPPORTED_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".txt", ".csv", ".tsv", ".json", ".docx", ".xlsx")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SENSITIVITIES = ("public", "internal", "confidential", "restricted")
RESTRICTED_TYPES = frozenset({"medical_certificate", "medical_record", "identity_document"})


def collection_route(scope: dict, schema: dict, restricted: bool) -> dict:
    """Stable server-owned destination; names are not caller authority or ERP paths."""
    tier = "restricted" if restricted else "standard"
    identity = [scope["workspaceId"], schema["id"], schema["source_system_id"], schema["source_entity"], tier]
    digest = hashlib.sha256(canonical(identity).encode()).hexdigest()[:24]
    label = re.sub(r"[^a-z0-9]+", "_", f"{schema['source_system_id']}_{schema['source_entity']}".lower()).strip("_")[:40]
    return {"collection": f"erp_lib_v1_{label}_{tier}_{digest}", "schema_id": schema["id"], "tier": tier,
        "source_system_id": schema["source_system_id"], "source_entity": schema["source_entity"], "routing_version": 1}


def type_is_restricted(schema: dict, document_type: str) -> bool:
    return document_type in RESTRICTED_TYPES or schema.get("minimum_sensitivity") == "restricted"


class WorkspaceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def refuse(status: int, code: str, message: str):
    raise WorkspaceError(status, code, message)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _unbase64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_scope(token: str, key: str) -> dict[str, Any]:
    try:
        raw, signature = token.split(".")
        expected = hmac.new(key.encode(), raw.encode(), hashlib.sha256).digest()
        if not key or not hmac.compare_digest(_unbase64(signature), expected):
            raise ValueError()
        scope = json.loads(_unbase64(raw))
        now = time.time()
        if scope.get("profile") != "pipeline-library-scope/v1" or not isinstance(scope.get("exp"), int) or not isinstance(scope.get("iat"), int) or scope["exp"] <= now or scope["iat"] > now + 5 or scope["exp"] - scope["iat"] > 180:
            raise ValueError()
        for name in ("workspaceId", "actorId", "role"):
            if not isinstance(scope.get(name), str) or not scope[name] or len(scope[name]) > 200:
                raise ValueError()
        for name in ("canManage", "canUpload", "readOnly"):
            if not isinstance(scope.get(name), bool):
                raise ValueError()
        return scope
    except Exception:
        refuse(401, "LIBRARY_SCOPE_REQUIRED", "A current server-issued document scope is required.")


class WorkspaceStore:
    """Optimistic, encrypted entries; PostgreSQL in production, SQLite in tests."""
    def __init__(self, engine: Any, cipher: RepresentationCipher):
        self.engine, self.cipher = engine, cipher
        schema = "erp_runtime" if engine.dialect.name == "postgresql" else None
        metadata = sa.MetaData(schema=schema)
        self.table = sa.Table("document_workspace_entries", metadata,
            sa.Column("workspace", sa.String(200), primary_key=True),
            sa.Column("entry_id", sa.String(200), primary_key=True),
            sa.Column("kind", sa.String(30), nullable=False),
            sa.Column("owner", sa.String(200), nullable=False),
            sa.Column("revision", sa.Integer, nullable=False),
            sa.Column("payload", sa.Text, nullable=False))
        if not cipher.available:
            refuse(503, "LIBRARY_ENCRYPTION_UNAVAILABLE", "Document encryption must be configured before upload.")
        if schema:
            with engine.begin() as connection:
                connection.execute(sa.text("CREATE SCHEMA IF NOT EXISTS erp_runtime"))
        metadata.create_all(engine)

    def get(self, workspace: str, identifier: str, connection: Any = None) -> dict | None:
        if connection is not None:
            row = connection.execute(sa.select(self.table).where(self.table.c.workspace == workspace, self.table.c.entry_id == identifier)).mappings().first()
            return None if row is None else json.loads(self.cipher.decrypt(row["payload"]))
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(self.table).where(self.table.c.workspace == workspace, self.table.c.entry_id == identifier)).mappings().first()
        return None if row is None else json.loads(self.cipher.decrypt(row["payload"]))

    def list(self, workspace: str, kind: str, limit: int = 1000, owner: str | None = None) -> list[dict]:
        with self.engine.connect() as connection:
            query = sa.select(self.table).where(self.table.c.workspace == workspace, self.table.c.kind == kind)
            if owner is not None:
                query = query.where(self.table.c.owner == owner)
            rows = connection.execute(query.limit(limit)).mappings().all()
        return [json.loads(self.cipher.decrypt(row["payload"])) for row in rows]

    def put(self, workspace: str, entry: dict, expected: int | None = None, connection: Any = None) -> dict:
        entry = {**entry, "revision": 1 if expected is None else expected + 1, "updated_at": timestamp()}
        values = dict(workspace=workspace, entry_id=entry["id"], kind=entry["kind"], owner=entry["owner_id"], revision=entry["revision"], payload=self.cipher.encrypt(canonical(entry)))
        def write(conn):
            if expected is None:
                conn.execute(self.table.insert().values(**values))
            else:
                result = conn.execute(self.table.update().where(self.table.c.workspace == workspace, self.table.c.entry_id == entry["id"], self.table.c.revision == expected).values(**values))
                if result.rowcount != 1:
                    refuse(409, "LIBRARY_REVISION_CONFLICT", "The document or schema changed. Reload before confirming.")
        try:
            if connection is not None:
                write(connection)
            else:
                with self.engine.begin() as conn:
                    write(conn)
        except IntegrityError:
            refuse(409, "LIBRARY_REVISION_CONFLICT", "This request already exists. Reload to recover its receipt.")
        return entry


def validate_schema(raw: dict) -> dict:
    allowed = {"source_system_id", "source_entity", "description", "primary_key", "fields", "document_types", "lookup_tool", "reader_roles", "minimum_sensitivity", "expected_revision"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        refuse(422, "SCHEMA_INVALID", "Use the documented source/entity schema format; unknown fields are not accepted.")
    for key in ("source_system_id", "source_entity"):
        if not isinstance(raw.get(key), str) or not IDENTIFIER.fullmatch(raw[key]):
            refuse(422, "SCHEMA_INVALID", f"A valid {key} is required.")
    fields = raw.get("fields")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 100:
        refuse(422, "SCHEMA_INVALID", "A schema needs 1–100 explicitly named fields.")
    names = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) - {"name", "type", "required", "description", "generated"} or not FIELD_NAME.fullmatch(str(field.get("name", ""))) or field["name"] in names or field.get("type") not in {"string", "integer", "number", "boolean", "date", "email"} or not isinstance(field.get("required", False), bool) or not isinstance(field.get("generated", False), bool):
            refuse(422, "SCHEMA_INVALID", "Field names must be unique, with supported types and a boolean required flag.")
        names.add(field["name"])
        if len(str(field.get("description", ""))) > 500:
            refuse(422, "SCHEMA_INVALID", "Field descriptions must be at most 500 characters.")
    if raw.get("primary_key") not in names:
        refuse(422, "SCHEMA_INVALID", "The primary key must name a declared field.")
    types = raw.get("document_types", ["other"])
    if not isinstance(types, list) or not 1 <= len(types) <= 30 or any(not isinstance(item, str) or not IDENTIFIER.fullmatch(item) for item in types):
        refuse(422, "SCHEMA_INVALID", "Declare 1–30 document types.")
    roles = raw.get("reader_roles", [])
    if not isinstance(roles, list) or len(roles) > 30 or any(not isinstance(item, str) or not 1 <= len(item) <= 100 for item in roles):
        refuse(422, "SCHEMA_INVALID", "Reader roles must be a bounded list.")
    sensitivity = raw.get("minimum_sensitivity", "confidential")
    if sensitivity not in SENSITIVITIES or len(str(raw.get("description", ""))) > 1000 or len(str(raw.get("lookup_tool", ""))) > 150:
        refuse(422, "SCHEMA_INVALID", "Invalid schema description, lookup tool or sensitivity.")
    return {**raw, "document_types": list(dict.fromkeys(types)), "reader_roles": list(dict.fromkeys(roles)), "minimum_sensitivity": sensitivity}


def validate_form(fields: dict, schema: dict) -> dict:
    definitions = {field["name"]: field for field in schema["fields"]}
    if not isinstance(fields, dict) or set(fields) - definitions.keys() or len(canonical(fields)) > 30_000:
        refuse(422, "FORM_INVALID", "Form fields must match the selected schema and size limit.")
    for name, definition in definitions.items():
        value = fields.get(name)
        if value is None or value == "":
            if definition.get("required") and not definition.get("generated"):
                refuse(422, "FORM_INVALID", f"Required field {name} is missing.")
            continue
        kind = definition["type"]
        valid = True
        if kind in {"string", "email", "date"}:
            valid = isinstance(value, str) and len(value) <= 4000
        elif kind == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "number":
            valid = isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
        elif kind == "boolean":
            valid = isinstance(value, bool)
        if valid and kind == "email":
            valid = bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))
        if valid and kind == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                valid = False
        if not valid:
            refuse(422, "FORM_INVALID", f"Field {name} must have type {kind}.")
    return fields


def extract_content(payload: bytes, suffix: str) -> tuple[str, list[str]]:
    """Bounded parsers only. No macros, formulas, external links or code execute."""
    if not payload:
        refuse(422, "FILE_EMPTY", "Choose a file containing data.")
    if suffix not in SUPPORTED_SUFFIXES:
        refuse(415, "FILE_UNSUPPORTED", "This file type is not supported. Choose PDF, images, text, CSV, JSON, DOCX or XLSX.")
    if len(payload) > MAX_FILE_BYTES:
        refuse(413, "FILE_TOO_LARGE", "The attachment limit is 16 MiB.")
    warnings: list[str] = []
    try:
        if suffix in {".txt", ".csv", ".tsv", ".json"}:
            text = payload.decode("utf-8-sig")
            if "\x00" in text:
                raise ValueError()
            if suffix == ".json":
                json.loads(text)
        elif suffix in {".docx", ".xlsx"}:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                entries = archive.infolist()
                if len(entries) > 1000 or sum(item.file_size for item in entries) > 32 * 1024 * 1024 or any("vbaproject" in item.filename.lower() or item.flag_bits & 1 for item in entries):
                    raise ValueError()
                paths = ["word/document.xml"] if suffix == ".docx" else [item.filename for item in entries if re.fullmatch(r"xl/(sharedStrings|worksheets/sheet[0-9]+)\.xml", item.filename)]
                if not paths:
                    raise ValueError()
                pieces = []
                if suffix == ".xlsx":
                    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                    shared = []
                    rows_limited = False
                    if "xl/sharedStrings.xml" in paths:
                        strings = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                        shared = ["".join(node.itertext()) for node in strings.findall("s:si", ns)]
                    for path in [path for path in paths if "worksheets/" in path][:10]:
                        root = ElementTree.fromstring(archive.read(path))
                        stream = io.StringIO(); writer = csv.writer(stream)
                        source_rows = root.findall(".//s:row", ns)
                        rows_limited = rows_limited or len(source_rows) > 501
                        for row in source_rows[:501]:
                            values = {}
                            for cell in row.findall("s:c", ns):
                                letters = re.sub(r"[0-9]", "", cell.get("r", ""))
                                column = 0
                                for letter in letters:
                                    column = column * 26 + ord(letter) - 64
                                if not 1 <= column <= 200:
                                    raise ValueError()
                                value = cell.findtext("s:v", default="", namespaces=ns)
                                if cell.get("t") == "s":
                                    value = shared[int(value)]
                                elif cell.get("t") == "inlineStr":
                                    value = "".join(cell.find("s:is", ns).itertext())
                                values[column] = value
                            writer.writerow([values.get(index, "") for index in range(1, max(values, default=0) + 1)])
                        pieces.append(path + "\n" + stream.getvalue())
                    if rows_limited or len([path for path in paths if "worksheets/" in path]) > 10:
                        warnings.append("Spreadsheet row/sheet limit reached; extracted content is partial.")
                else:
                    for path in paths[:50]:
                        root = ElementTree.fromstring(archive.read(path))
                        pieces.extend(node.text or "" for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "t")
                text = "\n".join(pieces)
                if suffix == ".xlsx":
                    warnings.append("Spreadsheet extraction uses stored text/cached cell values; formulas are not executed.")
        else:
            from erp_pipeline.ingestion.binary_assets import extract_binary_asset
            result = extract_binary_asset(payload, "chat_attachment")
            document = getattr(result, "document", None)
            if document is None:
                raise ValueError()
            text = "\n\n".join(getattr(page, "text", "") or "" for page in getattr(document, "pages", ()))
            warnings.extend(str(item)[:200] for item in getattr(result, "warnings", ())[:5])
    except Exception:
        refuse(422, "EXTRACTION_FAILED", "The file could not be read safely. It may be damaged, encrypted, or require unavailable OCR support.")
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]
        warnings.append("Extraction reached the text limit. Review the source before confirming; this is partial coverage.")
    return text, warnings


class LibraryVectors:
    """Opaque references only; access and current versions are checked in SQL."""
    def __init__(self, client: Any, embedding: Any, collection: str, restricted_allowed: bool = True, synthetic_workspaces: frozenset[str] = frozenset()):
        self.client, self.embedding, self.collection = client, embedding, collection
        self.restricted_allowed = restricted_allowed
        self.synthetic_workspaces = synthetic_workspaces

    def requires_synthetic_confirmation(self, scope: dict) -> bool:
        return not self.restricted_allowed and scope["workspaceId"] in self.synthetic_workspaces

    def _embed(self, text: str, identifier: str) -> list[float]:
        from erp_pipeline.sync.propagation import AIRepresentation
        result = self.embedding.embed_one(AIRepresentation(representation_id=identifier, entity_type="document", text_for_ai=text))
        if not result.vector or not all(math.isfinite(value) for value in result.vector):
            refuse(503, "INDEX_FAILED", "No valid embedding was produced.")
        return list(result.vector)

    def index(self, scope: dict, upload: dict, document: dict, text: str):
        from qdrant_client import models as models
        if document["sensitivity"] == "restricted" and not self.restricted_allowed and not (
            self.requires_synthetic_confirmation(scope) and document.get("synthetic_source_confirmed") is True
        ):
            refuse(409, "STORAGE_POLICY_DENIED", "Restricted documents require an approved on-premises vector location.")
        chunks = [text[start:start + 1000] for start in range(0, len(text), 850)]
        if not chunks:
            refuse(422, "NO_EXTRACTED_TEXT", "No text is available to index. Keep this attachment pending for review.")
        vectors = [self._embed(chunk, f"library:{upload['id']}:{index}") for index, chunk in enumerate(chunks)]
        collection = document["vector_collection"]
        if collection != collection_route(scope, {**document, "id": document["schema_id"]}, document["sensitivity"] == "restricted")["collection"]:
            refuse(409, "COLLECTION_ROUTE_INVALID", "The document destination does not match its server-owned schema route.")
        if not self.client.collection_exists(collection):
            try:
                self.client.create_collection(collection, vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE))
            except Exception:
                # Another worker may create the same deterministic route concurrently.
                if not self.client.collection_exists(collection):
                    raise
            for field in ("workspace", "owner", "reader_roles", "upload_id", "document_id", "schema_id", "document_type", "sensitivity"):
                self.client.create_payload_index(collection, field, models.PayloadSchemaType.KEYWORD)
        info = self.client.get_collection(collection)
        if info.config.params.vectors.size != len(vectors[0]):
            refuse(409, "EMBEDDING_DIMENSION_CHANGED", "The vector collection needs a reviewed embedding migration.")
        # Cloud strict mode requires an index for every filtered payload field.
        # index_revision is numeric, unlike the keyword identity fields above.
        # Also repair collections created before this index was introduced;
        # never drop an existing incompatible index or promote unqueryable data.
        revision_index = (info.payload_schema or {}).get("index_revision")
        if revision_index is None:
            self.client.create_payload_index(collection, "index_revision", models.PayloadSchemaType.INTEGER, wait=True)
        elif revision_index.data_type != models.PayloadSchemaType.INTEGER:
            refuse(409, "COLLECTION_INDEX_SCHEMA_CHANGED", "The revision filter requires a reviewed integer payload index.")
        self.client.upsert(collection, points=[models.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope['workspaceId']}:{upload['id']}:{upload['revision']}:{index}")), vector=vector,
            payload={"workspace": scope["workspaceId"], "owner": document["owner_id"], "reader_roles": document.get("reader_roles", []), "document_id": document["id"], "upload_id": upload["id"], "index_revision": upload["revision"], "chunk_index": index,
                "schema_id": document["schema_id"], "document_type": document["document_type"], "sensitivity": document["sensitivity"]}) for index, vector in enumerate(vectors)], wait=True)

    def search(self, scope: dict, query: str, limit: int, routes: dict[str, list[dict]]) -> list[dict]:
        from qdrant_client import models as models
        if not routes:
            return []
        vector = self._embed(query, "library:query")
        hits = []
        for collection, documents in routes.items():
            if not self.client.collection_exists(collection):
                refuse(503, "COLLECTION_UNAVAILABLE", "A current document collection is unavailable. Retrieval is incomplete; retry after checking the index.")
            # Filter current revisions before top-k, not just after: old versions and
            # unauthorized chunks must not crowd current permitted documents out.
            current = models.Filter(should=[models.Filter(must=[
                models.FieldCondition(key="document_id", match=models.MatchValue(value=doc["id"])),
                models.FieldCondition(key="upload_id", match=models.MatchValue(value=doc["current_upload_id"])),
                models.FieldCondition(key="index_revision", match=models.MatchValue(value=doc["indexed_revision"]))]) for doc in documents])
            query_filter = models.Filter(must=[models.FieldCondition(key="workspace", match=models.MatchValue(value=scope["workspaceId"])), current], should=[
                models.FieldCondition(key="owner", match=models.MatchValue(value=scope["actorId"])),
                models.FieldCondition(key="reader_roles", match=models.MatchValue(value=scope["role"]))])
            result = self.client.query_points(collection, query=vector, query_filter=query_filter, limit=min(limit * 5, 100))
            hits.extend({"upload_id": point.payload["upload_id"], "document_id": point.payload["document_id"], "index_revision": point.payload.get("index_revision"), "chunk_index": point.payload["chunk_index"], "score": point.score, "vector_collection": collection} for point in result.points)
        return sorted(hits, key=lambda item: item["score"], reverse=True)


class DocumentWorkspace:
    def __init__(self, store: WorkspaceStore, vectors: LibraryVectors | None = None):
        self.store, self.vectors = store, vectors
        self.lock = threading.RLock()
        self.extraction_slots = threading.BoundedSemaphore(2)

    def writable(self, scope: dict, manage: bool = False):
        if scope["readOnly"] or not scope["canManage" if manage else "canUpload"]:
            refuse(403, "LIBRARY_PERMISSION_DENIED", "Your current role cannot perform this document operation.")

    def owned(self, scope: dict, identifier: str, kind: str | None = None) -> dict:
        entry = self.store.get(scope["workspaceId"], identifier)
        if entry is None or entry["owner_id"] != scope["actorId"] or kind and entry["kind"] != kind:
            refuse(404, "LIBRARY_NOT_FOUND", "The requested document item was not found.")
        return entry

    def schemas(self, scope: dict) -> list[dict]:
        return self.store.list(scope["workspaceId"], "schema")

    def catalog(self, scope: dict) -> dict:
        schemas = self.schemas(scope)
        all_documents = self.store.list(scope["workspaceId"], "document")
        documents = [doc for doc in all_documents if self.can_read(scope, doc) and doc.get("current_upload_id")]
        collections, entities = [], []
        for schema in schemas:
            routes = []
            for restricted in (False, True):
                types = [kind for kind in schema["document_types"] if type_is_restricted(schema, kind) == restricted]
                route = collection_route(scope, schema, restricted)
                current = [doc for doc in documents if doc.get("vector_collection") == route["collection"]]
                route.update(document_types=types, accepts_uploads=bool(types), description=("Restricted, owner-only documents" if restricted else "Reviewed entity documents; sharing requires explicit schema-role consent"),
                    searchable_documents=len(current), document_type_counts={kind: sum(doc["document_type"] == kind for doc in current) for kind in sorted({doc["document_type"] for doc in current})},
                    last_document_update=max((doc["updated_at"] for doc in current), default=None), provisioning="created on first successful index; zero permitted documents does not prove physical absence")
                route["requires_synthetic_confirmation"] = bool(restricted and self.vectors and getattr(self.vectors, "requires_synthetic_confirmation", lambda _: False)(scope))
                routes.append(route)
                collections.append(route)
            entities.append({**schema, "collection_routes": routes})
        return {"entities": entities, "schema_only": True, "collections": collections, "as_of": timestamp(), "counts_complete": len(all_documents) < 1000 and len(schemas) < 1000,
            "scope": "schema definitions and permitted current documents only", "sample_limit": 1000,
            "legacy_current_documents": sum(not doc.get("vector_collection") for doc in documents),
            "routing_policy": "workspace + source system + entity schema; restricted documents separate; server derives destination at confirmation",
            "summary_contains_document_content": False}

    def import_schema(self, scope: dict, raw: dict) -> dict:
        self.writable(scope, manage=True)
        schema = validate_schema(raw)
        identifier = "schema_" + hashlib.sha256(f"{scope['workspaceId']}:{schema['source_system_id']}:{schema['source_entity']}".encode()).hexdigest()[:32]
        current = self.store.get(scope["workspaceId"], identifier)
        expected = schema.pop("expected_revision", None)
        if current and expected != current["revision"]:
            refuse(409, "SCHEMA_REVISION_CONFLICT", "Review the current schema revision before replacing it.")
        return self.store.put(scope["workspaceId"], {**schema, "id": identifier, "kind": "schema", "owner_id": scope["actorId"], "created_at": current["created_at"] if current else timestamp()}, expected=current["revision"] if current else None)

    def stage(self, scope: dict, filename: str, data: bytes, session_id: str, key: str, defer_extraction: bool = False) -> dict:
        self.writable(scope)
        if not 8 <= len(key) <= 128 or not IDENTIFIER.fullmatch(session_id):
            refuse(422, "UPLOAD_REQUEST_INVALID", "A conversation and bounded idempotency key are required.")
        suffix = Path(filename.replace("\\", "/")).suffix.lower()
        if not data:
            refuse(422, "FILE_EMPTY", "Choose a file containing data.")
        if len(data) > MAX_FILE_BYTES:
            refuse(413, "FILE_TOO_LARGE", "The attachment limit is 16 MiB.")
        if suffix not in SUPPORTED_SUFFIXES:
            refuse(415, "FILE_UNSUPPORTED", "This file type is not supported.")
        digest = hashlib.sha256(data).hexdigest()
        identifier = "upload_" + hashlib.sha256(f"{scope['workspaceId']}:{scope['actorId']}:{key}".encode()).hexdigest()[:40]
        current = self.store.get(scope["workspaceId"], identifier)
        if current:
            self.owned(scope, identifier, "upload")
            if current["content_hash"] != digest or current["session_id"] != session_id:
                refuse(409, "UPLOAD_KEY_CONFLICT", "The upload key was already used for another file or conversation.")
            return self.public_upload(current)
        text, warnings = ("", []) if defer_extraction else extract_content(data, suffix)
        safe_name = re.sub(r"[\x00-\x1f]", "_", filename.replace("\\", "/").rsplit("/", 1)[-1])[:120]
        entry = dict(id=identifier, kind="upload", owner_id=scope["actorId"], session_id=session_id, filename=safe_name, content_hash=digest, size_bytes=len(data), text=text,
            status="EXTRACTING" if defer_extraction else "REVIEW" if text.strip() else "NEEDS_TEXT", warnings=warnings, created_at=timestamp(),
            **({"extraction_state": "QUEUED"} if defer_extraction else {}))
        try:
            with self.store.engine.begin() as connection:
                self.store.put(scope["workspaceId"], {"id": "blob_" + identifier, "kind": "blob", "owner_id": scope["actorId"], "file_base64": base64.b64encode(data).decode()}, connection=connection)
                saved = self.store.put(scope["workspaceId"], entry, connection=connection)
        except WorkspaceError as error:
            if error.status != 409:
                raise
            return self.stage(scope, filename, data, session_id, key, defer_extraction)
        return self.public_upload(saved)

    def complete_extraction(self, scope: dict, identifier: str) -> None:
        # Saturated work stays durably QUEUED and can resume on status refresh.
        # Do not start unbounded OCR threads on the small shared deployment.
        if not self.extraction_slots.acquire(blocking=False):
            return
        try:
            self._complete_extraction(scope, identifier)
        finally:
            self.extraction_slots.release()

    def _complete_extraction(self, scope: dict, identifier: str) -> None:
        """Resumable, revision-fenced OCR. Raw bytes are durable before enqueue.

        A process restart cannot turn an unfinished extraction into reviewed data.
        GET may resume a queued/stale attempt; a late worker cannot overwrite a
        newer lease, model proposal, or confirmed attachment.
        """
        self.writable(scope)
        with self.lock:
            upload = self.owned(scope, identifier, "upload")
            if upload["status"] != "EXTRACTING":
                return
            if upload.get("extraction_state") == "RUNNING" and time.time() - upload.get("extraction_started_at", time.time()) < 300:
                return
            run_id = uuid.uuid4().hex
            try:
                upload = self.store.put(scope["workspaceId"], {**upload, "extraction_state": "RUNNING",
                    "extraction_run_id": run_id, "extraction_started_at": time.time()}, upload["revision"])
            except WorkspaceError as error:
                if error.status == 409:
                    return
                raise
            blob = self.owned(scope, "blob_" + identifier, "blob")
        try:
            text, warnings = extract_content(base64.b64decode(blob["file_base64"], validate=True), Path(upload["filename"]).suffix.lower())
            updates = {"text": text, "warnings": warnings, "status": "REVIEW" if text.strip() else "NEEDS_TEXT", "extraction_state": "SUCCEEDED", "error_code": None}
        except Exception:
            # Do not expose parser errors, source bytes, or filesystem details.
            updates = {"status": "EXTRACTION_FAILED", "extraction_state": "FAILED", "error_code": "EXTRACTION_FAILED",
                "warnings": ["Document extraction could not finish. Review the source or explicitly retry extraction."]}
        for _ in range(3):
            with self.lock:
                current = self.owned(scope, identifier, "upload")
                if current["status"] != "EXTRACTING" or current.get("extraction_run_id") != run_id:
                    return
                try:
                    self.store.put(scope["workspaceId"], {**current, **updates}, current["revision"])
                    return
                except WorkspaceError as error:
                    if error.status != 409:
                        raise

    def model_source(self, scope: dict, identifier: str) -> dict:
        """Owner-scoped source for the trusted model adapter, never discovery."""
        self.writable(scope)
        upload = self.owned(scope, identifier, "upload")
        if upload["size_bytes"] > 4 * 1024 * 1024:
            refuse(413, "MODEL_SOURCE_TOO_LARGE", "Use extracted-text review for files larger than 4 MiB.")
        blob = self.owned(scope, "blob_" + identifier, "blob")
        data = base64.b64decode(blob["file_base64"], validate=True)
        content_type = "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if data.startswith(b"\xff\xd8\xff") else "application/pdf" if data.startswith(b"%PDF-") else None
        if content_type is None:
            refuse(415, "MODEL_SOURCE_UNSUPPORTED", "This file requires extracted-text review.")
        return {"filename": upload["filename"], "content_type": content_type, "content_hash": upload["content_hash"], "data_base64": blob["file_base64"]}

    def public_upload(self, upload: dict, include_text: bool = False) -> dict:
        result = {key: value for key, value in upload.items() if key not in {"file_base64", "text", "commit_fingerprint", "kind", "extraction_run_id", "extraction_started_at"}}
        result["extracted_characters"] = len(upload.get("text", ""))
        result["model_source_available"] = upload.get("size_bytes", MAX_FILE_BYTES) <= 4 * 1024 * 1024 and Path(upload.get("filename", "")).suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf"}
        if include_text:
            result["extracted_text"] = upload.get("text", "")
        return result

    def get_upload(self, scope: dict, identifier: str, include_text: bool = False) -> dict:
        return self.public_upload(self.owned(scope, identifier, "upload"), include_text)

    def propose(self, scope: dict, identifier: str, proposal: dict, expected: int) -> dict:
        self.writable(scope)
        upload = self.owned(scope, identifier, "upload")
        if upload["revision"] != expected or upload["status"] not in {"REVIEW", "NEEDS_TEXT", "INDEX_FAILED", "INTERRUPTED", "EXTRACTING", "EXTRACTION_FAILED"}:
            refuse(409, "LIBRARY_REVISION_CONFLICT", "The attachment changed. Reload before preparing another proposal.")
        if len(canonical(proposal)) > 100_000:
            refuse(422, "PROPOSAL_TOO_LARGE", "The review proposal exceeds its size limit.")
        return self.public_upload(self.store.put(scope["workspaceId"], {**upload, "proposal": proposal}, expected))

    def list_uploads(self, scope: dict, session_id: str) -> list[dict]:
        return [self.public_upload(item) for item in self.store.list(scope["workspaceId"], "upload", owner=scope["actorId"]) if item["session_id"] == session_id]

    def inventory(self, scope: dict) -> dict:
        uploads = self.store.list(scope["workspaceId"], "upload", owner=scope["actorId"])
        all_documents = self.store.list(scope["workspaceId"], "document")
        documents = [item for item in all_documents if self.can_read(scope, item)]
        return {"as_of": timestamp(), "scope": "owned uploads and permitted documents", "counts_complete": len(uploads) < 1000 and len(all_documents) < 1000, "sample_limit": 1000, "uploaded": len(uploads), "pending_review": sum(item["status"] in {"REVIEW", "NEEDS_TEXT", "EXTRACTING", "EXTRACTION_FAILED"} for item in uploads),
            "indexing": sum(item["status"] == "INDEXING" for item in uploads), "index_failed": sum(item["status"] in {"INDEX_FAILED", "INTERRUPTED"} for item in uploads),
            "searchable_documents": sum(bool(item.get("current_upload_id")) for item in documents), "documents": [{key: value for key, value in item.items() if key not in {"form_fields", "form_rows"}} for item in documents],
            "supported_extensions": SUPPORTED_SUFFIXES, "max_upload_bytes": MAX_FILE_BYTES, "vector_index_configured": self.vectors is not None,
            "legacy_current_documents": sum(bool(item.get("current_upload_id")) and not item.get("vector_collection") for item in documents)}

    def can_read(self, scope: dict, document: dict) -> bool:
        return document["owner_id"] == scope["actorId"] or scope["role"] in document.get("reader_roles", [])

    def migrate_collection(self, scope: dict, request: dict) -> dict:
        """Copy current vectors, then CAS-promote the locator. Never delete legacy data."""
        self.writable(scope, manage=True)
        if set(request) - {"document_id", "expected_revision", "review_confirmed"} or request.get("review_confirmed") is not True:
            refuse(422, "REVIEW_REQUIRED", "Review and explicitly confirm the collection migration.")
        document = self.owned(scope, str(request.get("document_id", "")), "document")
        if document["revision"] != request.get("expected_revision") or document.get("pending_upload_id") or not document.get("current_upload_id"):
            refuse(409, "DOCUMENT_REVISION_CONFLICT", "The document changed or is indexing. Reload before migration.")
        if self.vectors is None:
            refuse(503, "INDEX_UNAVAILABLE", "Collection migration requires an available vector index.")
        # Use the reviewed document's stored classification, never downgrade it
        # using a later schema edit. A new document version adopts new schema rules.
        destination = collection_route(scope, {**document, "id": document["schema_id"]}, document["sensitivity"] == "restricted")
        if document.get("vector_collection") == destination["collection"]:
            return {"status": "ALREADY_ROUTED", "document_id": document["id"], "vector_collection": destination["collection"], "version": document["version"], "deleted_vectors": 0}
        if document.get("vector_collection"):
            refuse(409, "COLLECTION_ROUTE_INVALID", "Unknown existing collection locator; manual review is required.")
        upload = self.store.get(scope["workspaceId"], document["current_upload_id"])
        if not upload or upload["status"] != "READY":
            refuse(409, "DOCUMENT_NOT_READY", "The current file is not ready for migration.")
        candidate = {**document, "vector_collection": destination["collection"], "routing_version": 1}
        rows = document.get("form_rows") or ([document["form_fields"]] if document.get("form_fields") else [])
        text = upload["text"] + ("\n\nReviewed form records:\n" + canonical(rows) if rows else "")
        try:
            self.vectors.index(scope, {**upload, "revision": document["indexed_revision"]}, candidate, text)
        except WorkspaceError:
            raise
        except Exception:
            refuse(503, "COLLECTION_MIGRATION_FAILED", "Migration was not promoted. The previous current collection and file remain unchanged; refresh before retrying.")
        with self.lock, self.store.engine.begin() as connection:
            saved = self.store.put(scope["workspaceId"], candidate, document["revision"], connection)
            # Reload inside the transaction: a concurrent upload update must not
            # be overwritten merely to decorate a receipt.
            current_upload = self.store.get(scope["workspaceId"], upload["id"], connection)
            self.store.put(scope["workspaceId"], {**current_upload, "vector_collection": destination["collection"], "routing_version": 1}, current_upload["revision"], connection)
        return {"status": "MIGRATED", "document_id": saved["id"], "revision": saved["revision"], "version": saved["version"], "vector_collection": destination["collection"], "deleted_vectors": 0}

    def recover(self, scope: dict, upload_id: str, expected: int) -> dict:
        """Fence a stale attempt; any late vector writes remain non-current."""
        self.writable(scope)
        with self.lock, self.store.engine.begin() as connection:
            upload = self.owned(scope, upload_id, "upload")
            if upload["status"] in {"EXTRACTING", "EXTRACTION_FAILED"}:
                if upload["revision"] != expected or (upload.get("extraction_state") == "RUNNING" and time.time() - upload.get("extraction_started_at", time.time()) < 300):
                    refuse(409, "EXTRACTION_RECOVERY_NOT_READY", "Refresh first; an active extraction cannot be restarted.")
                return self.public_upload(self.store.put(scope["workspaceId"], {**upload, "status": "EXTRACTING", "extraction_state": "QUEUED"}, expected, connection))
            if upload["revision"] != expected or upload["status"] != "INDEXING" or time.time() - upload.get("index_started_at", time.time()) < 300:
                refuse(409, "INDEX_RECOVERY_NOT_READY", "Refresh first. Only an indexing attempt older than five minutes can be marked interrupted.")
            document = self.store.get(scope["workspaceId"], upload["document_id"], connection)
            if not document or document.get("pending_upload_id") != upload_id:
                refuse(409, "DOCUMENT_REVISION_CONFLICT", "This document reservation changed. Refresh its receipt.")
            self.store.put(scope["workspaceId"], {**document, "pending_upload_id": None}, document["revision"], connection)
            upload = self.store.put(scope["workspaceId"], {**upload, "status": "INTERRUPTED", "error_code": "INDEX_INTERRUPTED"}, expected, connection)
        return self.public_upload(upload)

    def commit(self, scope: dict, upload_id: str, request: dict) -> dict:
        self.writable(scope)
        allowed = {"expected_revision", "schema_id", "schema_revision", "document_type", "mode", "document_id", "document_revision", "target", "form_fields", "form_rows", "create_record", "share_with_schema_roles", "review_confirmed", "extraction_acknowledged", "synthetic_source_confirmed"}
        if set(request) - allowed or request.get("review_confirmed") is not True:
            refuse(422, "REVIEW_REQUIRED", "Review and explicitly confirm the document association and fields.")
        if "synthetic_source_confirmed" in request and type(request["synthetic_source_confirmed"]) is not bool:
            refuse(422, "REVIEW_REQUIRED", "The fictional-source declaration must be an explicit boolean.")
        fingerprint = hashlib.sha256(canonical({key: value for key, value in request.items() if key != "expected_revision"}).encode()).hexdigest()
        with self.lock:
            upload = self.owned(scope, upload_id, "upload")
            if upload["status"] in {"EXTRACTING", "EXTRACTION_FAILED"}:
                refuse(409, "EXTRACTION_NOT_COMPLETE", "Document extraction must finish before storage can be confirmed.")
            if upload["status"] == "READY":
                if upload.get("commit_fingerprint") != fingerprint:
                    refuse(409, "COMMIT_KEY_CONFLICT", "This attachment was already confirmed with different fields.")
                return self.public_upload(upload)
            if upload["revision"] != request.get("expected_revision") or upload["status"] == "INDEXING":
                refuse(409, "LIBRARY_REVISION_CONFLICT", "Reload this attachment before confirming or retrying.")
            if upload["warnings"] and request.get("extraction_acknowledged") is not True:
                refuse(422, "EXTRACTION_REVIEW_REQUIRED", "Acknowledge the extraction warnings before confirming.")
            schema = self.store.get(scope["workspaceId"], str(request.get("schema_id", "")))
            if schema is None or schema["kind"] != "schema" or schema["revision"] != request.get("schema_revision"):
                refuse(409, "SCHEMA_REVISION_CONFLICT", "The source schema changed or is unavailable. Analyse again.")
            document_type = request.get("document_type")
            if document_type not in schema["document_types"]:
                refuse(422, "DOCUMENT_TYPE_INVALID", "Choose a document type declared by this schema.")
            target = request.get("target")
            create_record = request.get("create_record") is True
            mode = request.get("mode", "new")
            if mode not in {"new", "version"}:
                refuse(422, "DOCUMENT_MODE_INVALID", "Choose a new document or an explicit new version.")
            previous = None
            if mode == "version":
                previous = self.owned(scope, str(request.get("document_id", "")), "document")
                if previous["revision"] != request.get("document_revision") or previous.get("pending_upload_id") or previous["schema_id"] != schema["id"] or previous["document_type"] != document_type:
                    refuse(409, "DOCUMENT_REVISION_CONFLICT", "The selected document changed or has a different association. Reload it.")
            if create_record:
                rows = request.get("form_rows")
                if rows is not None and (not isinstance(rows, list) or not 1 <= len(rows) <= 50 or request.get("form_fields")):
                    refuse(422, "FORM_INVALID", "A reviewed batch needs 1–50 rows, without separate single-form fields.")
                reviewed_rows = [validate_form(row, schema) for row in rows] if rows is not None else [validate_form(request.get("form_fields", {}), schema)]
                if len(canonical(reviewed_rows)) > 60_000:
                    refuse(422, "FORM_INVALID", "Reviewed form rows exceed the batch size limit.")
                record_ids = []
                old_ids = previous["target"].get("record_ids", [previous["target"].get("record_id")]) if previous else []
                for index, row in enumerate(reviewed_rows):
                    business_key = row.get(schema["primary_key"])
                    seed = f"{scope['workspaceId']}:{schema['id']}:{business_key}" if business_key is not None and business_key != "" else f"{scope['workspaceId']}:{scope['actorId']}:{upload_id}:{index}"
                    record_id = old_ids[index] if previous and index < len(old_ids) and (business_key is None or business_key == "") else "pipeline_record_" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex
                    if not record_id or record_id in record_ids:
                        refuse(409, "DUPLICATE_MEMBER_KEY", "The reviewed batch contains duplicate member keys.")
                    existing_record = self.store.get(scope["workspaceId"], record_id)
                    if existing_record and (not previous or record_id not in old_ids or existing_record["owner_id"] != scope["actorId"]):
                        refuse(409, "MEMBER_ALREADY_EXISTS", "This pipeline member key already exists. Review an explicit new version; no record was overwritten.")
                    record_ids.append(record_id)
                fields = reviewed_rows[0] if rows is None else {}
                target = {"kind": "pipeline_record", "record_id": record_ids[0], "business_key_name": schema["primary_key"], "business_key_value": str(fields.get(schema["primary_key"]) or record_ids[0]), "verified": False} if rows is None else {"kind": "pipeline_record_batch", "record_ids": record_ids, "business_key_name": schema["primary_key"], "verified": False}
            else:
                fields, reviewed_rows, record_ids = {}, [], []
                if request.get("form_rows"):
                    refuse(422, "FORM_INVALID", "ERP associations cannot create member batches.")
                if not isinstance(target, dict) or target.get("kind") != "verified_erp_record" or target.get("business_key_name") != schema["primary_key"] or not isinstance(target.get("business_key_value"), str) or not target["business_key_value"] or len(target["business_key_value"]) > 200:
                    refuse(422, "TARGET_REQUIRED", "A server-verified ERP association or reviewed pipeline form record is required.")
            if previous and previous["target"] != target:
                refuse(409, "DOCUMENT_REVISION_CONFLICT", "A new version must keep the same record association.")
            identifier = previous["id"] if previous else "document_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{scope['workspaceId']}:{scope['actorId']}:{upload_id}").hex
            reservation = previous or self.store.get(scope["workspaceId"], identifier)
            if reservation and (reservation["owner_id"] != scope["actorId"] or reservation.get("pending_upload_id") or not previous and reservation.get("current_upload_id")):
                refuse(409, "DOCUMENT_REVISION_CONFLICT", "The document has an active or completed reservation. Reload it.")
            sensitivity = "restricted" if type_is_restricted(schema, document_type) else schema["minimum_sensitivity"]
            if sensitivity == "restricted" and self.vectors and getattr(self.vectors, "requires_synthetic_confirmation", lambda _: False)(scope) and request.get("synthetic_source_confirmed") is not True:
                refuse(422, "SYNTHETIC_SOURCE_CONFIRMATION_REQUIRED", "Confirm that this source contains no real personal or medical information before using this prototype storage location.")
            destination = collection_route(scope, schema, sensitivity == "restricted")
            readers = schema["reader_roles"] if request.get("share_with_schema_roles") is True and sensitivity != "restricted" else []
            candidate = {"id": identifier, "kind": "document", "owner_id": scope["actorId"], "schema_id": schema["id"], "schema_revision": schema["revision"],
                "source_system_id": schema["source_system_id"], "source_entity": schema["source_entity"], "document_type": document_type, "target": target, "sensitivity": sensitivity,
                "reader_roles": readers, "vector_collection": destination["collection"], "routing_version": 1, "form_fields": fields, "form_rows": reviewed_rows if len(reviewed_rows) > 1 or request.get("form_rows") is not None else [], "current_upload_id": previous.get("current_upload_id") if previous else None, "version": previous.get("version", 0) if previous else 0,
                "pending_upload_id": upload_id, "created_at": previous["created_at"] if previous else timestamp()}
            if request.get("synthetic_source_confirmed") is True:
                candidate["synthetic_source_confirmed"] = True
            with self.store.engine.begin() as connection:
                # Reserve the upload without publishing its fields or access changes.
                # Existing readers must see one complete old version until promotion.
                pending = {**previous, "pending_upload_id": upload_id} if previous else candidate
                document = self.store.put(scope["workspaceId"], pending, reservation["revision"] if reservation else None, connection)
                upload = self.store.put(scope["workspaceId"], {**upload, "status": "INDEXING", "document_id": identifier, "commit_fingerprint": fingerprint, "index_started_at": time.time()}, upload["revision"], connection)
        try:
            if self.vectors is None:
                refuse(503, "INDEX_UNAVAILABLE", "The document is kept, but vector indexing is not configured.")
            text = upload["text"] + ("\n\nReviewed form records:\n" + canonical(reviewed_rows) if reviewed_rows else "")
            self.vectors.index(scope, upload, candidate, text)
            with self.lock, self.store.engine.begin() as connection:
                document = self.store.put(scope["workspaceId"], {**candidate, "current_upload_id": upload_id, "indexed_revision": upload["revision"], "version": document["version"] + 1, "pending_upload_id": None}, document["revision"], connection)
                upload = self.store.put(scope["workspaceId"], {**upload, "status": "READY", "vector_collection": destination["collection"], "routing_version": 1, "version": document["version"], "pipeline_record_id": record_ids[0] if create_record else None, "pipeline_record_ids": record_ids}, upload["revision"], connection)
                if previous and previous.get("current_upload_id"):
                    stale = self.store.get(scope["workspaceId"], previous["current_upload_id"], connection)
                    if stale:
                        self.store.put(scope["workspaceId"], {**stale, "status": "SUPERSEDED"}, stale["revision"], connection)
                if create_record:
                    for record_id, row in zip(record_ids, reviewed_rows):
                        old_record = self.store.get(scope["workspaceId"], record_id, connection)
                        if old_record and not previous:
                            refuse(409, "MEMBER_ALREADY_EXISTS", "A member key was created concurrently. No batch records were changed.")
                        self.store.put(scope["workspaceId"], {"id": record_id, "kind": "record", "owner_id": scope["actorId"], "schema_id": schema["id"], "document_id": identifier,
                            "fields": row, "source_system_id": schema["source_system_id"], "source_entity": schema["source_entity"], "origin": "reviewed_pipeline_form", "erp_created": False}, old_record["revision"] if old_record else None, connection)
            return self.public_upload(upload)
        except Exception as error:
            with self.lock, self.store.engine.begin() as connection:
                current_upload = self.store.get(scope["workspaceId"], upload_id, connection)
                current_document = self.store.get(scope["workspaceId"], identifier, connection)
                if current_upload and current_upload["revision"] == upload["revision"] and current_document and current_document.get("pending_upload_id") == upload_id:
                    self.store.put(scope["workspaceId"], {**current_document, "pending_upload_id": None}, current_document["revision"], connection)
                    upload = self.store.put(scope["workspaceId"], {**upload, "status": "INDEX_FAILED", "error_code": error.code if isinstance(error, WorkspaceError) else "INDEX_FAILED"}, upload["revision"], connection)
                elif current_upload:
                    upload = current_upload
            return self.public_upload(upload)

    def search(self, scope: dict, query: str, limit: int = 10) -> dict:
        if self.vectors is None:
            refuse(503, "INDEX_UNAVAILABLE", "Document retrieval is not configured.")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
            refuse(422, "QUERY_INVALID", "Enter a search phrase of at most 2000 characters.")
        found, seen = [], set()
        routes = {}
        documents = self.store.list(scope["workspaceId"], "document")
        for document in documents:
            if not self.can_read(scope, document) or not document.get("current_upload_id"):
                continue
            collection = document.get("vector_collection", self.vectors.collection)
            expected = collection_route(scope, {**document, "id": document["schema_id"]}, document["sensitivity"] == "restricted")["collection"]
            if collection not in {self.vectors.collection, expected}:
                refuse(503, "COLLECTION_ROUTE_INVALID", "A current document has an unrecognized destination; retrieval requires administrator review.")
            routes.setdefault(collection, []).append(document)
        for hit in self.vectors.search(scope, query, max(1, min(int(limit), 20)), routes):
            document = self.store.get(scope["workspaceId"], hit["document_id"])
            if document and self.can_read(scope, document) and document.get("vector_collection", self.vectors.collection) == hit.get("vector_collection") and document.get("current_upload_id") == hit["upload_id"] and document.get("indexed_revision") == hit.get("index_revision") and document["id"] not in seen:
                seen.add(document["id"])
                found.append({**hit, "source_system_id": document["source_system_id"], "source_entity": document["source_entity"], "document_type": document["document_type"], "version": document["version"]})
            if len(found) >= limit:
                break
        return {"items": found, "count": len(found), "collections_searched": len(routes), "inventory_complete": len(documents) < 1000, "coverage": "bounded cross-collection vector matches; not an inventory count"}

    def resolve(self, scope: dict, document_id: str) -> dict:
        document = self.store.get(scope["workspaceId"], document_id)
        if not document or document["kind"] != "document" or not self.can_read(scope, document) or not document.get("current_upload_id"):
            refuse(404, "LIBRARY_NOT_FOUND", "No permitted current document was found.")
        upload = self.store.get(scope["workspaceId"], document["current_upload_id"])
        if not upload or upload["status"] != "READY":
            refuse(409, "DOCUMENT_NOT_READY", "The current document is not confirmed searchable.")
        return {"document_id": document_id, "upload_id": upload["id"], "version": document["version"], "filename": upload["filename"], "document_type": document["document_type"],
            "source_system_id": document["source_system_id"], "source_entity": document["source_entity"], "vector_collection": document.get("vector_collection", self.vectors.collection if self.vectors else None), "target": document["target"], "text": upload["text"], "form_fields": document.get("form_fields", {}), "form_rows": document.get("form_rows", []), "warnings": upload["warnings"], "synthetic_source_confirmed": document.get("synthetic_source_confirmed", False)}
