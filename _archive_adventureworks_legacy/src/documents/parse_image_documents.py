from pathlib import Path
import json
import pytesseract
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGE_DIR = PROJECT_ROOT / "data" / "images"
OUTPUT_DIR = PROJECT_ROOT / "data" / "ai_ready"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# If Tesseract is not found automatically, uncomment this:
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def extract_text_from_image(image_path: Path) -> str:
    image = Image.open(image_path)
    text = pytesseract.image_to_string(image)
    return text.strip()


def create_record(image_path: Path, extracted_text: str) -> dict:
    return {
        "record_id": f"{image_path.stem}_ocr_record",
        "record_type": "image_document",
        "document_name": image_path.name,
        "text": extracted_text,
        "text_for_ai": f"Image document {image_path.name} contains the following extracted text: {extracted_text}",
        "metadata": {
            "source": "image_document",
            "processing_type": "one_time_ocr_extraction"
        }
    }


def main():
    records = []

    for image_file in IMAGE_DIR.glob("*"):
        if image_file.suffix.lower() not in [".png", ".jpg", ".jpeg"]:
            continue

        print(f"Processing image: {image_file.name}")
        text = extract_text_from_image(image_file)

        if not text:
            print(f"Warning: No text extracted from {image_file.name}")
            continue

        records.append(create_record(image_file, text))

    output_json = OUTPUT_DIR / "image_documents.json"
    output_jsonl = OUTPUT_DIR / "image_documents.jsonl"

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    with open(output_jsonl, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"\nCreated {len(records)} image OCR records")
    print(f"Saved: {output_json}")
    print(f"Saved: {output_jsonl}")


if __name__ == "__main__":
    main()