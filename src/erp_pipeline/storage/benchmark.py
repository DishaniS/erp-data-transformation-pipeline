"""The Phase 12 research benchmark: same corpus, three tiers, measured.

METHODOLOGY
-----------
One corpus of real MiniLM embeddings is loaded into HOT, into WARM, and into
COLD archives. The SAME query set runs against each. Recall is measured against
hand-declared labels - never against another tier's ranking, which would make
HOT perfect by definition and measure agreement rather than quality.

Latency uses a monotonic timer with warm-up runs discarded, and reports median
plus p95 (the latter only once there are enough samples for it to mean
anything).

WHAT IS COMPARABLE AND WHAT IS NOT
----------------------------------
``VECTOR_PAYLOAD_PROXY`` is the like-for-like footprint: vector components only,
in each tier's stored representation. The cold ARCHIVE size is measured
separately and is a different scope - it includes header, nonce, GCM tag and
metadata - so the two are reported side by side and never subtracted from each
other.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from erp_pipeline.storage.models import MeasurementKind, StorageTier

#: Deterministic ERP corpus templates. Five domains, structurally different
#: wording, so retrieval has to do more than match one obvious token.
ENTITY_TEMPLATES: Mapping[str, tuple[str, ...]] = {
    "invoice": (
        "Entity: Invoice\nInvoice Id: INV-{n:05d}\nCustomer Id: CUS-{c:04d}\n"
        "Amount: {amount}\nCurrency: {currency}\nStatus: {status}\n"
        "Issued On: {date}",
    ),
    "customer": (
        "Entity: Customer\nCustomer Id: CUS-{n:05d}\nName: {name}\n"
        "Email: contact{n}@example.test\nCountry: {country}\n"
        "Segment: {segment}",
    ),
    "payment": (
        "Entity: Payment\nPayment Id: PAY-{n:05d}\nInvoice Id: INV-{c:05d}\n"
        "Amount: {amount}\nMethod: {method}\nSettled On: {date}",
    ),
    "purchase_order": (
        "Entity: Purchase Order\nPurchase Order Id: PO-{n:05d}\n"
        "Supplier Id: SUP-{c:04d}\nAmount: {amount}\nStatus: {status}\n"
        "Raised On: {date}",
    ),
    "expense": (
        "Entity: Expense Claim\nClaim Id: EXP-{n:05d}\nEmployee Id: EMP-{c:04d}\n"
        "Amount: {amount}\nCategory: {category}\nTrip: {trip}\n"
        "Submitted On: {date}",
    ),
}

_CURRENCIES = ("LKR", "USD", "EUR", "GBP")
_STATUSES = ("approved", "rejected", "pending", "settled", "draft")
_METHODS = ("bank transfer", "cheque", "credit card", "direct debit")
_COUNTRIES = ("Sri Lanka", "Netherlands", "Germany", "United Kingdom")
_SEGMENTS = ("wholesale", "retail", "government", "enterprise")
_CATEGORIES = ("air travel", "hotel", "meals", "ground transport", "conference")
_TRIPS = ("Colombo to Amsterdam", "Berlin summit", "London audit visit",
          "regional sales tour", "supplier factory inspection")
_NAMES = ("Acme Trading", "Beta Supplies", "Gamma Logistics", "Delta Foods",
          "Epsilon Manufacturing", "Zeta Consulting", "Eta Pharmaceuticals")


@dataclass(frozen=True)
class BenchmarkRecord:
    """One corpus entry: an id, its text and its declared attributes."""

    representation_id: str
    entity_type: str
    text: str
    ordinal: int


@dataclass(frozen=True)
class BenchmarkQuery:
    """A query and the record a human says should answer it."""

    query: str
    expected_representation_id: str
    entity_type: str


def build_corpus(size: int = 500) -> tuple[BenchmarkRecord, ...]:
    """A deterministic ERP corpus spanning five entity types.

    Fully deterministic - no RNG - so a rerun benchmarks the same data and two
    runs are comparable.
    """
    records: list[BenchmarkRecord] = []
    entity_types = list(ENTITY_TEMPLATES)
    base_date = datetime(2025, 1, 1, tzinfo=timezone.utc)

    for index in range(size):
        entity_type = entity_types[index % len(entity_types)]
        template = ENTITY_TEMPLATES[entity_type][0]
        counterpart = (index * 7) % max(1, size // 2) + 1
        date = (base_date + timedelta(days=index % 730)).date().isoformat()

        text = template.format(
            n=index + 1,
            c=counterpart,
            amount=f"{(index * 137) % 99000 + 100}.{index % 100:02d}",
            currency=_CURRENCIES[index % len(_CURRENCIES)],
            status=_STATUSES[index % len(_STATUSES)],
            method=_METHODS[index % len(_METHODS)],
            country=_COUNTRIES[index % len(_COUNTRIES)],
            segment=_SEGMENTS[index % len(_SEGMENTS)],
            category=_CATEGORIES[index % len(_CATEGORIES)],
            trip=_TRIPS[index % len(_TRIPS)],
            name=_NAMES[index % len(_NAMES)],
            date=date,
        )

        records.append(
            BenchmarkRecord(
                representation_id=f"ai:{entity_type}:bench-{index + 1:05d}",
                entity_type=entity_type,
                text=text,
                ordinal=index,
            )
        )

    return tuple(records)


def build_queries(
    corpus: Sequence[BenchmarkRecord], count: int = 40
) -> tuple[BenchmarkQuery, ...]:
    """Hand-shaped query templates over deterministically chosen targets.

    The phrasing deliberately does NOT copy the record text: a query that
    repeated the document verbatim would test string matching, not embedding.
    Each template restates the record in the way a person would ask for it.
    """
    templates = {
        "invoice": "invoice {ident} for customer {other} {status} {currency}",
        "customer": "customer {ident} named {name} based in {country}",
        "payment": "payment {ident} settling invoice {other} by {method}",
        "purchase_order": "purchase order {ident} raised on supplier {other} {status}",
        "expense": "expense claim {ident} for {category} on the {trip}",
    }

    queries: list[BenchmarkQuery] = []
    stride = max(1, len(corpus) // count)

    for position in range(count):
        record = corpus[(position * stride) % len(corpus)]
        lines = dict(
            line.split(": ", 1)
            for line in record.text.splitlines()
            if ": " in line
        )

        identifier = next(
            (
                value
                for key, value in lines.items()
                if key.endswith("Id") and not key.startswith("Customer Id")
            ),
            "",
        ) or next(iter(lines.values()), "")

        query = templates[record.entity_type].format(
            ident=identifier,
            other=lines.get("Customer Id")
            or lines.get("Invoice Id")
            or lines.get("Supplier Id")
            or lines.get("Employee Id")
            or "",
            status=lines.get("Status", ""),
            currency=lines.get("Currency", ""),
            name=lines.get("Name", ""),
            country=lines.get("Country", ""),
            method=lines.get("Method", ""),
            category=lines.get("Category", ""),
            trip=lines.get("Trip", ""),
        ).strip()

        queries.append(
            BenchmarkQuery(
                query=" ".join(query.split()),
                expected_representation_id=record.representation_id,
                entity_type=record.entity_type,
            )
        )

    return tuple(queries)


def write_artifact(payload: Mapping[str, Any], path: Path) -> Path:
    """Write the benchmark artifact.

    Vectors are never included: the artifact is a report, and embedding a
    500x384 matrix in it would make it unreadable and would leak the index.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    return path


__all__ = [
    "ENTITY_TEMPLATES",
    "BenchmarkRecord",
    "BenchmarkQuery",
    "build_corpus",
    "build_queries",
    "write_artifact",
]
