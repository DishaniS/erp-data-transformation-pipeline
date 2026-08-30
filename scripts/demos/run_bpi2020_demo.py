"""Demonstrate the generic pipeline on the BPI Challenge 2020 event log.

WHAT THIS SCRIPT IS FOR
-----------------------
BPI Challenge 2020 is the dataset that originally motivated this research. It
is no longer a parallel implementation - it is one dataset, and this script
exists to prove a single claim:

    the supposedly generic framework can actually process the dataset that
    motivated the prototype, using nothing but ``erp_pipeline``.

WHAT IT DOES NOT DO
-------------------
It implements no ETL, no case building, no embedding, no vector storage, no
identity scheme and no retrieval of its own. Every step below is a call into
``erp_pipeline``. If this script ever grows domain logic, the generalization
has failed and the logic belongs in the framework instead.

The only BPI-specific knowledge involved lives in
``examples/bpi2020/event_log_config.json`` - column names and process names,
as data.

USAGE
-----
    python scripts/demos/run_bpi2020_demo.py
    python scripts/demos/run_bpi2020_demo.py --limit 5000
    python scripts/demos/run_bpi2020_demo.py --embed          # loads MiniLM
    python scripts/demos/run_bpi2020_demo.py --embed --store  # + vector store
    python scripts/demos/run_bpi2020_demo.py --json out.json

The dataset itself is not redistributed with this repository. Place the BPI
CSVs under ``data/bpi2020/raw/`` first; the script reports what is missing and
exits cleanly rather than failing obscurely.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from erp_pipeline.ingestion import FileIngestionService  # noqa: E402
from erp_pipeline.ingestion.document_classification import (  # noqa: E402
    DEFAULT_RULES,
    ClassificationConfig,
    ClassificationRule,
    classify_extracted_document,
)
from erp_pipeline.process import (  # noqa: E402
    EventLogConfig,
    ProcessCaseService,
    build_process_model,
)
from erp_pipeline.schemas.enums import SourceType  # noqa: E402
from erp_pipeline.verification import (  # noqa: E402
    IntegrityVerificationService,
    check_records,
)

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "examples" / "bpi2020" / "event_log_config.json"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "bpi2020" / "raw"
DEFAULT_DOCUMENT_DIRS = (
    PROJECT_ROOT / "data" / "bpi2020" / "documents",
    PROJECT_ROOT / "data" / "bpi2020" / "images",
)


# ======================================================================
# Configuration
# ======================================================================


def load_demo_config(path: Path) -> dict[str, Any]:
    """Read the dataset configuration. The only BPI knowledge in this demo."""
    if not path.exists():
        raise SystemExit(
            f"dataset configuration not found: {path}\n"
            "This file describes where BPI's case id, activity and timestamp "
            "columns live. It is configuration, not data, and ships with the "
            "repository."
        )

    return json.loads(path.read_text(encoding="utf-8"))


def event_log_config_for(
    demo_config: Mapping[str, Any], process_type: str
) -> EventLogConfig:
    """Build the generic ``EventLogConfig`` for one BPI file."""
    block = demo_config["event_log"]

    return EventLogConfig(
        case_id_field=block["case_id_field"],
        activity_field=block["activity_field"],
        timestamp_field=block.get("timestamp_field"),
        event_key_field=block.get("event_key_field"),
        process_type=process_type,
        excluded_fields=frozenset(block.get("excluded_fields", ())),
        entity_reference_fields=dict(block.get("entity_reference_fields", {})),
    )


def classification_config_for(demo_config: Mapping[str, Any]) -> ClassificationConfig:
    """Extend the framework's generic vocabulary with this dataset's terms."""
    block = demo_config.get("document_classification", {})
    extra = tuple(
        ClassificationRule(
            document_type=rule["document_type"],
            keywords=tuple(rule["keywords"]),
            negative_keywords=tuple(rule.get("negative_keywords", ())),
            weight=float(rule.get("weight", 1.0)),
        )
        for rule in block.get("extra_rules", ())
    )

    return ClassificationConfig(rules=DEFAULT_RULES + extra)


# ======================================================================
# Stage 1 - generic file ingestion
# ======================================================================


def iter_event_rows(
    path: Path, limit: int | None
) -> Iterator[Mapping[str, Any]]:
    """Stream one CSV through GENERIC ingestion, not a bespoke reader."""
    result = FileIngestionService().ingest(path)

    for index, row in enumerate(result.iter_records()):
        if limit is not None and index >= limit:
            break

        yield dict(row.values)


# ======================================================================
# Stage 2 - generic process/case building
# ======================================================================


def build_cases_for_file(
    path: Path,
    process_type: str,
    demo_config: Mapping[str, Any],
    limit: int | None,
) -> tuple[Any, ...]:
    """Turn one event-log file into cases using the generic process layer."""
    service = ProcessCaseService(
        source_system_id=demo_config["source_system_id"],
        config=event_log_config_for(demo_config, process_type),
        source_type=SourceType.CSV,
    )

    rows = iter_event_rows(path, limit)

    # ``skip_invalid`` because a public research export legitimately contains
    # rows with no case id; refusing the whole file over them would make the
    # demo useless while proving nothing.
    return service.build_cases(rows, skip_invalid=True), service


# ======================================================================
# Stage 3 - documents through generic ingestion + classification
# ======================================================================


def classify_documents(
    demo_config: Mapping[str, Any], directories: Sequence[Path]
) -> list[dict[str, Any]]:
    """Classify the dataset's PDFs and scans with the generic classifier."""
    config = classification_config_for(demo_config)
    ingestion = FileIngestionService()
    classified: list[dict[str, Any]] = []

    for directory in directories:
        if not directory.exists():
            continue

        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue

            try:
                result = ingestion.ingest(path)
            except Exception as error:  # noqa: BLE001 - a demo must not die here
                classified.append(
                    {"file": path.name, "error": type(error).__name__}
                )
                continue

            outcome = classify_extracted_document(result, config=config)
            classified.append(
                {
                    "file": path.name,
                    "pages": getattr(result, "page_count", None),
                    **outcome.to_dict(),
                }
            )

    return classified


