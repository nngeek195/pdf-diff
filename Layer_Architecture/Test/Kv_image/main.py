import tkinter as tk
from tkinter import filedialog, ttk
import threading
import queue
import os
import difflib
import math
import traceback

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageTk, ImageDraw, ImageFilter, ImageEnhance

# ── Optional OCR support ──────────────────────────────────────
try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

# ─────────────────────────────────────────────────────────────
# CONFIG & THEME
# ─────────────────────────────────────────────────────────────
BG            = "#0D1117"
SURFACE       = "#161B22"
SURFACE2      = "#21262D"
BORDER        = "#30363D"
TEXT          = "#E6EDF3"
SUBTEXT       = "#8B949E"
ACCENT        = "#58A6FF"
ADDED_COLOR   = (79, 195, 247, 115)
REMOVED_COLOR = (255, 213, 79, 115)
ERROR_COLOR   = "#F85149"
WARN_COLOR    = "#E3B341"
OK_COLOR      = "#3FB950"

RENDER_SCALE       = 1.5
CHUNK_PAGES        = 10
SEAM_MIN_RUN       = 6
SEAM_SCAN_PCT      = 0.35
PAGE_GAP           = 28
OCR_MIN_WORD_COUNT = 20
OCR_MIN_CONF       = 30


# ─────────────────────────────────────────────────────────────
# OCR HELPERS
# ─────────────────────────────────────────────────────────────

def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def _ocr_page_to_words(pil_image: Image.Image, page_num: int,
                        scale: float = 1.0) -> list:
    if not HAS_TESSERACT:
        return []
    src  = _preprocess_for_ocr(pil_image)
    data = pytesseract.image_to_data(src, output_type=pytesseract.Output.DICT)
    words = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        try:
            conf = int(data["conf"][i])
        except (ValueError, TypeError):
            conf = 0
        if conf < OCR_MIN_CONF:
            continue
        x0     = data["left"][i]   / scale
        top    = data["top"][i]    / scale
        width  = data["width"][i]  / scale
        height = data["height"][i] / scale
        words.append({
            "text":   text,
            "x0":     x0,
            "top":    top,
            "x1":     x0 + width,
            "bottom": top + height,
            "page":   page_num,
        })
    return words


# ─────────────────────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────────────────────

class PDFDiffApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Pro PDF Diff Engine — Hybrid LCS + Kv + OCR")
        self.root.geometry("1300x850")
        self.root.configure(bg=BG)

        self.old_file = None
        self.new_file = None
        self.q        = queue.Queue()

        self.old_photos = []
        self.new_photos = []
        self.old_y      = PAGE_GAP
        self.new_y      = PAGE_GAP
        self.sync_offset_y = 0.0

        self.setup_ui()
        self.process_queue()

    # ─────────────────────────────────────────────────────────
    # UI SETUP
    # ─────────────────────────────────────────────────────────

    def setup_ui(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TProgressbar", thickness=3,
                        background=ACCENT, troughcolor=BG, borderwidth=0)

        self.progress = ttk.Progressbar(self.root, style="TProgressbar",
                                        orient="horizontal", mode="determinate")
        self.progress.pack(fill=tk.X, side=tk.TOP)

        top_bar = tk.Frame(self.root, bg=SURFACE, height=56,
                           highlightbackground=BORDER, highlightthickness=1)
        top_bar.pack(fill=tk.X, side=tk.TOP)
        top_bar.pack_propagate(False)

        tk.Label(top_bar, text="⬛ PDF DIFF", font=("Courier New", 14, "bold"),
                 bg=SURFACE, fg=TEXT).pack(side=tk.LEFT, padx=(20, 10), pady=15)

        tess_color = OK_COLOR if HAS_TESSERACT else ERROR_COLOR
        tess_text  = "OCR ✓" if HAS_TESSERACT else "OCR ✗ (pip install pytesseract)"
        tk.Label(top_bar, text=tess_text, bg=SURFACE, fg=tess_color,
                 font=("Arial", 9)).pack(side=tk.LEFT, padx=(0, 14), pady=15)

        btn_cfg = dict(bg=SURFACE2, fg="#C9D1D9", font=("Arial", 10),
                       relief=tk.FLAT, padx=14, pady=6, cursor="hand2",
                       highlightbackground=BORDER, highlightthickness=1,
                       activebackground=BORDER)

        self.btn_old = tk.Button(top_bar, text="📂 Old PDF", **btn_cfg,
                                 command=self.select_old)
        self.btn_old.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_new = tk.Button(top_bar, text="📂 New PDF", **btn_cfg,
                                 command=self.select_new)
        self.btn_new.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_run = tk.Button(top_bar, text="▶ Compare",
                                 bg="#238636", fg="white",
                                 font=("Arial", 10, "bold"), relief=tk.FLAT,
                                 command=self.run_diff, padx=18, pady=6,
                                 cursor="hand2", state=tk.DISABLED,
                                 activebackground="#2ea043",
                                 activeforeground="white")
        self.btn_run.pack(side=tk.LEFT, padx=10, pady=10)

        self.lbl_status = tk.Label(top_bar, text="Select both PDFs to begin.",
                                   bg=SURFACE, fg=SUBTEXT, font=("Arial", 10))
        self.lbl_status.pack(side=tk.RIGHT, padx=20, pady=15)

        pane_wrapper = tk.Frame(self.root, bg=BG)
        pane_wrapper.pack(fill=tk.BOTH, expand=True)

        self.sb_old = ttk.Scrollbar(pane_wrapper, orient="vertical",
                                    command=self._scroll_old_indep)
        self.sb_old.pack(side=tk.LEFT, fill=tk.Y)

        left_col = tk.Frame(pane_wrapper, bg=BG)
        left_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lh = tk.Frame(left_col, bg=SURFACE2, height=30,
                      highlightbackground=BORDER, highlightthickness=1)
        lh.pack(fill=tk.X)
        lh.pack_propagate(False)
        tk.Label(lh, text="OLD VERSION", bg=SURFACE2, fg=WARN_COLOR,
                 font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=14, pady=5)

        self.canvas_old = tk.Canvas(left_col, bg="#010409", highlightthickness=0,
                                    yscrollcommand=self._update_sbs_old)
        self.canvas_old.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.sb_center = ttk.Scrollbar(pane_wrapper, orient="vertical",
                                       command=self._scroll_sync)
        self.sb_center.pack(side=tk.LEFT, fill=tk.Y)

        self.sb_new = ttk.Scrollbar(pane_wrapper, orient="vertical",
                                    command=self._scroll_new_indep)
        self.sb_new.pack(side=tk.RIGHT, fill=tk.Y)

        right_col = tk.Frame(pane_wrapper, bg=BG,
                             highlightbackground=BORDER, highlightthickness=1)
        right_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rh = tk.Frame(right_col, bg=SURFACE2, height=30,
                      highlightbackground=BORDER, highlightthickness=1)
        rh.pack(fill=tk.X)
        rh.pack_propagate(False)
        tk.Label(rh, text="NEW VERSION", bg=SURFACE2, fg=ACCENT,
                 font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=14, pady=5)

        self.canvas_new = tk.Canvas(right_col, bg="#010409", highlightthickness=0,
                                    yscrollcommand=self.sb_new.set)
        self.canvas_new.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for canvas in (self.canvas_old, self.canvas_new):
            canvas.bind("<MouseWheel>", self.on_mousewheel)
            canvas.bind("<Button-4>",   self.on_mousewheel)
            canvas.bind("<Button-5>",   self.on_mousewheel)

    # ─────────────────────────────────────────────────────────
    # SCROLLBAR LOGIC
    # ─────────────────────────────────────────────────────────

    def _update_offset(self):
        self.sync_offset_y = (float(self.canvas_new.canvasy(0))
                              - float(self.canvas_old.canvasy(0)))

    def _update_sbs_old(self, first, last):
        self.sb_old.set(first, last)
        self.sb_center.set(first, last)

    def _scroll_old_indep(self, *args):
        self.canvas_old.yview(*args)
        self._update_offset()

    def _scroll_new_indep(self, *args):
        self.canvas_new.yview(*args)
        self._update_offset()

    def _scroll_sync(self, *args):
        self.canvas_old.yview(*args)
        target = float(self.canvas_old.canvasy(0)) + self.sync_offset_y
        self._scroll_canvas_to_y(self.canvas_new, target)

    def on_mousewheel(self, event):
        if event.num == 4:
            delta = -1
        elif event.num == 5:
            delta = 1
        else:
            delta = int(-1 * (event.delta / 120))

        if event.widget == self.canvas_old:
            self.canvas_old.yview_scroll(delta, "units")
            self._scroll_canvas_to_y(self.canvas_new,
                                     float(self.canvas_old.canvasy(0)) + self.sync_offset_y)
        else:
            self.canvas_new.yview_scroll(delta, "units")
            self._scroll_canvas_to_y(self.canvas_old,
                                     float(self.canvas_new.canvasy(0)) - self.sync_offset_y)
        return "break"

    def _scroll_canvas_to_y(self, canvas: tk.Canvas, y: float):
        bbox = canvas.bbox("all")
        if not bbox:
            return
        h = bbox[3] - bbox[1]
        if h <= 0:
            return
        canvas.yview_moveto(max(0.0, min(1.0, y / h)))

    # ─────────────────────────────────────────────────────────
    # FILE SELECTION
    # ─────────────────────────────────────────────────────────

    def select_old(self):
        path = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if path:
            self.old_file = path
            name = os.path.basename(path)
            self.btn_old.config(
                text=f"📄 {name[:19]+'…' if len(name) > 22 else name}",
                fg=ACCENT, highlightbackground=ACCENT)
            self.check_ready()

    def select_new(self):
        path = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if path:
            self.new_file = path
            name = os.path.basename(path)
            self.btn_new.config(
                text=f"📄 {name[:19]+'…' if len(name) > 22 else name}",
                fg=ACCENT, highlightbackground=ACCENT)
            self.check_ready()

    def check_ready(self):
        if self.old_file and self.new_file:
            self.btn_run.config(state=tk.NORMAL)

    def _set_status(self, msg: str, error=False, warn=False):
        color = ERROR_COLOR if error else (WARN_COLOR if warn else SUBTEXT)
        self.lbl_status.config(
            text=msg[:87] + "…" if len(msg) > 90 else msg, fg=color)

    # ─────────────────────────────────────────────────────────
    # START DIFF
    # ─────────────────────────────────────────────────────────

    def run_diff(self):
        self.btn_run.config(state=tk.DISABLED)
        self.canvas_old.delete("all")
        self.canvas_new.delete("all")
        self.old_photos.clear()
        self.new_photos.clear()
        self.old_y = PAGE_GAP
        self.new_y = PAGE_GAP
        self.sync_offset_y = 0.0
        self.progress["value"] = 2
        self._set_status("Starting…")
        self.root.update_idletasks()
        threading.Thread(target=self.diff_worker, daemon=True).start()

    # ─────────────────────────────────────────────────────────
    # EXTRACTION
    # ─────────────────────────────────────────────────────────

    def extract_page_range(self, pdf_path: str,
                           start_page: int, end_page: int,
                           pdfium_doc) -> list:
        words = []
        with pdfplumber.open(pdf_path) as pdf:
            max_pg = len(pdf.pages) - 1
            if start_page > max_pg:
                return words
            end_page = min(end_page, max_pg)

            for pg in range(start_page, end_page + 1):
                try:
                    plumber_page = pdf.pages[pg]
                    native_words = plumber_page.extract_words()

                    if len(native_words) >= OCR_MIN_WORD_COUNT:
                        for w in native_words:
                            words.append({
                                "text":   w["text"],
                                "page":   pg,
                                "x0":     w["x0"],
                                "top":    w["top"],
                                "x1":     w["x1"],
                                "bottom": w["bottom"],
                            })
                    else:
                        if not HAS_TESSERACT:
                            self.q.put(("warn",
                                        f"Page {pg+1}: only {len(native_words)} native words "
                                        "— pytesseract not installed, skipping OCR."))
                            for w in native_words:
                                words.append({
                                    "text":   w["text"],
                                    "page":   pg,
                                    "x0":     w["x0"],
                                    "top":    w["top"],
                                    "x1":     w["x1"],
                                    "bottom": w["bottom"],
                                })
                            continue

                        self.q.put(("status",
                                    f"Page {pg+1}: {len(native_words)} native words — "
                                    "switching to OCR…"))
                        pdfium_page = pdfium_doc[pg]
                        bitmap      = pdfium_page.render(scale=RENDER_SCALE)
                        pil_img     = bitmap.to_pil().convert("RGB")
                        ocr_words   = _ocr_page_to_words(pil_img, page_num=pg,
                                                          scale=RENDER_SCALE)
                        if ocr_words:
                            words.extend(ocr_words)
                        else:
                            self.q.put(("warn",
                                        f"Page {pg+1}: OCR returned no words."))

                except Exception as e:
                    self.q.put(("warn", f"Page {pg+1} extraction error: {e}"))

        return words

    # ─────────────────────────────────────────────────────────
    # SEAM DETECTION
    # ─────────────────────────────────────────────────────────

    def find_safe_seam(self, old_words: list, new_words: list):
        old_texts = [w["text"] for w in old_words]
        new_texts = [w["text"] for w in new_words]
        if not old_texts or not new_texts:
            return None
        scan_from = int(len(old_texts) * (1 - SEAM_SCAN_PCT))
        new_index = {}
        for j, t in enumerate(new_texts):
            new_index.setdefault(t, []).append(j)
        best_seam = None
        for i in range(scan_from, len(old_texts)):
            for j in new_index.get(old_texts[i], []):
                run = 0
                while (i + run < len(old_texts) and j + run < len(new_texts)
                       and old_texts[i + run] == new_texts[j + run]):
                    run += 1
                if run >= SEAM_MIN_RUN:
                    if not best_seam or (i + run) > best_seam["old_seam"]:
                        best_seam = {"old_seam": i + run, "new_seam": j + run}
        return best_seam

    # ─────────────────────────────────────────────────────────
    # RENDERING
    # ─────────────────────────────────────────────────────────

    def render_page(self, doc, page_idx: int,
                    highlights: list, color: tuple) -> Image.Image:
        page    = doc[page_idx]
        bitmap  = page.render(scale=RENDER_SCALE)
        pil_img = bitmap.to_pil().convert("RGBA")
        overlay = Image.new("RGBA", pil_img.size, (255, 255, 255, 0))
        draw    = ImageDraw.Draw(overlay)
        for w in highlights:
            draw.rectangle([
                w["x0"]     * RENDER_SCALE - 2,
                w["top"]    * RENDER_SCALE - 2,
                w["x1"]     * RENDER_SCALE + 2,
                w["bottom"] * RENDER_SCALE + 2,
            ], fill=color)
        return Image.alpha_composite(pil_img, overlay)

    def render_and_queue_pages(self, doc, start: int, end: int,
                               highlights: list, color: tuple, tag: str):
        """
        Unconditionally render every page in [start, end].
        Pages with no highlights render as plain images — no overlay.
        This guarantees blank, image-only and fully-identical pages
        always appear in the canvas output.
        """
        for p in range(start, end + 1):
            if p >= len(doc):
                break
            page_highlights = [w for w in highlights if w["page"] == p]
            try:
                img = self.render_page(doc, p, page_highlights, color)
                self.q.put((tag, img))
            except Exception as e:
                self.q.put(("warn", f"Render error page {p+1}: {e}"))

    # ─────────────────────────────────────────────────────────
    # WORKER THREAD
    # ─────────────────────────────────────────────────────────

    def diff_worker(self):
        old_pdf_doc = new_pdf_doc = None
        try:
            try:
                import kv_mechanism
                has_kv = True
            except ImportError:
                has_kv = False
                self.q.put(("warn", "kv_mechanism not found — using difflib only."))

            def normalize_pages(words: list) -> list:
                if not words:
                    return words
                min_pg = min(w["page"] for w in words)
                return [{**w, "page": w["page"] - min_pg} for w in words]

            old_pdf_doc  = pdfium.PdfDocument(self.old_file)
            new_pdf_doc  = pdfium.PdfDocument(self.new_file)
            total_old    = len(old_pdf_doc)
            total_new    = len(new_pdf_doc)
            total_chunks = math.ceil(max(total_old, total_new) / CHUNK_PAGES)

            old_tail: list = []
            new_tail: list = []
            total_removed = total_added = 0

            for chunk in range(total_chunks):
                page_start   = chunk * CHUNK_PAGES
                old_page_end = min(page_start + CHUNK_PAGES - 1, total_old - 1)
                new_page_end = min(page_start + CHUNK_PAGES - 1, total_new - 1)
                is_last      = (chunk == total_chunks - 1)

                pct = int(5 + (chunk / total_chunks) * 85)
                self.q.put(("progress", pct))
                self.q.put(("status",
                            f"Processing pages {page_start+1}–"
                            f"{max(old_page_end, new_page_end)+1}…"))

                # Render range is determined by page indices — computed NOW,
                # before extraction, so it is never affected by empty word lists.
                first_old_render = old_tail[0]["page"] if old_tail else page_start
                first_new_render = new_tail[0]["page"] if new_tail else page_start
                # Default last page = chunk boundary; refined below if words exist
                last_old_render  = old_page_end
                last_new_render  = new_page_end

                # 1. EXTRACT
                try:
                    old_chunk_words = self.extract_page_range(
                        self.old_file, page_start, old_page_end, old_pdf_doc)
                    new_chunk_words = self.extract_page_range(
                        self.new_file, page_start, new_page_end, new_pdf_doc)
                except Exception as e:
                    self.q.put(("warn", f"Chunk {chunk+1} extraction failed: {e}"))
                    # Pages still need to appear — render them blank
                    self.render_and_queue_pages(
                        old_pdf_doc, first_old_render, last_old_render,
                        [], REMOVED_COLOR, "page_old")
                    self.render_and_queue_pages(
                        new_pdf_doc, first_new_render, last_new_render,
                        [], ADDED_COLOR, "page_new")
                    old_tail, new_tail = [], []
                    continue

                old_words = old_tail + old_chunk_words
                new_words = new_tail + new_chunk_words

                # KEY FIX ── even when BOTH word lists are empty we must render.
                # This covers fully-blank pages, pure-image pages, covers, etc.
                if not old_words and not new_words:
                    self.render_and_queue_pages(
                        old_pdf_doc, first_old_render, last_old_render,
                        [], REMOVED_COLOR, "page_old")
                    self.render_and_queue_pages(
                        new_pdf_doc, first_new_render, last_new_render,
                        [], ADDED_COLOR, "page_new")
                    old_tail, new_tail = [], []
                    continue

                commit_old = len(old_words)
                commit_new = len(new_words)

                # 2. SEAM
                if not is_last:
                    seam = self.find_safe_seam(old_words, new_words)
                    if seam:
                        commit_old = seam["old_seam"]
                        commit_new = seam["new_seam"]
                    elif old_chunk_words or new_chunk_words:
                        old_tail, new_tail = old_words, new_words
                        continue

                old_commit = old_words[:commit_old]
                new_commit = new_words[:commit_new]

                # Narrow render range to actual word extents (never widen it)
                if old_commit:
                    last_old_render = old_commit[-1]["page"]
                if new_commit:
                    last_new_render = new_commit[-1]["page"]

                # 3. LCS first pass
                sm = difflib.SequenceMatcher(
                    None,
                    [w["text"] for w in old_commit],
                    [w["text"] for w in new_commit],
                )
                lcs_removed, lcs_added = [], []
                for op, i1, i2, j1, j2 in sm.get_opcodes():
                    if op in ("replace", "delete"):
                        lcs_removed.extend(old_commit[i1:i2])
                    if op in ("replace", "insert"):
                        lcs_added.extend(new_commit[j1:j2])

                # 4. Kv second pass
                if has_kv and (lcs_removed or lcs_added):
                    self.q.put(("status", f"Running Kv on chunk {chunk+1}…"))
                    old_min = min(w["page"] for w in lcs_removed) if lcs_removed else 0
                    new_min = min(w["page"] for w in lcs_added)   if lcs_added   else 0

                    kv_results = kv_mechanism.run_diff(
                        normalize_pages(lcs_removed),
                        normalize_pages(lcs_added),
                    )
                    final_removed = [
                        {**w, "page": w["page"] + old_min}
                        for w in kv_results.get("removed_words", [])
                    ]
                    final_added = [
                        {**w, "page": w["page"] + new_min}
                        for w in kv_results.get("added_words", [])
                    ]
                else:
                    final_removed = lcs_removed
                    final_added   = lcs_added

                total_removed += len(final_removed)
                total_added   += len(final_added)

                # 5. RENDER — every page in range, with or without diffs
                self.q.put(("status", f"Rendering chunk {chunk+1}…"))
                self.render_and_queue_pages(
                    old_pdf_doc, first_old_render, last_old_render,
                    final_removed, REMOVED_COLOR, "page_old")
                self.render_and_queue_pages(
                    new_pdf_doc, first_new_render, last_new_render,
                    final_added, ADDED_COLOR, "page_new")

                old_tail = old_words[commit_old:]
                new_tail = new_words[commit_new:]

            self.q.put(("done",
                        f"✅ Done — {total_removed} removed · {total_added} added"))

        except Exception as e:
            self.q.put(("error", f"{type(e).__name__}: {e}"))
            print(traceback.format_exc())
        finally:
            for doc in (old_pdf_doc, new_pdf_doc):
                if doc:
                    try:
                        doc.close()
                    except Exception:
                        pass

    # ─────────────────────────────────────────────────────────
    # QUEUE PROCESSOR
    # ─────────────────────────────────────────────────────────

    def process_queue(self):
        try:
            while True:
                msg_type, data = self.q.get_nowait()

                if msg_type == "status":
                    self._set_status(data)
                elif msg_type == "warn":
                    self._set_status(data, warn=True)
                elif msg_type == "progress":
                    self.progress["value"] = data
                elif msg_type in ("page_old", "page_new"):
                    is_old    = (msg_type == "page_old")
                    canvas    = self.canvas_old if is_old else self.canvas_new
                    photos    = self.old_photos  if is_old else self.new_photos
                    current_y = self.old_y       if is_old else self.new_y

                    photo = ImageTk.PhotoImage(data)
                    photos.append(photo)
                    cx = canvas.winfo_width() // 2

                    canvas.create_rectangle(
                        cx - data.width  // 2 - 1, current_y - 1,
                        cx + data.width  // 2 + 1, current_y + data.height + 1,
                        outline=BORDER, fill=BORDER)
                    canvas.create_image(cx, current_y, image=photo, anchor="n")

                    new_y = current_y + data.height + PAGE_GAP
                    if is_old:
                        self.old_y = new_y
                    else:
                        self.new_y = new_y
                    canvas.config(scrollregion=canvas.bbox("all"))

                elif msg_type == "done":
                    self._set_status(data)
                    self.btn_run.config(state=tk.NORMAL)
                    self.progress["value"] = 100
                    self.root.after(2000, lambda: self.progress.configure(value=0))

                elif msg_type == "error":
                    self._set_status(f"❌ {data}", error=True)
                    self.btn_run.config(state=tk.NORMAL)
                    self.progress["value"] = 0

        except queue.Empty:
            pass
        self.root.after(50, self.process_queue)


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app  = PDFDiffApp(root)
    root.mainloop()