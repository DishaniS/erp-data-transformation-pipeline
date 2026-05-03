from pathlib import Path
import json


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AI_READY_DIR = PROJECT_ROOT / "data" / "ai_ready"

OUTPUT_JSON = AI_READY_DIR / "unified_ai_knowledge_base.json"
OUTPUT_JSONL = AI_READY_DIR / "unified_ai_knowledge_base.jsonl"


INPUT_FILES = [
    "erp_sales_records.json",
    "policy_documents.json",
    "image_documents.json",
]


def load_records(file_name):
    path = AI_READY_DIR / file_name

    if not path.exists():
        print(f"Warning: {file_name} not found. Skipping.")
        return []

    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)

    print(f"Loaded {len(records)} records from {file_name}")
    return records


def normalize_record(record, source_file):
    return {
        "knowledge_id": record.get("record_id"),
        "record_type": record.get("record_type"),
        "text_for_ai": record.get("text_for_ai") or record.get("text") or "",
        "source_file": source_file,
        "original_record": record,
        "metadata": {
            **record.get("metadata", {}),
            "unified_layer": True,
            "ai_ready_format": "json_text_embedding_ready"
        }
    }


def main():
    unified_records = []

    for file_name in INPUT_FILES:
        records = load_records(file_name)

        for record in records:
            unified_records.append(normalize_record(record, file_name))

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(unified_records, f, indent=2, ensure_ascii=False, default=str)

    with open(OUTPUT_JSONL, "w", encoding="utf-8") as f:
        for record in unified_records:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    print("\nUnified AI Knowledge Base created successfully.")
    print(f"Total records: {len(unified_records)}")
    print(f"Saved JSON: {OUTPUT_JSON}")
    print(f"Saved JSONL: {OUTPUT_JSONL}")


if __name__ == "__main__":
    main()