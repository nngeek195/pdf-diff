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

try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

# ─────────────────────────────────────────────────────────────
# CONFIG & THEME
# ─────────────────────────────────────────────────────────────
BG           = "#0D1117"
SURFACE      = "#161B22"
SURFACE2     = "#21262D"
BORDER       = "#30363D"
TEXT         = "#E6EDF3"
SUBTEXT      = "#8B949E"
ACCENT       = "#58A6FF"
ADDED_COLOR   = (79, 195, 247, 115)
REMOVED_COLOR = (255, 213, 79, 115)
ERROR_COLOR  = "#F85149"
WARN_COLOR   = "#E3B341"
OK_COLOR     = "#3FB950"

RENDER_SCALE       = 1.5
CHUNK_PAGES        = 10
SEAM_MIN_RUN       = 6
SEAM_SCAN_PCT      = 0.35
PAGE_GAP           = 28
OCR_MIN_WORD_COUNT = 10      # pages with fewer native words → treated as scanned
OCR_MIN_CONF       = 30      # tesseract confidence threshold (0-100)
OCR_PREPROCESS     = True    # enhance contrast/sharpen before OCR


# ─────────────────────────────────────────────────────────────
# OCR HELPERS
# ─────────────────────────────────────────────────────────────

def preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """Grayscale + contrast boost + sharpen — improves Tesseract accuracy."""
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def ocr_page_to_words(pil_image: Image.Image, page_num: int,
                       scale: float = 1.0) -> list[dict]:
    """
    Run Tesseract on a rendered page image and return word dicts in the same
    schema as pdfplumber: {text, x0, top, x1, bottom, page}.

    `scale` is the render scale used when generating pil_image so that
    coordinates are converted back to PDF-space units.
    """
    if not HAS_TESSERACT:
        return []

    src = preprocess_for_ocr(pil_image) if OCR_PREPROCESS else pil_image.convert("L")

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
# APP
# ─────────────────────────────────────────────────────────────

class OCRPDFDiffApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PDF Diff — OCR / Scanned PDF Engine")
        self.root.geometry("1300x850")
        self.root.configure(bg=BG)

        self.old_file: str | None = None
        self.new_file: str | None = None
        self.q: queue.Queue = queue.Queue()

        self.old_photos: list = []
        self.new_photos: list = []
        self.old_y     = PAGE_GAP
        self.new_y     = PAGE_GAP
        self.sync_offset_y = 0.0

        self._build_ui()
        self._poll_queue()

    # ─────────────────────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TProgressbar", thickness=3,
                        background=ACCENT, troughcolor=BG, borderwidth=0)

        self.progress = ttk.Progressbar(self.root, style="TProgressbar",
                                        orient="horizontal", mode="determinate")
        self.progress.pack(fill=tk.X, side=tk.TOP)

        # ── top bar ──────────────────────────────────────────
        top = tk.Frame(self.root, bg=SURFACE, height=56,
                       highlightbackground=BORDER, highlightthickness=1)
        top.pack(fill=tk.X, side=tk.TOP)
        top.pack_propagate(False)

        tk.Label(top, text="🔍 OCR PDF DIFF", font=("Courier New", 13, "bold"),
                 bg=SURFACE, fg=TEXT).pack(side=tk.LEFT, padx=(20, 8), pady=14)

        # Tesseract availability badge
        tess_color = OK_COLOR if HAS_TESSERACT else ERROR_COLOR
        tess_label = "Tesseract ✓" if HAS_TESSERACT else "Tesseract ✗  (pip install pytesseract)"
        tk.Label(top, text=tess_label, bg=SURFACE, fg=tess_color,
                 font=("Arial", 9)).pack(side=tk.LEFT, padx=(0, 16), pady=14)

        btn_cfg = dict(bg=SURFACE2, fg="#C9D1D9", font=("Arial", 10),
                       relief=tk.FLAT, padx=14, pady=6, cursor="hand2",
                       highlightbackground=BORDER, highlightthickness=1,
                       activebackground=BORDER)

        self.btn_old = tk.Button(top, text="📂 Old PDF", **btn_cfg,
                                 command=self._pick_old)
        self.btn_old.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_new = tk.Button(top, text="📂 New PDF", **btn_cfg,
                                 command=self._pick_new)
        self.btn_new.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_run = tk.Button(top, text="▶ Compare",
                                 bg="#238636", fg="white",
                                 font=("Arial", 10, "bold"), relief=tk.FLAT,
                                 command=self._start_diff, padx=18, pady=6,
                                 cursor="hand2", state=tk.DISABLED,
                                 activebackground="#2ea043", activeforeground="white")
        self.btn_run.pack(side=tk.LEFT, padx=10, pady=10)

        self.lbl_status = tk.Label(top, text="Select both PDFs to begin.",
                                   bg=SURFACE, fg=SUBTEXT, font=("Arial", 10))
        self.lbl_status.pack(side=tk.RIGHT, padx=20, pady=14)

        # ── canvas area with 3 scrollbars ────────────────────
        wrapper = tk.Frame(self.root, bg=BG)
        wrapper.pack(fill=tk.BOTH, expand=True)

        self.sb_old = ttk.Scrollbar(wrapper, orient="vertical",
                                    command=self._scroll_old_indep)
        self.sb_old.pack(side=tk.LEFT, fill=tk.Y)

        left_col = tk.Frame(wrapper, bg=BG)
        left_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lh = tk.Frame(left_col, bg=SURFACE2, height=30,
                      highlightbackground=BORDER, highlightthickness=1)
        lh.pack(fill=tk.X); lh.pack_propagate(False)
        tk.Label(lh, text="OLD VERSION", bg=SURFACE2, fg=WARN_COLOR,
                 font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=14, pady=5)

        self.canvas_old = tk.Canvas(left_col, bg="#010409", highlightthickness=0,
                                    yscrollcommand=self._update_sbs_old)
        self.canvas_old.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.sb_center = ttk.Scrollbar(wrapper, orient="vertical",
                                       command=self._scroll_sync)
        self.sb_center.pack(side=tk.LEFT, fill=tk.Y)

        self.sb_new = ttk.Scrollbar(wrapper, orient="vertical",
                                    command=self._scroll_new_indep)
        self.sb_new.pack(side=tk.RIGHT, fill=tk.Y)

        right_col = tk.Frame(wrapper, bg=BG,
                             highlightbackground=BORDER, highlightthickness=1)
        right_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rh = tk.Frame(right_col, bg=SURFACE2, height=30,
                      highlightbackground=BORDER, highlightthickness=1)
        rh.pack(fill=tk.X); rh.pack_propagate(False)
        tk.Label(rh, text="NEW VERSION", bg=SURFACE2, fg=ACCENT,
                 font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=14, pady=5)

        self.canvas_new = tk.Canvas(right_col, bg="#010409", highlightthickness=0,
                                    yscrollcommand=self.sb_new.set)
        self.canvas_new.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for c in (self.canvas_old, self.canvas_new):
            c.bind("<MouseWheel>", self._mousewheel)
            c.bind("<Button-4>",   self._mousewheel)
            c.bind("<Button-5>",   self._mousewheel)

    # ─────────────────────────────────────────────────────────
    # SCROLLBAR LOGIC
    # ─────────────────────────────────────────────────────────

    def _update_sbs_old(self, first, last):
        self.sb_old.set(first, last)
        self.sb_center.set(first, last)

    def _scroll_old_indep(self, *args):
        self.canvas_old.yview(*args)
        self._recalc_offset()

    def _scroll_new_indep(self, *args):
        self.canvas_new.yview(*args)
        self._recalc_offset()

    def _scroll_sync(self, *args):
        self.canvas_old.yview(*args)
        target = float(self.canvas_old.canvasy(0)) + self.sync_offset_y
        self._move_canvas_to(self.canvas_new, target)

    def _recalc_offset(self):
        self.sync_offset_y = (float(self.canvas_new.canvasy(0))
                              - float(self.canvas_old.canvasy(0)))

    def _mousewheel(self, event):
        delta = -1 if event.num == 4 else (1 if event.num == 5
                else int(-event.delta / 120))
        if event.widget == self.canvas_old:
            self.canvas_old.yview_scroll(delta, "units")
            self._move_canvas_to(self.canvas_new,
                                 float(self.canvas_old.canvasy(0)) + self.sync_offset_y)
        else:
            self.canvas_new.yview_scroll(delta, "units")
            self._move_canvas_to(self.canvas_old,
                                 float(self.canvas_new.canvasy(0)) - self.sync_offset_y)
        return "break"

    def _move_canvas_to(self, canvas: tk.Canvas, y: float):
        bbox = canvas.bbox("all")
        if not bbox: return
        h = bbox[3] - bbox[1]
        if h <= 0: return
        canvas.yview_moveto(max(0.0, min(1.0, y / h)))

    # ─────────────────────────────────────────────────────────
    # FILE SELECTION
    # ─────────────────────────────────────────────────────────

    def _pick_old(self):
        p = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if p:
            self.old_file = p
            name = os.path.basename(p)
            self.btn_old.config(
                text=f"📄 {name[:19]+'…' if len(name)>22 else name}",
                fg=ACCENT, highlightbackground=ACCENT)
            self._check_ready()

    def _pick_new(self):
        p = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if p:
            self.new_file = p
            name = os.path.basename(p)
            self.btn_new.config(
                text=f"📄 {name[:19]+'…' if len(name)>22 else name}",
                fg=ACCENT, highlightbackground=ACCENT)
            self._check_ready()

    def _check_ready(self):
        if self.old_file and self.new_file:
            self.btn_run.config(state=tk.NORMAL)

    def _set_status(self, msg: str, error=False, warn=False):
        color = ERROR_COLOR if error else (WARN_COLOR if warn else SUBTEXT)
        self.lbl_status.config(
            text=msg[:87]+"…" if len(msg) > 90 else msg, fg=color)

    # ─────────────────────────────────────────────────────────
    # START DIFF
    # ─────────────────────────────────────────────────────────

    def _start_diff(self):
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
        threading.Thread(target=self._worker, daemon=True).start()

    # ─────────────────────────────────────────────────────────
    # EXTRACTION  (auto-route: native text vs OCR)
    # ─────────────────────────────────────────────────────────

    def _is_scanned_page(self, plumber_page) -> bool:
        """Return True if the page has too few native words to be trusted."""
        try:
            words = plumber_page.extract_words()
            return len(words) < OCR_MIN_WORD_COUNT
        except Exception:
            return True

    def _extract_page_range(self, pdf_path: str,
                             start_page: int, end_page: int,
                             pdfium_doc) -> list[dict]:
        """
        Extract words from pages [start_page, end_page] (0-based, inclusive).

        Per-page routing:
          • Native text  → pdfplumber  (fast, pixel-perfect coords)
          • Scanned/image → pypdfium2 render + pytesseract OCR
        """
        words = []
        with pdfplumber.open(pdf_path) as pdf:
            max_pg = len(pdf.pages) - 1
            if start_page > max_pg:
                return words
            end_page = min(end_page, max_pg)

            for pg in range(start_page, end_page + 1):
                try:
                    plumber_page = pdf.pages[pg]

                    if not self._is_scanned_page(plumber_page):
                        # ── Native path ──────────────────────
                        for w in plumber_page.extract_words():
                            words.append({
                                "text":   w["text"],
                                "page":   pg,
                                "x0":     w["x0"],
                                "top":    w["top"],
                                "x1":     w["x1"],
                                "bottom": w["bottom"],
                            })
                    else:
                        # ── OCR path ─────────────────────────
                        if not HAS_TESSERACT:
                            self.q.put(("warn",
                                        f"Page {pg+1} appears scanned but pytesseract "
                                        "is not installed — skipping OCR."))
                            continue

                        self.q.put(("status",
                                    f"OCR scanning page {pg+1}…"))

                        pdfium_page = pdfium_doc[pg]
                        bitmap  = pdfium_page.render(scale=RENDER_SCALE)
                        pil_img = bitmap.to_pil().convert("RGB")

                        ocr_words = ocr_page_to_words(
                            pil_img, page_num=pg, scale=RENDER_SCALE)

                        if ocr_words:
                            words.extend(ocr_words)
                        else:
                            self.q.put(("warn",
                                        f"Page {pg+1}: OCR returned no words "
                                        "(low quality scan?)."))

                except Exception as e:
                    self.q.put(("warn", f"Page {pg+1} extraction error: {e}"))

        return words

    # ─────────────────────────────────────────────────────────
    # SEAM DETECTION
    # ─────────────────────────────────────────────────────────

    def _find_seam(self, old_words: list, new_words: list) -> dict | None:
        old_t = [w["text"] for w in old_words]
        new_t = [w["text"] for w in new_words]
        if not old_t or not new_t:
            return None

        scan_from = int(len(old_t) * (1 - SEAM_SCAN_PCT))
        new_index: dict[str, list[int]] = {}
        for j, t in enumerate(new_t):
            new_index.setdefault(t, []).append(j)

        best = None
        for i in range(scan_from, len(old_t)):
            for j in new_index.get(old_t[i], []):
                run = 0
                while (i + run < len(old_t) and j + run < len(new_t)
                       and old_t[i + run] == new_t[j + run]):
                    run += 1
                if run >= SEAM_MIN_RUN:
                    if not best or (i + run) > best["old_seam"]:
                        best = {"old_seam": i + run, "new_seam": j + run}
        return best

    # ─────────────────────────────────────────────────────────
    # RENDERING
    # ─────────────────────────────────────────────────────────

    def _render_page(self, doc, page_idx: int,
                     highlights: list[dict], color: tuple) -> Image.Image:
        page   = doc[page_idx]
        bitmap = page.render(scale=RENDER_SCALE)
        img    = bitmap.to_pil().convert("RGBA")
        overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
        draw    = ImageDraw.Draw(overlay)
        for w in highlights:
            draw.rectangle([
                w["x0"]     * RENDER_SCALE - 2,
                w["top"]    * RENDER_SCALE - 2,
                w["x1"]     * RENDER_SCALE + 2,
                w["bottom"] * RENDER_SCALE + 2,
            ], fill=color)
        return Image.alpha_composite(img, overlay)

    def _render_range(self, doc, start: int, end: int,
                      highlights: list[dict], color: tuple, tag: str):
        for p in range(start, end + 1):
            if p >= len(doc):
                break
            ph = [w for w in highlights if w["page"] == p]
            try:
                img = self._render_page(doc, p, ph, color)
                self.q.put((tag, img))
            except Exception as e:
                self.q.put(("warn", f"Render error page {p+1}: {e}"))

    # ─────────────────────────────────────────────────────────
    # WORKER THREAD
    # ─────────────────────────────────────────────────────────

    def _worker(self):
        old_doc = new_doc = None
        try:
            # Try Kv C++ engine
            try:
                import kv_mechanism
                has_kv = True
            except ImportError:
                has_kv = False
                self.q.put(("warn", "kv_mechanism not found — using difflib only."))

            def normalize_pages(words: list[dict]) -> list[dict]:
                if not words:
                    return words
                min_pg = min(w["page"] for w in words)
                return [{**w, "page": w["page"] - min_pg} for w in words]

            old_doc = pdfium.PdfDocument(self.old_file)
            new_doc = pdfium.PdfDocument(self.new_file)
            total_old = len(old_doc)
            total_new = len(new_doc)
            total_chunks = math.ceil(max(total_old, total_new) / CHUNK_PAGES)

            old_tail: list[dict] = []
            new_tail: list[dict] = []
            total_removed = total_added = 0

            for chunk in range(total_chunks):
                p_start   = chunk * CHUNK_PAGES
                old_p_end = min(p_start + CHUNK_PAGES - 1, total_old - 1)
                new_p_end = min(p_start + CHUNK_PAGES - 1, total_new - 1)
                is_last   = (chunk == total_chunks - 1)

                pct = int(5 + (chunk / total_chunks) * 85)
                self.q.put(("progress", pct))
                self.q.put(("status",
                            f"Processing pages {p_start+1}–"
                            f"{max(old_p_end, new_p_end)+1}…"))

                # 1. Extract (auto-routed per page)
                try:
                    old_chunk = self._extract_page_range(
                        self.old_file, p_start, old_p_end, old_doc)
                    new_chunk = self._extract_page_range(
                        self.new_file, p_start, new_p_end, new_doc)
                except Exception as e:
                    self.q.put(("warn", f"Chunk {chunk+1} extraction failed: {e}"))
                    continue

                old_words = old_tail + old_chunk
                new_words = new_tail + new_chunk
                if not old_words and not new_words:
                    continue

                commit_old = len(old_words)
                commit_new = len(new_words)

                # 2. Seam
                if not is_last:
                    seam = self._find_seam(old_words, new_words)
                    if seam:
                        commit_old = seam["old_seam"]
                        commit_new = seam["new_seam"]
                    elif old_chunk or new_chunk:
                        old_tail, new_tail = old_words, new_words
                        continue

                old_commit = old_words[:commit_old]
                new_commit = new_words[:commit_new]

                # Save render bounds before any normalization
                first_old_render = old_tail[0]["page"] if old_tail else p_start
                first_new_render = new_tail[0]["page"] if new_tail else p_start
                last_old_page    = old_commit[-1]["page"] if old_commit else p_start
                last_new_page    = new_commit[-1]["page"] if new_commit else new_p_end

                # 3. LCS first pass
                sm = difflib.SequenceMatcher(
                    None,
                    [w["text"] for w in old_commit],
                    [w["text"] for w in new_commit],
                )
                lcs_removed, lcs_added = [], []
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag in ("replace", "delete"):
                        lcs_removed.extend(old_commit[i1:i2])
                    if tag in ("replace", "insert"):
                        lcs_added.extend(new_commit[j1:j2])

                # 4. Kv second pass
                if has_kv and (lcs_removed or lcs_added):
                    self.q.put(("status", f"Running Kv on chunk {chunk+1}…"))
                    old_min = min(w["page"] for w in lcs_removed) if lcs_removed else 0
                    new_min = min(w["page"] for w in lcs_added)   if lcs_added   else 0

                    kv_res = kv_mechanism.run_diff(
                        normalize_pages(lcs_removed),
                        normalize_pages(lcs_added),
                    )
                    final_removed = [
                        {**w, "page": w["page"] + old_min}
                        for w in kv_res.get("removed_words", [])
                    ]
                    final_added = [
                        {**w, "page": w["page"] + new_min}
                        for w in kv_res.get("added_words", [])
                    ]
                else:
                    final_removed = lcs_removed
                    final_added   = lcs_added

                total_removed += len(final_removed)
                total_added   += len(final_added)

                # 5. Render
                self.q.put(("status", f"Rendering chunk {chunk+1}…"))
                self._render_range(old_doc, first_old_render, last_old_page,
                                   final_removed, REMOVED_COLOR, "page_old")
                self._render_range(new_doc, first_new_render, last_new_page,
                                   final_added, ADDED_COLOR, "page_new")

                old_tail = old_words[commit_old:]
                new_tail = new_words[commit_new:]

            self.q.put(("done",
                        f"✅ Done — {total_removed} removed · {total_added} added"))

        except Exception as e:
            self.q.put(("error", f"{type(e).__name__}: {e}"))
            print(traceback.format_exc())
        finally:
            for doc in (old_doc, new_doc):
                if doc:
                    try: doc.close()
                    except Exception: pass

    # ─────────────────────────────────────────────────────────
    # QUEUE POLLING
    # ─────────────────────────────────────────────────────────

    def _poll_queue(self):
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
                    is_old  = (msg_type == "page_old")
                    canvas  = self.canvas_old if is_old else self.canvas_new
                    photos  = self.old_photos  if is_old else self.new_photos
                    cur_y   = self.old_y       if is_old else self.new_y

                    photo = ImageTk.PhotoImage(data)
                    photos.append(photo)
                    cx = canvas.winfo_width() // 2

                    canvas.create_rectangle(
                        cx - data.width  // 2 - 1, cur_y - 1,
                        cx + data.width  // 2 + 1, cur_y + data.height + 1,
                        outline=BORDER, fill=BORDER)
                    canvas.create_image(cx, cur_y, image=photo, anchor="n")

                    new_y = cur_y + data.height + PAGE_GAP
                    if is_old: self.old_y = new_y
                    else:      self.new_y = new_y
                    canvas.config(scrollregion=canvas.bbox("all"))

                elif msg_type == "done":
                    self._set_status(data)
                    self.btn_run.config(state=tk.NORMAL)
                    self.progress["value"] = 100
                    self.root.after(2000,
                                    lambda: self.progress.configure(value=0))
                elif msg_type == "error":
                    self._set_status(f"❌ {data}", error=True)
                    self.btn_run.config(state=tk.NORMAL)
                    self.progress["value"] = 0

        except queue.Empty:
            pass
        self.root.after(50, self._poll_queue)


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app  = OCRPDFDiffApp(root)
    root.mainloop()