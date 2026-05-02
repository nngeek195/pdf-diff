"""
Layer 1 — Word Extraction
==========================
Uses pdfplumber.extract_words() — the same engine as the working script.

COORDINATE SYSTEM
------------------
pdfplumber uses TOP-LEFT origin (y increases DOWN) — screen coordinates.
Word fields:
    x0     — left edge
    top    — top edge
    x1     — right edge
    bottom — bottom edge
    text   — word string
    page   — 0-based page index

This means NO y-flip is needed when painting highlights.
The painter just does:  x0*scale, top*scale, x1*scale, bottom*scale

PAGE INDEXING
--------------
All page indices in this layer are 0-based (pdfplumber convention).
Layer 4 passes 0-based page_start/page_end to extract_page_range().
"""

import pdfplumber


def extract_page_range(pdf_path: str, page_start: int, page_end: int) -> list[dict]:
    """
    Extract words from pages [page_start..page_end] inclusive (0-based).

    Returns list of dicts:
        {'text': str, 'page': int, 'x0': float, 'top': float,
         'x1': float, 'bottom': float}
    """
    words = []
    with pdfplumber.open(pdf_path) as pdf:
        max_page = len(pdf.pages) - 1
        if page_start > max_page:
            return words
        end = min(page_end, max_page)
        for page_num in range(page_start, end + 1):
            page = pdf.pages[page_num]
            for w in page.extract_words():
                words.append({
                    'text':   w['text'],
                    'page':   page_num,     # 0-based
                    'x0':     w['x0'],
                    'top':    w['top'],
                    'x1':     w['x1'],
                    'bottom': w['bottom'],
                })
    return words


def get_page_count(pdf_path: str) -> int:
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python layer1_extraction.py <file.pdf>")
        sys.exit(1)
    words = extract_page_range(sys.argv[1], 0, 2)
    print(f"Extracted {len(words)} words from first 3 pages")
    for w in words[:10]:
        print(f"  {w}")