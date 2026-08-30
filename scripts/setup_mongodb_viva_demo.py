"""Seed a deterministic MongoDB demo database for the SQL-vs-NoSQL walkthrough.

WHAT THIS IS FOR
----------------
The pipeline discovers relational schemas by READING a declared schema, and
MongoDB schemas by OBSERVING a bounded sample of documents. Both paths end at
the same ``SourceSchema`` / ``SourceEntity`` / ``SourceField`` contracts. This
script creates a collection shaped to make that visible: every BSON type the
inference layer handles, plus the awkward cases - a field whose type varies
between documents, a field missing from some documents, an empty array, and a
binary attachment.

WHY A SCRIPT RATHER THAN SHELL COMMANDS IN A DOCUMENT
-----------------------------------------------------
A demo that depends on pasting twenty mongosh commands in the right order fails
in front of an audience. This is idempotent: it drops and recreates ONLY its own
demo database, so running it twice produces exactly the same state.

SAFETY
------
* Touches one database, named by ``MONGO_VIVA_DB`` and defaulting to
  ``erp_viva_mongodb_demo``. It refuses to run against anything else.
* Reads connection details from the environment. No credential is hard-coded,
  and none is printed - the summary reports counts and types only.
* Every value is synthetic. No real person, employer or document.

Run:
    .venv/Scripts/python.exe scripts/setup_mongodb_viva_demo.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

#: The ONLY database this script will drop. Guarded below.
DEFAULT_DEMO_DB = "erp_viva_mongodb_demo"

def _demo_pdf() -> bytes:
    """A real PDF carrying real text.

    A bare ``%PDF`` header with no page content passes magic-byte detection and
    then yields nothing to chunk, which fails the multimodal stage. The demo
    should exercise the extractor properly, so this builds a genuine document.
    """
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()

    for index, line in enumerate([
        "CERTIFICATE OF EMPLOYMENT",
        "Synthetic Demo Registry, Colombo",
        "Employee Reference: EMP002",
        "Position: Senior Accounts Officer",
        "Department: Finance",
        "Issued: 2019-03-11",
        "Reference Number: DEMO-2019-44127",
    ]):
        page.insert_text((56, 70 + index * 22), line, fontsize=11)

    payload = document.tobytes()
    document.close()

    return payload


def _demo_png() -> bytes:
    """A real PNG containing readable text, so the OCR path has work to do."""
    import pymupdf

    document = pymupdf.open()
    document.new_page(width=460, height=190).insert_text(
        (28, 100), "EMP004 STAFF ID CARD", fontsize=26
    )
    bitmap = document.load_page(0).get_pixmap(dpi=300).tobytes("png")
    document.close()

    return bitmap


DEMO_PDF = _demo_pdf()
DEMO_PNG = _demo_png()


def _client():
    import pymongo

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:  # pragma: no cover - dotenv is optional
        pass

    host = os.getenv("MONGO_VIVA_HOST") or os.getenv("MONGO_PHASE5_HOST", "localhost")
    port = int(os.getenv("MONGO_VIVA_PORT") or os.getenv("MONGO_PHASE5_PORT", "27017"))
    user = os.getenv("MONGO_VIVA_USER") or os.getenv("MONGO_PHASE5_ADMIN_USER")
    password = os.getenv("MONGO_VIVA_PASSWORD") or os.getenv(
        "MONGO_PHASE5_ADMIN_PASSWORD"
    )
    auth_db = os.getenv("MONGO_PHASE5_AUTH_DB", "admin")

    kwargs = {"serverSelectionTimeoutMS": 8000}

    if user and password:
        kwargs.update(username=user, password=password, authSource=auth_db)

    return pymongo.MongoClient(f"mongodb://{host}:{port}", **kwargs), host, port


def employees(ObjectId, Decimal128, Binary, Int64):
    """Nine employees, deliberately not uniform.

    The shape variation is the point: a relational table cannot express it, and
    the inference layer has to report what it actually observed rather than
    what a schema promised.
    """
    return [
        {
            "_id": ObjectId("650000000000000000000001"),
            "employee_id": "EMP001",
            "name": "Kamal Perera",
            "department": "Human Resources",
            "salary": Decimal128("98000.00"),
            "active": True,
            "joined_at": datetime(2018, 1, 9, tzinfo=timezone.utc),
            "leave_days": 21,
            "employment": {
                "grade": 5,
                "contract": {"type": "permanent", "probation_months": 3},
            },
            "tags": ["hr", "onboarding"],
            "email": "kamal.perera@example.invalid",
        },
        {
            "_id": ObjectId("650000000000000000000002"),
            "employee_id": "EMP002",
            "name": "Nimal Silva",
            "department": "Finance",
            "salary": Decimal128("125000.50"),
            "active": True,
            "joined_at": datetime(2019, 3, 11, tzinfo=timezone.utc),
            "leave_days": 18,
            "employment": {
                "grade": 7,
                "contract": {"type": "permanent", "probation_months": 6},
            },
            "tags": ["finance", "manager"],
            "email": "nimal.silva@example.invalid",
            # The binary path: a real PDF signature, so detection is genuine.
            "birth_certificate": Binary(DEMO_PDF),
        },
        {
            "_id": ObjectId("650000000000000000000003"),
            "employee_id": "EMP003",
            "name": "Sunil Fernando",
            "department": "Procurement",
            # DOUBLE where the others are Decimal128 - the numeric widening case.
            "salary": 87500.75,
            "active": False,
            "joined_at": datetime(2017, 7, 24, tzinfo=timezone.utc),
            "leave_days": Int64(30),
            "employment": {"grade": 4},
            "tags": [],
            # email absent entirely - optionality, not nullability
        },
        {
            "_id": ObjectId("650000000000000000000004"),
            "employee_id": "EMP004",
            "name": "Ayesha Jayawardena",
            "department": "Finance",
            "salary": Decimal128("76000.00"),
            "active": True,
            "joined_at": datetime(2021, 11, 2, tzinfo=timezone.utc),
            "leave_days": 14,
            "employment": {
                "grade": 3,
                "contract": {"type": "fixed_term", "probation_months": 3},
            },
            "tags": ["finance"],
            # explicit null - distinct from absent
            "email": None,
            "profile_photo": Binary(DEMO_PNG),
        },
        {
            "_id": ObjectId("650000000000000000000005"),
            "employee_id": "EMP005",
            "name": "Ruwan Bandara",
            "department": "Plant Operations",
            "salary": Decimal128("64000.00"),
            "active": True,
            "joined_at": datetime(2022, 5, 30, tzinfo=timezone.utc),
            "leave_days": 12,
            "employment": {"grade": 2},
            "tags": ["operations", "shift-lead"],
            "email": "ruwan.bandara@example.invalid",
            # An array of embedded documents.
            "certifications": [
                {"name": "Forklift", "issued": datetime(2022, 6, 1, tzinfo=timezone.utc)},
                {"name": "Safety L2", "issued": datetime(2023, 2, 14, tzinfo=timezone.utc)},
            ],
        },
        {
            "_id": ObjectId("650000000000000000000006"),
            "employee_id": "EMP006",
            "name": "Dilani Weerasinghe",
            "department": "Human Resources",
            "salary": Decimal128("91000.00"),
            "active": True,
            "joined_at": datetime(2020, 9, 14, tzinfo=timezone.utc),
            "leave_days": 20,
            "employment": {"grade": 6},
            "tags": ["hr", "recruitment"],
            "email": "dilani.w@example.invalid",
            # THE MIXED-TYPE FIELD. A string here, an ObjectId below. The
            # inference layer must report this honestly rather than electing a
            # majority type.
            "supervisor_ref": "EMP001",
        },
        {
            "_id": ObjectId("650000000000000000000007"),
            "employee_id": "EMP007",
            "name": "Chaminda Rathnayake",
            "department": "Procurement",
            "salary": Decimal128("70000.00"),
            "active": True,
            "joined_at": datetime(2023, 1, 16, tzinfo=timezone.utc),
            "leave_days": 10,
            "employment": {"grade": 3},
            "tags": ["procurement"],
            "email": "chaminda.r@example.invalid",
            "supervisor_ref": ObjectId("650000000000000000000003"),
        },
        {
            "_id": ObjectId("650000000000000000000008"),
            "employee_id": "EMP008",
            "name": "Priya Anandan",
            "department": "Finance",
            "salary": Decimal128("103000.25"),
            "active": True,
            "joined_at": datetime(2019, 8, 5, tzinfo=timezone.utc),
            "leave_days": 22,
            "employment": {
                "grade": 6,
                "contract": {"type": "permanent", "probation_months": 6},
            },
            "tags": ["finance", "audit"],
            "email": "priya.anandan@example.invalid",
        },
        {
            "_id": ObjectId("650000000000000000000009"),
            "employee_id": "EMP009",
            "name": "Tharindu Silva",
            "department": "Plant Operations",
            "salary": Decimal128("58000.00"),
            "active": False,
            "joined_at": datetime(2016, 4, 18, tzinfo=timezone.utc),
            "leave_days": 0,
            "employment": {"grade": 1},
            "tags": ["operations"],
            "email": "tharindu.silva@example.invalid",
        },
    ]


def invoices(ObjectId, Decimal128, Int64):
    """Five invoices with nested line items - the array-of-documents case."""
    return [
        {
            "_id": ObjectId(f"6500000000000000000001{i:02d}"),
            "invoice_id": f"INV-{1000 + i}",
            "customer_id": f"CUS-{i:02d}",
            "amount": Decimal128(amount),
            "currency": currency,
            "status": status,
            "issued_on": datetime(2025, month, 15, tzinfo=timezone.utc),
            "line_count": Int64(len(lines)),
            "lines": lines,
        }
        for i, (amount, currency, status, month, lines) in enumerate(
            [
                ("15400.50", "LKR", "approved", 1,
                 [{"sku": "SKU-A", "qty": 2, "unit_price": Decimal128("7700.25")}]),
                ("8200.00", "USD", "pending", 2,
                 [{"sku": "SKU-B", "qty": 1, "unit_price": Decimal128("8200.00")}]),
                ("45300.75", "EUR", "approved", 3,
                 [{"sku": "SKU-C", "qty": 3, "unit_price": Decimal128("15100.25")},
                  {"sku": "SKU-D", "qty": 1, "unit_price": Decimal128("0.00")}]),
                ("2750.25", "LKR", "rejected", 4, []),
                ("19800.00", "GBP", "settled", 5,
                 [{"sku": "SKU-E", "qty": 5, "unit_price": Decimal128("3960.00")}]),
            ],
            start=1,
        )
    ]


def main() -> int:
    try:
        from bson import Binary, Decimal128, Int64, ObjectId
    except ImportError:
        print("pymongo/bson is not installed in this environment")

        return 2

    demo_db = os.getenv("MONGO_VIVA_DB", DEFAULT_DEMO_DB)

    # Guard: this script drops a database. It drops ONLY its own.
    if demo_db != DEFAULT_DEMO_DB and not demo_db.startswith("erp_viva_"):
        print(
            f"refusing to operate on {demo_db!r}: this script only manages a "
            f"database named {DEFAULT_DEMO_DB!r} or prefixed 'erp_viva_'"
        )

        return 2

    try:
        client, host, port = _client()
        version = client.server_info()["version"]
    except Exception as error:  # noqa: BLE001
        print(f"MongoDB is not reachable: {type(error).__name__}")
        print("Set MONGO_VIVA_HOST / MONGO_VIVA_PORT, or start a local server.")

        return 1

    print(f"connected to MongoDB {version} at {host}:{port}")

    # Idempotent: same input, same end state.
    client.drop_database(demo_db)
    database = client[demo_db]

    employee_docs = employees(ObjectId, Decimal128, Binary, Int64)
    invoice_docs = invoices(ObjectId, Decimal128, Int64)

    database["employees"].insert_many(employee_docs)
    database["invoices"].insert_many(invoice_docs)

    print(f"database        : {demo_db} (dropped and recreated)")
    print(f"  employees     : {database['employees'].count_documents({})} documents")
    print(f"  invoices      : {database['invoices'].count_documents({})} documents")
    print("\nshape variation seeded for inference demonstration:")
    print("  ObjectId          _id on every document")
    print("  string            name, department, email")
    print("  int32 / int64     leave_days (int and Int64)")
    print("  Decimal128        salary, amount, unit_price")
    print("  double            salary on EMP003 - numeric widening case")
    print("  boolean           active")
    print("  date              joined_at, issued_on")
    print("  binData           birth_certificate (PDF), profile_photo (PNG)")
    print("  embedded object   employment, employment.contract (two levels)")
    print("  primitive array   tags")
    print("  empty array       tags on EMP003, lines on INV-1004")
    print("  document array    certifications, lines")
    print("  absent field      email missing on EMP003")
    print("  explicit null     email null on EMP004")
    print("  MIXED TYPE        supervisor_ref: string on EMP006, ObjectId on EMP007")
    print("\nAll values are synthetic. No real person or document.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
