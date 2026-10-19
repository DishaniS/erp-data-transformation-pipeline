"""Actual loopback FastAPI uploads: synthetic classification corpus, no stores.

Run with pytest -s to see metrics; exact responses/results are saved under
artifacts/document_classification/<unique-run>/. No prior evidence is changed.
"""
from __future__ import annotations

import io
import json
import math
import os
import socket
import statistics
import threading
import time
from pathlib import Path

import pytest

from erp_pipeline.ingestion.document_classification import classify_upload_text


TEXTS = {
    "nic": "NATIONAL IDENTITY CARD\nNIC NUMBER: 199512345678\nNAME: TEST PERSON\nDATE OF BIRTH: 1995-01-01\nSYNTHETIC TEST DOCUMENT",
    "birth_certificate": "CERTIFICATE OF BIRTH\nNAME: TEST CHILD\nDATE OF BIRTH: 2000-01-01\nPLACE OF BIRTH: TEST TOWN\nBIRTH REGISTRATION\nREGISTRAR GENERAL\nSYNTHETIC TEST DOCUMENT",
    "medical_bill": "MEDICAL INVOICE\nTEST HOSPITAL\nPATIENT: TEST PERSON\nCONSULTATION AND TREATMENT\nCHARGES: 150.00\nTOTAL AMOUNT: 150.00\nSYNTHETIC TEST DOCUMENT",
}
LABELS = (*TEXTS, "unknown")


def _pdf(texts):
    import fitz
    doc = fitz.open()
    for text in texts:
        doc.new_page(width=650, height=450).insert_text((30, 45), text, fontsize=21)
    data = doc.tobytes()
    doc.close()
    return data


def _image(text):
    import fitz
    from PIL import Image
    doc = fitz.open(stream=_pdf([text]), filetype="pdf")
    data = doc[0].get_pixmap(dpi=150).tobytes("png")
    doc.close()
    return Image.open(io.BytesIO(data)).convert("RGB")


def _image_bytes(text, extension="png", condition="clear"):
    from PIL import ImageFilter
    image = _image(text)
    if condition == "blurred":
        image = image.filter(ImageFilter.GaussianBlur(2.4))
    elif condition == "rotated":
        image = image.rotate(90, expand=True, fillcolor="white")
    result = io.BytesIO()
    image.save(result, format="JPEG" if extension in {"jpg", "jpeg"} else "PNG")
    return result.getvalue()


def _corpus():
    import fitz
    cases = []
    for label, text in TEXTS.items():
        for extension in ("png", "jpg", "jpeg", "pdf"):
            payload = _pdf([text]) if extension == "pdf" else _image_bytes(text, extension)
            cases.append((label, extension, "clear", payload))
        for condition in ("blurred", "rotated"):
            cases.append((label, "png", condition, _image_bytes(text, condition=condition)))
        cases.append((label, "pdf", "multipage", _pdf([
            "Synthetic cover page. No document classification information is on this page.", text,
        ])))
        doc = fitz.open()
        page = doc.new_page(width=650, height=450)
        page.insert_image(page.rect, stream=_image_bytes(text))
        payload = doc.tobytes(); doc.close()
        cases.append((label, "pdf", "scanned", payload))
        overlap = {
            "nic": "\nPLACE OF BIRTH: TEST TOWN",
            "birth_certificate": "\nPARENT NIC NUMBER: 951234567V",
            "medical_bill": "\nNIC NUMBER: 951234567V\nDATE OF BIRTH: 1995-01-01",
        }[label]
        cases.append((label, "pdf", "overlap", _pdf([text + overlap])))
    unknowns = [
        "HOSPITAL",
        "NAME: TEST PERSON\nDATE OF BIRTH: 2000-01-01",
        "NATIONAL IDENTITY CARD\nCERTIFICATE OF BIRTH\nMEDICAL INVOICE",
        "OFFICE SUPPLIES INVOICE\nTOTAL AMOUNT: 150\nOFFICE CHAIRS",
        "199512345678",
        "WEATHER OBSERVATION\nTHE SKY IS CLEAR\nTEMPERATURE IS NORMAL",
    ]
    for index, text in enumerate(unknowns):
        extension = "pdf" if index % 2 else "png"
        cases.append(("unknown", extension, "negative", _pdf([text]) if extension == "pdf" else _image_bytes(text)))
    cases.extend([
        ("unknown", "png", "empty", _image_bytes("")),
        ("unknown", "pdf", "conflicting_pages", _pdf([TEXTS["nic"], TEXTS["birth_certificate"]])),
        ("unknown", "pdf", "garbled", _pdf(["qzx wqz 765 hjkl ???"]))
    ])
    return cases