# ======================================================================
# Stage 4 - AI representations, embedding, storage, retrieval
# ======================================================================


def embed(representations: Sequence[Any]) -> dict[str, Any]:
    """Run the GENERIC embedding path over the built cases.

    Local model only - no external service, so this half of the demonstration
    always runs.
    """
    from erp_pipeline.ai import EmbeddingService, SentenceTransformerModel

    summary = EmbeddingService(SentenceTransformerModel()).embed_many(
        representations
    )

    return {
        "model_id": summary.model_id,
        "dimension": summary.dimension,
        "representations_read": summary.representations_read,
        "embeddings_generated": summary.embeddings_generated,
        "embeddings_skipped": summary.embeddings_skipped,
        "embeddings_failed": summary.embeddings_failed,
        "duration_seconds": summary.duration_seconds,
        "counters_balance": summary.counters_balance,
        "_records": summary.records,
    }


def demonstrate_routing(embeddings: Sequence[Any]) -> dict[str, Any]:
    """Show which tier the storage policy would choose for each case.

    Routing is pure computation over the record's own metadata, so this
    demonstrates the tiering research WITHOUT needing a vector database, a
    Qdrant collection or a cold-archive key. Nothing is written anywhere.
    """
    from erp_pipeline.storage import StoragePolicyRouter
    from erp_pipeline.storage.models import StorageRoutingContext
    from erp_pipeline.storage.service import DEFAULT_PROFILE

    router = StoragePolicyRouter()
    distribution: dict[str, int] = {}
    reasons: dict[str, int] = {}
    example: dict[str, Any] | None = None

    for record in embeddings:
        if not getattr(record, "vector", None):
            continue

        context = StorageRoutingContext(
            representation_id=record.representation_id,
            sensitivity=DEFAULT_PROFILE.sensitivity,
            business_criticality=DEFAULT_PROFILE.business_criticality,
            latency_requirement=DEFAULT_PROFILE.latency_requirement,
        )
        decision = router.route(context)

        tier = decision.selected_tier.value
        distribution[tier] = distribution.get(tier, 0) + 1
        reasons[decision.reason_code.value] = (
            reasons.get(decision.reason_code.value, 0) + 1
        )

        if example is None:
            example = {
                "representation_id": decision.representation_id,
                "selected_tier": tier,
                "reason": decision.reason,
                "policy": f"{decision.policy_id}@{decision.policy_version}",
                "scores": {
                    score.tier.value: score.total for score in decision.scores
                },
            }

    return {
        "mode": "policy decision only - nothing was written",
        "distribution": distribution,
        "reason_codes": reasons,
        "example_decision": example,
    }


