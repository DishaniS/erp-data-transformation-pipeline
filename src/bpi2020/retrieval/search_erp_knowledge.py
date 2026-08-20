"""
Semantic Search over BPI 2020 ERP Knowledge Base

Source:
    Qdrant collection: bpi2020_erp_knowledge

Purpose:
    Search transformed ERP cases, PDF policies, scanned invoices, receipts,
    and approval forms using natural language queries.

Example queries:
    - Find travel claim approval policy
    - Find reimbursement rules for missing receipts
    - Find domestic declaration payment request cases
    - Find scanned invoices related to travel expenses
    - Find delayed travel permit cases
"""

import sys
from pathlib import Path
from typing import List, Dict, Any

from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient


# ============================================================
# Project paths and env
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bpi2020.common.config import EmbeddingSettings, get_vector_collection
from bpi2020.qdrant_connection import QdrantSettings


# ============================================================
# Config
# ============================================================

QDRANT_COLLECTION = get_vector_collection()

EMBEDDING_MODEL = EmbeddingSettings.from_env().model_id


# ============================================================
# Search functions
# ============================================================

def load_model() -> SentenceTransformer:
    print(f"Loading embedding model: {EMBEDDING_MODEL}")
    return SentenceTransformer(EMBEDDING_MODEL)


def connect_qdrant() -> QdrantClient:
    settings = QdrantSettings.from_env()
    print(f"Connecting to Qdrant: {settings.target}")
    client = settings.create_client()
    if not client.collection_exists(QDRANT_COLLECTION):
        raise RuntimeError(
            f"Qdrant collection '{QDRANT_COLLECTION}' does not exist. "
            "Run generate_and_store_embeddings.py first."
        )
    return client


def search_erp_knowledge(
    query: str,
    model: SentenceTransformer,
    client: QdrantClient,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    Search Qdrant using semantic similarity.
    Compatible with newer qdrant-client versions.
    """
    query_vector = model.encode(
        query,
        normalize_embeddings=True
    ).tolist()

    response = client.query_points(
        collection_name=QDRANT_COLLECTION,
        query=query_vector,
        limit=limit,
        with_payload=True,
    )

    results = response.points

    formatted_results = []

    for result in results:
        payload = result.payload or {}

        formatted_results.append({
            "score": result.score,
            # Stable cross-layer key. This is what resolves the hit back to
            # PostgreSQL; source_record_id is shown for traceability only.
            "record_id": payload.get("record_id") or payload.get("unified_record_id"),
            "record_type": payload.get("record_type"),
            "title": payload.get("title"),
            "primary_reference": payload.get("primary_reference"),
            "process_type": payload.get("process_type"),
            "document_type": payload.get("document_type"),
            "source_table": payload.get("source_table"),
            "source_record_id": payload.get("source_record_id"),
            "text_for_ai": payload.get("text_for_ai"),
        })

    return formatted_results


def print_results(query: str, results: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 100)
    print(f"QUERY: {query}")
    print("=" * 100)

    if not results:
        print("No results found.")
        return

    for index, item in enumerate(results, start=1):
        print(f"\nResult {index}")
        print("-" * 100)
        print(f"Score            : {item.get('score'):.4f}")
        print(f"Record ID        : {item.get('record_id')}")
        print(f"Record Type      : {item.get('record_type')}")
        print(f"Title            : {item.get('title')}")
        print(f"Primary Reference: {item.get('primary_reference')}")
        print(f"Process Type     : {item.get('process_type')}")
        print(f"Document Type    : {item.get('document_type')}")
        print(f"Source Table     : {item.get('source_table')}")
        print(f"Source Record ID : {item.get('source_record_id')}")

        text = item.get("text_for_ai") or ""
        preview = text[:700].replace("\n", " ")

        print(f"Preview          : {preview}...")


# ============================================================
# Demo search queries
# ============================================================

def run_demo_queries():
    model = load_model()
    client = connect_qdrant()

    demo_queries = [
        "Find travel claim approval policy",
        "Find reimbursement rules for missing receipts",
        "Find scanned invoices related to travel expenses",
        "Find domestic declaration payment request cases",
        "Find approval workflow for finance declarations",
        "Find prepaid travel cost reimbursement cases",
    ]

    for query in demo_queries:
        results = search_erp_knowledge(
            query=query,
            model=model,
            client=client,
            limit=5,
        )

        print_results(query, results)


def run_interactive_search():
    model = load_model()
    client = connect_qdrant()

    print("\nERP Semantic Search is ready.")
    print("Type your query and press Enter.")
    print("Type 'exit' to stop.\n")

    while True:
        query = input("Search query: ").strip()

        if query.lower() in ["exit", "quit", "q"]:
            print("Stopping search.")
            break

        if not query:
            continue

        results = search_erp_knowledge(
            query=query,
            model=model,
            client=client,
            limit=5,
        )

        print_results(query, results)


# ============================================================
# Main
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Semantic search over the BPI 2020 ERP knowledge base."
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the built-in demo queries without prompting.",
    )
    parser.add_argument(
        "--query",
        help="Run a single query non-interactively and exit.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of results per query (default: 5).",
    )
    args = parser.parse_args()

    print("\nBPI 2020 ERP Knowledge Semantic Search")
    print("=" * 80)
    print(f"Qdrant collection: {QDRANT_COLLECTION}")

    if args.query:
        model = load_model()
        client = connect_qdrant()
        print_results(
            args.query,
            search_erp_knowledge(args.query, model, client, limit=args.limit),
        )
        return

    if args.demo:
        run_demo_queries()
        return

    mode = input("\nChoose mode: [1] demo queries  [2] interactive search: ").strip()

    if mode == "1":
        run_demo_queries()
    else:
        run_interactive_search()


if __name__ == "__main__":
    main()