@pytest.fixture(scope="module")
def real_upload_api(tmp_path_factory):
    """Real local model/server; no database/vector service is constructed."""
    from dotenv import dotenv_values
    import httpx
    import uvicorn
    from erp_pipeline.api import create_app, ApiSettings
    from erp_pipeline.ingestion import FileIngestionService
    from erp_pipeline.ingestion.ocr import probe_ocr
    from erp_pipeline.ai import SentenceTransformerModel, EmbeddingService
    from erp_pipeline.orchestration import PipelineServices, OrchestrationService, UploadStore, InMemoryJobStore, InlineJobExecutor
    from erp_pipeline.orchestration.representation_store import InMemoryRepresentationStore
    root = Path(__file__).resolve().parents[3]
    with pytest.MonkeyPatch.context() as patch:
        for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
            patch.setenv(key, "1")
        settings = dotenv_values(root / ".env")
        for key in ("TESSERACT_CMD", "TESSERACT_PATH"):
            if settings.get(key): patch.setenv(key, settings[key])
        assert probe_ocr().available, "Local Tesseract/English data required; no simulated OCR fallback"
        model = SentenceTransformerModel(device="cpu")
        started = time.perf_counter()
        assert model.dimension == 384
        model.encode(["synthetic classification warmup"])
        warmup_ms = (time.perf_counter() - started) * 1000
        memory = InMemoryRepresentationStore()
        services = PipelineServices(ingestion=FileIngestionService(), embedding=EmbeddingService(model),
                                    representations=memory, uploads=UploadStore(tmp_path_factory.mktemp("classification") / "uploads"))
        assert services.storage is None and services.catalog is None and services.connection_factory is None
        orchestration = OrchestrationService(services=services, job_store=InMemoryJobStore(), executor=InlineJobExecutor())
        app = create_app(settings=ApiSettings(), orchestration=orchestration)
        # Permit only the loopback HTTP transport during the integration test.
        real_connect = socket.socket.connect
        def local_connect(sock, address):
            if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
                raise AssertionError("External connections forbidden in classification evaluation")
            return real_connect(sock, address)
        patch.setattr(socket.socket, "connect", local_connect)
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True); thread.start()
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline: time.sleep(.02)
        assert server.started
        client = httpx.Client(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=120, trust_env=False)
        try:
            yield client, services, memory, model, warmup_ms
        finally:
            client.close(); server.should_exit = True; thread.join(timeout=20); listener.close()
            assert not thread.is_alive(), "Local test server failed to shut down"


