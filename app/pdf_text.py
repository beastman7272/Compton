from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError as exc:
    raise ImportError(
        "Missing dependency: PyMuPDF. Install it with: pip install pymupdf"
    ) from exc


@dataclass(slots=True)
class PdfPageText:
    upload_id: int | None
    pdf_path: Path
    page_number: int
    text: str


@dataclass(slots=True)
class PdfTextResult:
    pdf_path: Path
    page_count: int
    pages: list[PdfPageText]


def extract_pdf_text(
    pdf_path: Path | str,
    upload_id: int | None = None,
    max_pages: int | None = None,
) -> PdfTextResult:
    """
    Extract text from a PDF page-by-page.

    Page numbers are stored as 1-based numbers because that is what users
    expect to see in the app.
    """
    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"File is not a PDF: {pdf_path}")

    pages: list[PdfPageText] = []

    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
        limit = min(page_count, max_pages) if max_pages else page_count

        for page_index in range(limit):
            page = doc.load_page(page_index)
            text = page.get_text("text") or ""

            pages.append(
                PdfPageText(
                    upload_id=upload_id,
                    pdf_path=pdf_path,
                    page_number=page_index + 1,
                    text=clean_extracted_text(text),
                )
            )

    return PdfTextResult(
        pdf_path=pdf_path,
        page_count=page_count,
        pages=pages,
    )


def get_pdf_page_count(pdf_path: Path | str) -> int:
    """
    Return the number of pages in a PDF.
    """
    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    with fitz.open(pdf_path) as doc:
        return doc.page_count


def extract_first_pages_text(
    pdf_path: Path | str,
    upload_id: int | None = None,
    max_pages: int = 10,
) -> PdfTextResult:
    """
    Extract text from the first N pages.

    This is intended for Architect / GC / contact extraction.
    """
    return extract_pdf_text(
        pdf_path=pdf_path,
        upload_id=upload_id,
        max_pages=max_pages,
    )


def clean_extracted_text(text: str) -> str:
    """
    Light cleanup only.

    Do not over-normalize here because search-result context should remain
    readable to the user.
    """
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = [line.strip() for line in text.split("\n")]
    cleaned_lines: list[str] = []

    previous_blank = False

    for line in lines:
        if not line:
            if not previous_blank:
                cleaned_lines.append("")
            previous_blank = True
            continue

        cleaned_lines.append(line)
        previous_blank = False

    return "\n".join(cleaned_lines).strip()


def page_has_meaningful_text(text: str, min_chars: int = 40) -> bool:
    """
    Basic check to identify pages where text extraction produced something useful.

    If this returns False for many pages, the PDF may be scanned/image-based
    and may need OCR later.
    """
    compact = "".join(ch for ch in text if ch.isalnum())
    return len(compact) >= min_chars


def pdf_needs_ocr(
    pdf_path: Path | str,
    sample_pages: int = 5,
    min_text_pages: int = 1,
) -> bool:
    """
    Heuristic OCR detector.

    Returns True when the sampled pages produce little/no extractable text.
    V1 does not perform OCR, but this gives us a clean way to flag the issue.
    """
    result = extract_pdf_text(
        pdf_path=pdf_path,
        max_pages=sample_pages,
    )

    meaningful_pages = sum(
        1 for page in result.pages
        if page_has_meaningful_text(page.text)
    )

    return meaningful_pages < min_text_pages