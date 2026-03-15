"""
Layer 3 — Direct Injection (The Painter)
==========================================
Paints highlights directly onto PDF pages using exact bounding boxes
from the word database.

  OLD PDF  →  Yellow highlights on removed words
  NEW PDF  →  Blue   highlights on added words

Key design for chunked streaming
---------------------------------
paint_page_range_to_images() — renders pages to PIL Image objects and
returns them directly. The pipeline queues these images for the UI to
display, and also appends them to the output PDF.

This avoids the previous approach of writing a shared output PDF file
that both the background thread (writing) and main thread (reading)
accessed simultaneously — which caused race conditions and required
re-reading the entire accumulated PDF on every chunk.
"""

import os
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from layer1_extraction import WordObject, get_page_dimensions


# ── Highlight colors (RGBA) ───────────────────────────────────────────────────
COLOR_REMOVED = (255, 235,  59, 130)   # Yellow — removed from OLD
COLOR_ADDED   = ( 66, 165, 245, 130)   # Blue   — added in NEW

RENDER_SCALE  = 2.0


# ── Internal: paint one page ─────────────────────────────────────────────────

def _paint_page(pdfium_page, highlight_words: list, color: tuple) -> Image.Image:
    """
    Render one pdfium page to a PIL Image and draw highlight rectangles.
    pdfplumber coords (top-left origin) match pypdfium2 render orientation.
    """
    bitmap = pdfium_page.render(scale=RENDER_SCALE)
    img    = bitmap.to_pil().convert("RGBA")
    iw, ih = img.size

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    for word in highlight_words:
        px0 = int(word.bbox.x0 * RENDER_SCALE)
        py0 = int(word.bbox.y0 * RENDER_SCALE)
        px1 = int(word.bbox.x1 * RENDER_SCALE)
        py1 = int(word.bbox.y1 * RENDER_SCALE)
        pad = max(1, int(1.5 * RENDER_SCALE))
        draw.rectangle([
            max(0, px0-pad), max(0, py0-pad),
            min(iw, px1+pad), min(ih, py1+pad)
        ], fill=color)

    return Image.alpha_composite(img, overlay).convert("RGB")


# ── Chunked painting: returns images AND saves to PDF ────────────────────────

def paint_page_range_to_images(
    pdf_path:        str,
    word_db:         list,          # WordObjects for this chunk
    highlight_words: set,           # WordObject instances to highlight
    color:           tuple,
    page_start:      int,
    page_end:        int,
) -> list:
    """
    Render pages [page_start … page_end], draw highlights, and return
    a list of (page_number, PIL.Image) tuples.

    Returns images directly — the caller decides what to do with them
    (display in UI and/or save to disk). No shared file I/O here.
    """
    # Group highlight words by page
    by_page: dict[int, list] = {}
    for w in word_db:
        if w in highlight_words:
            by_page.setdefault(w.page_number, []).append(w)

    pdf_doc     = pdfium.PdfDocument(pdf_path)
    total_pages = len(pdf_doc)
    end         = min(page_end, total_pages)

    result = []
    for page_num in range(page_start, end + 1):
        pdfium_page   = pdf_doc[page_num - 1]
        words_on_page = by_page.get(page_num, [])
        img           = _paint_page(pdfium_page, words_on_page, color)
        result.append((page_num, img))
        pdfium_page.close()

    pdf_doc.close()
    return result


def save_images_to_pdf(
    page_images: list,   # list of (page_num, PIL.Image)
    output_path: str,
    append:      bool = False,
) -> None:
    """
    Save (or append) a list of PIL Images to a PDF file.
    If append=True and the file exists, new images are appended after
    the existing pages using a safe read-then-write approach.
    """
    if not page_images:
        return

    imgs = [img for _, img in page_images]

    if append and Path(output_path).exists():
        # Read existing pages first, then write all together
        existing_doc  = pdfium.PdfDocument(output_path)
        existing_imgs = []
        for ep in existing_doc:
            bm = ep.render(scale=RENDER_SCALE)
            existing_imgs.append(bm.to_pil().convert("RGB"))
            ep.close()
        existing_doc.close()
        imgs = existing_imgs + imgs

    first, rest = imgs[0], imgs[1:]
    first.save(
        output_path,
        save_all=True,
        append_images=rest,
        format="PDF",
        resolution=72 * RENDER_SCALE,
    )


# ── Full painting (small docs / testing) ─────────────────────────────────────

def paint_pdf(
    original_pdf_path: str,
    word_db:           list,
    target_indices:    set,
    output_path:       str,
    highlight_color:   tuple,
    label:             str = "",
) -> str:
    print(f"  [{label}] Painting {len(target_indices)} highlights → {output_path}")
    highlight_words = {w for w in word_db if w.index in target_indices}
    page_dims   = get_page_dimensions(original_pdf_path)
    num_pages   = len(page_dims)
    by_page: dict[int, list] = {}
    for w in highlight_words:
        by_page.setdefault(w.page_number, []).append(w)
    pdf_doc = pdfium.PdfDocument(original_pdf_path)
    page_images = []
    for page_num in range(1, num_pages + 1):
        pdfium_page   = pdf_doc[page_num - 1]
        words_on_page = by_page.get(page_num, [])
        img           = _paint_page(pdfium_page, words_on_page, highlight_color)
        page_images.append(img)
        pdfium_page.close()
    pdf_doc.close()
    if page_images:
        first, rest = page_images[0], page_images[1:]
        first.save(output_path, save_all=True, append_images=rest,
                   format="PDF", resolution=72 * RENDER_SCALE)
    return output_path


def paint_both(
    old_pdf_path:    str,
    new_pdf_path:    str,
    old_db:          list,
    new_db:          list,
    removed_indices: set,
    added_indices:   set,
    output_dir:      str = ".",
) -> tuple:
    os.makedirs(output_dir, exist_ok=True)
    old_out = str(Path(output_dir) / "old_marked.pdf")
    new_out = str(Path(output_dir) / "new_marked.pdf")
    paint_pdf(old_pdf_path, old_db, removed_indices, old_out, COLOR_REMOVED, "OLD")
    paint_pdf(new_pdf_path, new_db, added_indices,   new_out, COLOR_ADDED,   "NEW")
    return old_out, new_out