def test_labeled_files_through_actual_fastapi_upload(real_upload_api):
    client, services, memory, model, warmup_ms = real_upload_api
    root = Path(__file__).resolve().parents[3]
    out = root / "artifacts" / "document_classification" / str(time.time_ns())
    out.mkdir(parents=True, exist_ok=False)
    rows = []; errors = []
    for i, (expected, extension, condition, payload) in enumerate(_corpus()):
        started = time.perf_counter()
        response = client.post("/v1/files/documents", files={"file": (f"scan_{i:02d}.{extension}", payload,
                               "application/pdf" if extension == "pdf" else ("image/png" if extension == "png" else "image/jpeg"))},
                               data={"sensitivity": "public"})
        latency = (time.perf_counter() - started) * 1000
        assert response.status_code == 201, response.text
        body = response.json(); predicted = body["document_type"]
        job_response = client.get("/v1/jobs/" + body["index_job_id"]); job = job_response.json()
        extraction = services.upload_results.get(body["upload_id"])
        classification = extraction.document.document_metadata["document_classification"]
        assert classification == {k: body[k] for k in ("document_type", "confidence_score", "matched_rules")}
        reps = [memory.get(rid) for rid in memory.list_ids() if memory.get(rid).metadata.get("document_id") == body["document_id"]]
        for rep in reps:
            assert rep.metadata["document_classification"] == classification
            if predicted != "unknown": assert rep.metadata["document_type"] == predicted
            resolved = client.get("/v1/representations/" + rep.representation_id)
            assert resolved.status_code == 200 and resolved.json()["text"] == rep.text_for_ai
        assert job["counters"]["embeddings_generated"] == len(reps)
        assert job["counters"]["vectors_stored"] == 0
        assert job["status"] == ("partial" if reps else "succeeded")
        if condition not in {"blurred", "rotated"} and expected != predicted:
            errors.append(f"{i} {expected}/{condition}: {predicted}")
        row = dict(id=i,expected=expected,predicted=predicted,extension=extension,condition=condition,
                   upload_latency_ms=latency,http_status=response.status_code,response=body,job=job,
                   chunks=len(reps),model=model.model_id,dimension=model.dimension)
        rows.append(row)
        (out / "responses.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    confusion = {truth: {pred: 0 for pred in LABELS} for truth in LABELS}
    for row in rows: confusion[row["expected"]][row["predicted"]] += 1
    per_class = {}
    for label in LABELS:
        tp = confusion[label][label]; actual = sum(confusion[label].values()); predicted = sum(confusion[k][label] for k in LABELS)
        per_class[label] = dict(precision=tp/predicted if predicted else None, recall=tp/actual if actual else None, support=actual)
    result = dict(documents=len(rows),correct=sum(r["expected"]==r["predicted"] for r in rows),
                  incorrect=sum(r["expected"]!=r["predicted"] for r in rows),unknown_predictions=sum(r["predicted"]=="unknown" for r in rows),
                  known_class_abstentions=sum(r["predicted"]=="unknown" and r["expected"]!="unknown" for r in rows),
                  accuracy=sum(r["expected"]==r["predicted"] for r in rows)/len(rows),confusion_matrix=confusion,per_class=per_class,
                  median_upload_ms=statistics.median(r["upload_latency_ms"] for r in rows),p95_upload_ms=sorted(r["upload_latency_ms"] for r in rows)[math.ceil(.95*len(rows))-1],
                  warmup_ms=warmup_ms,postgres_connections=0,qdrant_clients=0,vector_writes=0,
                  scope="Fixed labeled English synthetic corpus; real loopback FastAPI, existing OCR/PDF parsers and local embedding model. Unknown is a correct rejection only for unknown-labeled inputs.")
    (out / "metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("\nCLASSIFICATION_METRICS", str(out), json.dumps(result))
    assert len(rows) == 36
    assert not errors, errors


def test_declared_type_and_erp_identity_are_preserved(real_upload_api):
    client, services, memory, model, _ = real_upload_api
    response = client.post("/v1/files/documents", files={"file": ("employee999.png", _image_bytes(TEXTS["nic"]), "image/png")},
                           data={"document_type": "caller_declared_type", "source_system_id": "synthetic", "sensitivity": "public"})
    body = response.json()
    assert response.status_code == 201 and body["document_type"] == "nic"
    reps = [memory.get(rid) for rid in memory.list_ids() if memory.get(rid).metadata.get("source_system_id") == "synthetic"]
    assert reps
    for rep in reps:
        assert rep.metadata["document_type"] == "caller_declared_type"
        assert rep.metadata["document_classification"]["document_type"] == "nic"
        assert rep.metadata.get("business_key_value") is None


def test_filename_and_ocr_failure_cannot_classify(real_upload_api):
    from erp_pipeline.ingestion.models import IngestionOptions, ImageOptions
    client, services, memory, model, _ = real_upload_api
    previous = services.ingestion
    from erp_pipeline.ingestion import FileIngestionService
    services.ingestion = FileIngestionService(IngestionOptions(image=ImageOptions(ocr_enabled=False)))
    try:
        response = client.post("/v1/files/documents", files={"file": ("national_identity_card.png", _image_bytes(TEXTS["nic"]), "image/png")})
    finally:
        services.ingestion = previous
    assert response.status_code == 201
    assert response.json()["extraction_status"] == "ocr_unavailable"
    assert response.json()["document_type"] == "unknown"
    assert response.json()["confidence_score"] == 0


def test_classifier_consumes_all_page_text_without_first_page_prefix_limit():
    from types import SimpleNamespace
    from erp_pipeline.ingestion.document_classification import classify_uploaded_document
    doc = SimpleNamespace(status="extracted",pages=[SimpleNamespace(text="Cover material. "*300),SimpleNamespace(text=TEXTS["birth_certificate"])])
    assert classify_uploaded_document(doc)["document_type"] == "birth_certificate"
    doc.pages.append(SimpleNamespace(text=TEXTS["medical_bill"]))
    assert classify_uploaded_document(doc)["document_type"] == "unknown"
