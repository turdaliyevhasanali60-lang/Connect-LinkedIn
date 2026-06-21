import fitz  # PyMuPDF


def extract_pdf_text(pdf_path: str) -> str:
    """Extract raw text from a LinkedIn 'Save to PDF' export. LinkedIn's export is
    clean, selectable text (no OCR needed) — this runs in well under 100ms."""
    doc = fitz.open(pdf_path)
    try:
        text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()
    return text.strip()


def guess_name(pdf_text: str) -> str:
    """LinkedIn's PDF export puts the person's name as the first non-empty line.
    Used as a fallback if the model doesn't extract a name."""
    for line in pdf_text.splitlines():
        line = line.strip()
        if line:
            return line
    return "there"
