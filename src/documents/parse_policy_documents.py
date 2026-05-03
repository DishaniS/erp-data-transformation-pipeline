from pathlib import Path
import json
import fitz  # PyMuPDF


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOC_DIR = PROJECT_ROOT / "data" / "documents"
OUTPUT_DIR = PROJECT_ROOT / "data" / "ai_ready"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def extract_text_from_pdf(pdf_path):
    doc = fitz.open(pdf_path)
    full_text = ""

    for page in doc:
        full_text += page.get_text()

    return full_text


def split_into_sections(text):
    lines = text.split("\n")

    sections = []
    current_section = {
        "title": "Introduction",
        "content": ""
    }

    for line in lines:
        line = line.strip()

        if not line:
            continue

        # detect section headers (simple rule)
        if line[0].isdigit() or line.isupper():
            if current_section["content"]:
                sections.append(current_section)

            current_section = {
                "title": line,
                "content": ""
            }
        else:
            current_section["content"] += " " + line

    if current_section["content"]:
        sections.append(current_section)

    return sections


def create_ai_ready_records(file_name, sections):
    records = []

    for i, sec in enumerate(sections):
        text = sec["content"].strip()

        if not text:
            continue

        record = {
            "record_id": f"{file_name}_section_{i}",
            "record_type": "policy_document",
            "document_name": file_name,
            "section_title": sec["title"],
            "text": text,
            "text_for_ai": f"{sec['title']}: {text}",
            "metadata": {
                "source": "policy_document",
                "processing_type": "one_time_pdf_extraction"
            }
        }

        records.append(record)

    return records


def main():
    all_records = []

    for pdf_file in DOC_DIR.glob("*.pdf"):
        print(f"Processing: {pdf_file.name}")

        text = extract_text_from_pdf(pdf_file)
        sections = split_into_sections(text)

        records = create_ai_ready_records(pdf_file.stem, sections)
        all_records.extend(records)

    output_json = OUTPUT_DIR / "policy_documents.json"
    output_jsonl = OUTPUT_DIR / "policy_documents.jsonl"

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(all_records, f, indent=2, ensure_ascii=False)

    with open(output_jsonl, "w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nCreated {len(all_records)} policy document records")
    print(f"Saved: {output_json}")
    print(f"Saved: {output_jsonl}")


if __name__ == "__main__":
    main()