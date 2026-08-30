"""A fully wired Member 4, driven the way an external member would drive it.

THE RULE THESE FIXTURES EXIST TO ENFORCE
----------------------------------------
Every contract test in this package goes through HTTP. Not through
``OrchestrationService``, not through a stage function, not through a store.
An integration test that reaches into Python internals proves the internals
work; it proves nothing about the surface Members 2 and 3 actually get, which
is the only thing Phase 11 is about.

The one deliberate exception is inspection AFTER a flow has run - counting rows
in a store to prove a secret was not persisted, for instance. That is
measurement of a side effect, not participation in the contract.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from erp_pipeline.ai.service import EmbeddingService
from erp_pipeline.api import ApiSettings, create_app
from erp_pipeline.ingestion import FileIngestionService
from erp_pipeline.mapping import MappingService
from erp_pipeline.orchestration import (
    InlineJobExecutor,
    InMemoryJobStore,
    InMemoryLifecycleRegistry,
    InMemoryRepresentationStore,
    InMemorySecretProvider,
    OrchestrationService,
    PipelineServices,
    UploadStore,
)
from erp_pipeline.storage.state import InMemoryTierStateStore
from erp_pipeline.sync import InMemoryCanonicalStore
from erp_pipeline.transformation import TransformationService

from tests.erp_pipeline.api.test_search_resolution_and_filters import (
    DIMENSION,
    DeterministicTestModel,
    InProcessTier,
    PatchedStorage,
)

#: The service key Members 2 and 3 present. A test value; it must never appear
#: in a response, a log line or the frontend bundle.
SERVICE_API_KEY = "PHASE11_SERVICE_KEY_4471"

#: A browser origin the deployment has explicitly allowed.
ALLOWED_ORIGIN = "http://localhost:5173"
#: One it has not.
FOREIGN_ORIGIN = "https://attacker.example"


def _load_project_env() -> None:
    """Load ``.env`` so an OCR probe finds Tesseract, as other suites do."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")


_load_project_env()


CERTIFICATE_LINES = [
    "BIRTH CERTIFICATE",
    "Registrar General of Births and Deaths, Colombo",
    "Name: Nimal Silva",
    "Employee Reference: EMP002",
    "Date of Birth: 1991-06-14",
    "Place of Birth: Kandy",
    "Registration Number: BC-1991-44127",
]


def build_pdf(lines) -> bytes:
    """A small text PDF. Each line is placed separately: one long
    ``insert_text`` call is clipped at the page edge and collapses to a single
    chunk, which Phase 9 measured the hard way."""
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    page = document.new_page()

    for index, line in enumerate(lines):
        page.insert_text((56, 70 + index * 22), line, fontsize=11)

    payload = document.tobytes()
    document.close()

    return payload


def build_png_of_text(text: str) -> bytes:
    """A PNG carrying readable text, so the OCR path has something to find."""
    fitz = pytest.importorskip("pymupdf")
    typed = fitz.open()
    typed.new_page(width=460, height=190).insert_text((28, 100), text, fontsize=26)
    bitmap = typed.load_page(0).get_pixmap(dpi=300).tobytes("png")
    typed.close()

    return bitmap


def build_blank_png() -> bytes:
    pillow = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pillow.new("RGB", (220, 110), "white").save(buffer, "PNG")

    return buffer.getvalue()


EMPLOYEE_CSV = (
    "employee_id,full_name,department,designation,employment_status,joined_on\n"
    "EMP001,Kamal Perera,Human Resources,HR Executive,ACTIVE,2018-01-09\n"
    "EMP002,Nimal Silva,Finance,Senior Accounts Officer,ACTIVE,2019-03-11\n"
    "EMP003,Sunil Fernando,Procurement,Procurement Officer,RESIGNED,2017-07-24\n"
    "EMP004,Ayesha Jayawardena,Finance,Accounts Assistant,ACTIVE,2021-11-02\n"
).encode("utf-8")


class Member4:
    """Member 4 as a deployment: every service wired, one inline executor.

    ``rebuild_app`` exists for the restart test. The stores are held on this
    object rather than inside the app, so a new application can be built over
    the same persisted state - which is what surviving a restart means.
    """

    def __init__(self, tmp_path: Path, *, api_key: str | None = SERVICE_API_KEY,
                 cors_origins: tuple[str, ...] = (ALLOWED_ORIGIN,)) -> None:
        self.tmp_path = tmp_path
        self.api_key = api_key
        self.cors_origins = cors_origins

        # State that must outlive a restart.
        self.representations = InMemoryRepresentationStore()
        # Phase 9's current-version registry. Without it the LIFECYCLE_COMMIT
        # stage has nowhere to record which version of a slot is current, so a
        # replaced document is never superseded and BOTH versions keep coming
        # back - which is what a deployment that omits this service actually
        # gets, and is why the harness now wires it explicitly.
        self.lifecycle = InMemoryLifecycleRegistry()
        self.state_store = InMemoryTierStateStore()
        self.hot = InProcessTier()
        self.uploads = UploadStore(tmp_path / "uploads")
        self.records = InMemoryCanonicalStore()

        self.build_app()

    def build_app(self):
        self.storage = PatchedStorage(hot=self.hot, state_store=self.state_store)
        self.services = PipelineServices(
            ingestion=FileIngestionService(),
            mapping=MappingService(),
            transformation=TransformationService(),
            records=self.records,
            representations=self.representations,
            lifecycle=self.lifecycle,
            storage=self.storage,
            embedding=EmbeddingService(DeterministicTestModel(dimension=DIMENSION)),
            uploads=self.uploads,
            secrets=InMemorySecretProvider(),
        )
        self.orchestration = OrchestrationService(
            services=self.services,
            job_store=InMemoryJobStore(),
            executor=InlineJobExecutor(),
        )
        self.settings = ApiSettings(
            upload_dir=self.tmp_path / "uploads",
            api_key=self.api_key,
            cors_origins=self.cors_origins,
        )
        self.app = create_app(
            settings=self.settings, orchestration=self.orchestration
        )

        return self.app

    def rebuild_app(self):
        """Restart Member 4 over the same stores."""
        return self.build_app()


@pytest.fixture
def member4(tmp_path):
    pytest.importorskip("pymupdf")

    return Member4(tmp_path)


@pytest.fixture
def client(member4):
    from fastapi.testclient import TestClient

    with TestClient(member4.app) as test_client:
        yield test_client


@pytest.fixture
def member1():
    from tests.erp_pipeline.integration.fakes import FakeMember1

    return FakeMember1()


@pytest.fixture
def member2(client):
    from tests.erp_pipeline.integration.fakes import FakeMember2

    return FakeMember2(client=client, api_key=SERVICE_API_KEY)


@pytest.fixture
def member3(client, member1, member2):
    from tests.erp_pipeline.integration.fakes import FakeMember3

    return FakeMember3(
        client=client,
        api_key=SERVICE_API_KEY,
        member1=member1,
        member2=member2,
    )
