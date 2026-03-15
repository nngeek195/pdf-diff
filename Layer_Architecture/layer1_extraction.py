"""
Layer 1 — Data Extraction
==========================
Scans a PDF with pdfplumber and builds a "Word Database":
a list of WordObject instances, each pinned to its exact physical
location on the original page via a BoundingBox.

WordObject fields:
    index        : int   — global zero-based position across all pages
    text         : str   — the word string (stripped)
    page_number  : int   — 1-based page number
    bbox         : BoundingBox(x0, y0, x1, y1) in pdfplumber coords
                          (origin = top-left, y increases downward)

Two extraction modes:
    extract_word_database()  — full PDF at once (small docs / testing)
    extract_page_range()     — one page range at a time (chunked pipeline)
"""

from dataclasses import dataclass
from typing import NamedTuple
import pdfplumber


class BoundingBox(NamedTuple):
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(unsafe_hash=True)
class WordObject:
    index:       int
    text:        str
    page_number: int
    bbox:        BoundingBox

    def __repr__(self):
        return (f"WordObject(idx={self.index}, text={self.text!r}, "
                f"page={self.page_number}, bbox={self.bbox})")


# ── Internal: extract one page ───────────────────────────────────────────────

def _words_from_page(page, page_num: int, start_index: int) -> list[WordObject]:
    """
    Extract all WordObjects from a single open pdfplumber page.
    start_index = global index for the first word on this page.
    """
    raw_words = page.extract_words(
        x_tolerance=3,
        y_tolerance=3,
        keep_blank_chars=False,
        use_text_flow=True,
    )
    result = []
    idx = start_index
    for wd in raw_words:
        text = wd["text"].strip()
        if not text:
            continue
        bbox = BoundingBox(
            x0=float(wd["x0"]),
            y0=float(wd["top"]),      # pdfplumber: distance from top of page
            x1=float(wd["x1"]),
            y1=float(wd["bottom"]),
        )
        result.append(WordObject(index=idx, text=text, page_number=page_num, bbox=bbox))
        idx += 1
    return result


# ── Full extraction (small docs / testing) ───────────────────────────────────

def extract_word_database(pdf_path: str) -> list[WordObject]:
    """Extract every word from the whole PDF, returning a flat list."""
    database: list[WordObject] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            database.extend(_words_from_page(page, page_num, len(database)))
    return database


# ── Chunked extraction (progressive pipeline) ────────────────────────────────

def extract_page_range(
    pdf_path: str,
    page_start: int,
    page_end: int,
    global_index_offset: int = 0,
) -> list[WordObject]:
    """
    Extract WordObjects for pages [page_start … page_end] only (1-based, inclusive).

    global_index_offset is added to every word's .index so indices stay
    globally unique even though we only open part of the document.

    Parameters
    ----------
    pdf_path             : path to the PDF
    page_start           : first page (1-based)
    page_end             : last  page (1-based, inclusive)
    global_index_offset  : value added to all word indices

    Returns
    -------
    list[WordObject]
    """
    result: list[WordObject] = []
    with pdfplumber.open(pdf_path) as pdf:
        end = min(page_end, len(pdf.pages))
        for page_num in range(page_start, end + 1):
            page  = pdf.pages[page_num - 1]
            words = _words_from_page(page, page_num, global_index_offset + len(result))
            result.extend(words)
    return result


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_page_count(pdf_path: str) -> int:
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


def get_page_dimensions(pdf_path: str) -> dict[int, tuple[float, float]]:
    """Return {page_number: (width, height)} for every page."""
    dims = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            dims[page_num] = (float(page.width), float(page.height))
    return dims


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Usage: python layer1_extraction.py <file.pdf>")
        sys.exit(1)
    db = extract_word_database(path)
    print(f"Extracted {len(db)} words | {get_page_count(path)} pages")
    for w in db[:5]:
        print(f"  {w}")