def store_vectors(embeddings: Sequence[Any]) -> dict[str, Any]:
    """Physically store vectors through the GENERIC storage service.

    Uses whatever the runtime configuration already points at, and refuses to
    invent infrastructure: if no vector store is reachable the demo reports
    that plainly rather than creating collections a research machine did not
    ask for.
    """
    from erp_pipeline.runtime.services import build_storage_service
    from erp_pipeline.runtime.settings import RuntimeSettings

    settings = RuntimeSettings.from_environment()

    try:
        storage = build_storage_service(settings, engine=None)
    except Exception as error:  # noqa: BLE001 - reported, never fatal
        return {
            "status": "unavailable",
            "detail": f"{type(error).__name__}: {error}",
        }

    if storage is None or not storage.tiers.available():
        return {
            "status": "unavailable",
            "detail": (
                "no storage tier is configured. Set ERP_QDRANT_* (and "
                "ERP_COLD_ARCHIVE_KEY when the cold tier is enabled) to run "
                "this part of the demonstration."
            ),
        }

    placed: dict[str, int] = {}

    for record in embeddings:
        if not getattr(record, "vector", None):
            continue

        metadata, _decision = storage.store(record)
        tier = metadata.current_tier.value
        placed[tier] = placed.get(tier, 0) + 1

    return {
        "status": "stored",
        "vectors_stored": sum(placed.values()),
        "tier_distribution": placed,
        "_storage": storage,
    }


def demonstrate_retrieval(
    storage: Any, query: str, top_k: int = 5
) -> list[dict[str, Any]]:
    """Retrieve through the GENERIC hybrid store, not a bespoke search."""
    from erp_pipeline.ai import SentenceTransformerModel

    vector = SentenceTransformerModel().encode([query])[0]
    result = storage.search(vector, limit=top_k)

    return [
        {
            "representation_id": hit.representation_id,
            "score": round(float(hit.score), 6),
            "tier": hit.tier.value,
        }
        for hit in result.hits
    ]


# ======================================================================
# Orchestration
# ======================================================================


def run_demo(args: argparse.Namespace) -> dict[str, Any]:
    demo_config = load_demo_config(Path(args.config))
    data_dir = Path(args.data_dir)

    report: dict[str, Any] = {
        "dataset": "BPI Challenge 2020",
        "source_system_id": demo_config["source_system_id"],
        "framework_package": "erp_pipeline",
        "files": [],
        "totals": {},
    }

    all_cases: list[Any] = []
    all_representations: list[Any] = []
    service = None

    for entry in demo_config["files"]:
        path = data_dir / entry["filename"]

        if not path.exists():
            report["files"].append(
                {"file": entry["filename"], "status": "not_present"}
            )
            continue

        cases, service = build_cases_for_file(
            path, entry["process_type"], demo_config, args.limit
        )
        representations = service.to_representations(cases)

        all_cases.extend(cases)
        all_representations.extend(representations)

        model = build_process_model(cases, entry["process_type"]) if cases else None

        report["files"].append(
            {
                "file": entry["filename"],
                "status": "processed",
                "process_type": entry["process_type"],
                "cases_built": len(cases),
                "events_total": sum(case.total_events for case in cases),
                "distinct_activities": len(model.activities) if model else 0,
                "start_activities": list(model.start_activities) if model else [],
                "end_activities": list(model.end_activities) if model else [],
                "example_case": cases[0].to_dict(include_events=False) if cases else None,
            }
        )

    if not all_cases:
        report["totals"] = {"cases": 0}
        report["note"] = (
            f"No BPI CSV files were found under {data_dir}. The dataset is not "
            "redistributed with this repository; download it and place the "
            "CSVs there to run the full demonstration."
        )
        return report

    # -- identity integrity over everything built, via the generic verifier --
    identity_issues = check_records(
        [case.case_record_id for case in all_cases]
    )

    report["totals"] = {
        "cases": len(all_cases),
        "events": sum(case.total_events for case in all_cases),
        "representations": len(all_representations),
        "identity_issues": len(identity_issues),
    }
    report["identity_issues"] = [issue.to_dict() for issue in identity_issues[:20]]

    if not args.skip_documents:
        report["documents"] = classify_documents(demo_config, DEFAULT_DOCUMENT_DIRS)

    if args.embed:
        outcome = embed(all_representations)
        embeddings = outcome.pop("_records", ())
        report["embedding"] = outcome

        # Tier routing is pure computation, so the storage research is
        # demonstrated whether or not a vector database is available.
        report["routing"] = demonstrate_routing(embeddings)

        if args.store:
            stored = store_vectors(embeddings)
            storage = stored.pop("_storage", None)
            report["storage"] = stored

            if storage is not None:
                report["retrieval"] = {
                    "query": args.query,
                    "hits": demonstrate_retrieval(storage, args.query),
                }

                verifier = IntegrityVerificationService(
                    tier_state=storage.state,
                    expected_model_id=outcome.get("model_id"),
                    expected_dimension=outcome.get("dimension"),
                )
                verification = verifier.verify_storage().merged(
                    verifier.verify_embeddings(
                        [
                            record
                            for record in embeddings
                            if getattr(record, "vector", None)
                        ]
                    )
                )

                report["verification"] = verification.to_dict()

    return report


