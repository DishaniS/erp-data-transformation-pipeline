"""Create an explicitly synthetic OCR input for the frontend acceptance run."""
import sys
from pathlib import Path
import pymupdf

output = Path(sys.argv[1])
output.parent.mkdir(parents=True, exist_ok=True)
document = pymupdf.open()
page = document.new_page(width=720, height=420)
lines = [
    "SYNTHETIC DOCUMENT - LOCAL TEST ONLY",
    "Person: Lakshan QA Sample",
    "Employee ID: EMP-QA-1042",
    "Document type: Test certificate",
    "Certificate reference: QA-7382-ORCHID",
    "Issue date: 14 October 2026",
    "Verification phrase: silver orchid bridge",
]
for index, line in enumerate(lines):
    page.insert_text((36, 54 + index * 45), line, fontsize=23)
output.write_bytes(page.get_pixmap(dpi=160).tobytes("png"))
document.close()
print(f"Synthetic OCR image created: {output.name}")
