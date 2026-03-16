"""
Layer 3 — Page Renderer / Painter
====================================
Uses pypdfium2 for rendering + PIL for highlight overlay.

COORDINATE SYSTEM
------------------
pdfplumber gives x0/top/x1/bottom in TOP-LEFT origin (y increases DOWN).
pypdfium2 renders to PIL images also with TOP-LEFT origin.

So the paint formula is simply:
    px0 = word['x0']     * scale - 2
    py0 = word['top']    * scale - 2
    px1 = word['x1']     * scale + 2
    py1 = word['bottom'] * scale + 2

NO y-flip needed. This is why the working script's highlights land
perfectly — it uses the same coordinate system end-to-end.

HIGHLIGHT COLORS
-----------------
REMOVED = (255, 213,  79, 115)  — yellow  rgba(255,213,79,0.45)
ADDED   = ( 79, 195, 247, 115)  — blue    rgba(79,195,247,0.45)
"""

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

RENDER_SCALE   = 1.5
REMOVED_COLOR  = (255, 213,  79, 115)
ADDED_COLOR    = ( 79, 195, 247, 115)


def render_page(doc: pdfium.PdfDocument,
                page_idx: int,
                highlights: list[dict],
                color: tuple) -> Image.Image:
    """
    Render one page from an open PdfDocument and paint word highlights.

    Args:
        doc        — open pdfium.PdfDocument (0-based indexing)
        page_idx   — 0-based page index
        highlights — list of word dicts with x0/top/x1/bottom keys
        color      — RGBA tuple for the highlight fill

    Returns:
        PIL Image (RGB) with highlights composited on top.
    """
    page   = doc[page_idx]
    bitmap = page.render(scale=RENDER_SCALE)
    img    = bitmap.to_pil().convert("RGBA")

    if highlights:
        overlay = Image.new('RGBA', img.size, (255, 255, 255, 0))
        draw    = ImageDraw.Draw(overlay)
        for w in highlights:
            # Direct coord mapping — no y-flip (pdfplumber = top-left origin)
            x0 = w['x0']     * RENDER_SCALE - 2
            y0 = w['top']    * RENDER_SCALE - 2
            x1 = w['x1']     * RENDER_SCALE + 2
            y1 = w['bottom'] * RENDER_SCALE + 2
            draw.rectangle([x0, y0, x1, y1], fill=color)
        img = Image.alpha_composite(img, overlay)

    page.close()
    return img.convert("RGB")


def render_page_range(doc: pdfium.PdfDocument,
                      page_start: int,
                      page_end: int,
                      highlights: list[dict],
                      color: tuple) -> list[tuple[int, Image.Image]]:
    """
    Render pages [page_start..page_end] inclusive (0-based).
    Returns list of (page_idx, PIL.Image).
    """
    total = len(doc)
    result = []
    for p in range(page_start, min(page_end + 1, total)):
        page_highlights = [w for w in highlights if w['page'] == p]
        img = render_page(doc, p, page_highlights, color)
        result.append((p, img))
    return result