def render(report: Mapping[str, Any]) -> None:
    print()
    print("=" * 72)
    print("BPI Challenge 2020 demonstration - running on erp_pipeline only")
    print("=" * 72)
    print(f"source system : {report['source_system_id']}")

    for entry in report["files"]:
        if entry["status"] != "processed":
            print(f"  {entry['file']:<32} not present")
            continue

        print(
            f"  {entry['file']:<32} {entry['cases_built']:>7} cases  "
            f"{entry['events_total']:>8} events  "
            f"{entry['distinct_activities']:>3} activities"
        )

    totals = report.get("totals", {})

    if not totals.get("cases"):
        print()
        print(report.get("note", "nothing to process"))
        return

    print()
    print(f"cases              : {totals['cases']}")
    print(f"events             : {totals['events']}")
    print(f"representations    : {totals['representations']}")
    print(f"identity issues    : {totals['identity_issues']}")

    documents = report.get("documents")

    if documents:
        print()
        print("documents classified by the generic classifier:")
        for item in documents:
            if "error" in item:
                print(f"  {item['file']:<40} ERROR {item['error']}")
            else:
                print(
                    f"  {item['file']:<40} {item['document_type']:<24} "
                    f"conf={item['confidence']:.2f}"
                )

    embedding = report.get("embedding")

    if embedding:
        print()
        print(f"embedding model    : {embedding['model_id']} "
              f"(dim {embedding['dimension']})")
        print(f"vectors generated  : {embedding['embeddings_generated']}")

    routing = report.get("routing")

    if routing:
        print()
        print(f"tier routing       : {routing['distribution']} "
              f"({routing['mode']})")
        print(f"reason codes       : {routing['reason_codes']}")

        example = routing.get("example_decision")

        if example:
            print(f"example decision   : {example['selected_tier']} - "
                  f"{example['reason']}")

    storage = report.get("storage")

    if storage:
        print()
        if storage["status"] == "stored":
            print(f"vectors stored     : {storage['vectors_stored']} "
                  f"{storage['tier_distribution']}")
        else:
            print(f"vector storage     : unavailable - {storage['detail']}")

    retrieval = report.get("retrieval")

    if retrieval:
        print()
        print(f"retrieval query    : {retrieval['query']!r}")
        for hit in retrieval["hits"]:
            print(f"  {hit['score']:.4f}  [{hit['tier']}]  {hit['representation_id']}")

    verification = report.get("verification")

    if verification:
        print()
        print(
            f"cross-store verify : "
            f"{'PASS' if verification['passed'] else 'FAIL'} "
            f"({verification['checks_run']} checks, "
            f"{verification['failure_count']} failures)"
        )

    print()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Demonstrate the generic ERP pipeline on the BPI Challenge 2020 "
            "event log. Implements no pipeline logic of its own."
        )
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument(
        "--limit",
        type=int,
        default=20000,
        help=(
            "Maximum event rows read per file (default: 20000). Use 0 for the "
            "whole file - the full log is ~270,000 events."
        ),
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Also generate embeddings. Loads the local MiniLM model.",
    )
    parser.add_argument(
        "--store",
        action="store_true",
        help="Also route vectors through hybrid tiered storage (implies --embed).",
    )
    parser.add_argument(
        "--skip-documents",
        action="store_true",
        help="Skip PDF/image ingestion and classification.",
    )
    parser.add_argument(
        "--query",
        default="declaration rejected by the budget owner",
        help="Retrieval query used when --store is set.",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="Write the full machine-readable report to this path.",
    )

    args = parser.parse_args(argv)

    if args.store:
        args.embed = True

    if args.limit == 0:
        args.limit = None

    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_demo(args)

    render(report)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        print(f"report written to {args.json_path}")

    totals = report.get("totals", {})

    if not totals.get("cases"):
        return 0

    # A demonstration that produced identity issues has failed to demonstrate
    # anything good, so it exits non-zero.
    return 1 if totals.get("identity_issues") else 0


if __name__ == "__main__":
    raise SystemExit(main())
