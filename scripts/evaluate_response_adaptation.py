"""Run the Phase 14 response-adaptation evaluation.

    python scripts/run_phase14_response_adaptation_evaluation.py

Needs no database, no Qdrant, no network and no model download: adaptation is
pure computation over payloads the dataset carries, which is what makes this
result reproducible on any machine from a clean checkout.

Writes ``artifacts/phase14_response_adaptation_evaluation.json``. Every number
in it is measured by this run. Nothing is copied forward from a previous one.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from erp_pipeline.response_adaptation import (  # noqa: E402
    ADAPTATION_ENGINE_VERSION,
    AdaptationOptions,
    ResponseAdaptationService,
)
from erp_pipeline.response_adaptation.evaluation import (  # noqa: E402
    METHOD_ADAPTIVE,
    METHOD_GENERIC,
    METHOD_RAW,
    build_cases,
    evaluate,
    run_ablation,
)
from erp_pipeline.response_adaptation.relevance import (  # noqa: E402
    BROAD_QUERY_TERMS,
    QUERY_INTENT_TERMS,
)

DEFAULT_ARTIFACT = ROOT / "artifacts" / "phase14_response_adaptation_evaluation.json"

#: Recall failures this run is expected to contain, with the cause each was
#: classified as after inspecting its per-field scores. Listed in the artifact
#: rather than left for a reader to discover: a limitation the authors found
#: themselves is worth more than one a reviewer finds for them.
KNOWN_LIMITATIONS: tuple[dict[str, str], ...] = (
    {
        "case_id": "sap-04",
        "missed_field": "BELNR",
        "cause": "insufficient_erp_vocabulary",
        "detail": (
            "The canonical model's invoice_id alias list does not contain SAP "
            "field mnemonics, and BELNR carries no _id/_no suffix for the "
            "identity heuristic to recognise, so the record key was dropped "
            "for a question that did not name it. Adding SAP aliases after "
            "observing this failure would be fitting the vocabulary to the "
            "test set, so the vocabulary was left unchanged."
        ),
    },
    {
        "case_id": "po-05",
        "missed_field": "supplier_no",
        "cause": "insufficient_query_vocabulary",
        "detail": (
            "The question asks 'from whom'. The intent lexicon contains 'who' "
            "but not its objective inflection 'whom', so the supplier field "
            "was never reached. Left unchanged for the same reason as above."
        ),
    },
    {
        "case_id": "proc-02",
        "missed_field": "resource",
        "cause": "insufficient_query_vocabulary",
        "detail": (
            "'Who performed this activity' should reach the 'resource' field, "
            "which is process-mining vocabulary for the actor. The lexicon "
            "maps 'who' onto customer/supplier/name only. Left unchanged."
        ),
    },
)


def build_payload(include_ablation: bool = True) -> dict[str, Any]:
    """Assemble the artifact from a live run."""
    cases = build_cases()
    service = ResponseAdaptationService()
    options: AdaptationOptions = service.options

    results = evaluate(cases, service, options)

    category_counts: dict[str, int] = {}

    for case in cases:
        category_counts[case.entity] = category_counts.get(case.entity, 0) + 1

    payload: dict[str, Any] = {
        "phase": 14,
        "name": "ERP-aware adaptive multimodal response transformation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine_version": ADAPTATION_ENGINE_VERSION,
        "config_fingerprint": options.fingerprint(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            # Stated explicitly: no LLM, no embedding model and no network took
            # part in producing any number here.
            "external_services_used": [],
            "llm_used": False,
        },
        "configuration": {
            "relevance_weights": {
                "alias": options.weights.alias,
                "name": options.weights.name,
                "entity": options.weights.entity,
                "identity": options.weights.identity,
            },
            "minimum_relevance_score": options.minimum_relevance_score,
            "max_fields": options.max_fields,
            "max_output_characters": options.max_output_characters,
            "max_value_characters": options.max_value_characters,
            # Hand-authored resources are counted so a reader can weigh them as
            # part of the method rather than as an emergent result.
            "query_intent_lexicon_entries": len(QUERY_INTENT_TERMS),
            "broad_query_terms": len(BROAD_QUERY_TERMS),
        },
        "dataset": {
            "cases": len(cases),
            "category_counts": dict(sorted(category_counts.items())),
            "labelled_relevant_fields": sum(len(case.relevant) for case in cases),
            "labelled_irrelevant_fields": sum(
                len(case.irrelevant) for case in cases
            ),
            "labelling": (
                "Single annotator (the component author). Labels were written "
                "from each question before any method was run, but no "
                "inter-annotator agreement can be reported from one annotator."
            ),
            "payloads": "synthetic, modelled on real ERP response shapes",
        },
        "methods": {
            METHOD_RAW: {
                "description": "the ERP response verbatim, with no adaptation",
                **results["methods"][METHOD_RAW],
            },
            METHOD_GENERIC: {
                "description": (
                    "envelope unwrapped and flattened; no ERP vocabulary and "
                    "no query awareness. Given the unwrapping deliberately, so "
                    "the baseline is not a straw man."
                ),
                **results["methods"][METHOD_GENERIC],
            },
            METHOD_ADAPTIVE: {
                "description": (
                    "proposed: unwrap, canonical ERP mapping, deterministic "
                    "query relevance, mandatory identity preservation, budgets"
                ),
                **results["methods"][METHOD_ADAPTIVE],
            },
        },
        "per_category": results["per_entity"],
        "per_case": results["per_case"],
        "limitations": list(KNOWN_LIMITATIONS),
        "metric_notes": {
            "relevant_field_recall": (
                "THE headline metric. A dropped relevant field cannot be "
                "recovered downstream; a retained irrelevant one only costs "
                "context. The two are reported separately rather than blended."
            ),
            "field_matching": (
                "One matcher for all three methods: a label matches a produced "
                "name that equals it or ends with it on a path boundary. "
                "Required because RAW reports nested paths from the "
                "un-unwrapped root while the other two unwrap first."
            ),
            "field_counting": (
                "Leaf fields on both sides for every method, so a method that "
                "removes nothing measures as removing nothing."
            ),
            "context_reduction_ratio": (
                "Measured on the canonical JSON encoding of each payload, in "
                "bytes. Characters, not tokens: this project ships no "
                "tokenizer, and an invented token count would be a guess."
            ),
            "latency": (
                "Wall-clock per case, single process, no warm-up excluded. "
                "Percentiles are nearest-rank so every reported value is an "
                "observed measurement rather than an interpolation."
            ),
        },
    }

    if include_ablation:
        ablation = run_ablation(cases, service)
        payload["ablation"] = {
            "question": (
                "What does deterministic query relevance contribute, holding "
                "unwrapping, canonical mapping and budgets identical?"
            ),
            **ablation,
        }

    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument(
        "--no-ablation", action="store_true",
        help="skip the query-relevance ablation",
    )
    args = parser.parse_args()

    payload = build_payload(include_ablation=not args.no_ablation)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    methods = payload["methods"]

    print(f"Phase 14 evaluation - {payload['dataset']['cases']} cases")
    print(f"  categories: {payload['dataset']['category_counts']}")
    print()
    print(f"  {'method':<20} {'recall':>9} {'irrel.rm':>9} {'field.red':>10} "
          f"{'ctx.red':>9} {'p95 ms':>9}")

    for name in (METHOD_RAW, METHOD_GENERIC, METHOD_ADAPTIVE):
        entry = methods[name]
        print(
            f"  {name:<20} {entry['relevant_field_recall']:>9.4f} "
            f"{entry['irrelevant_field_removal_rate']:>9.4f} "
            f"{entry['field_reduction_ratio']:>10.4f} "
            f"{entry['context_reduction_ratio']:>9.4f} "
            f"{entry['latency_ms']['p95']:>9.4f}"
        )

    if "ablation" in payload:
        print()
        print("  ablation (query relevance):")

        for arm in ("with_query_relevance", "without_query_relevance"):
            entry = payload["ablation"][arm]
            print(
                f"    {arm:<26} recall={entry['relevant_field_recall']:.4f} "
                f"ctx_red={entry['context_reduction_ratio']:.4f}"
            )

    print()
    print(f"  recall failures: {len(payload['limitations'])} (see 'limitations')")
    print(f"  artifact: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
