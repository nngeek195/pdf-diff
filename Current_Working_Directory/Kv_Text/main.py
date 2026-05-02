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
from PIL import Image, ImageTk, ImageDraw

# --- Configuration & Theme Colors ---
BG = "#0D1117"
SURFACE = "#161B22"
SURFACE2 = "#21262D"
BORDER = "#30363D"
TEXT = "#E6EDF3"
SUBTEXT = "#8B949E"
ACCENT = "#58A6FF"
ADDED_COLOR = (79, 195, 247, 115)
REMOVED_COLOR = (255, 213, 79, 115)
ERROR_COLOR = "#F85149"
WARN_COLOR  = "#E3B341"

RENDER_SCALE  = 1.5
CHUNK_PAGES   = 10
SEAM_MIN_RUN  = 6
SEAM_SCAN_PCT = 0.35
PAGE_GAP = 28


class PDFDiffApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Pro PDF Diff Engine - Hybrid LCS + Kv")
        self.root.geometry("1300x850")
        self.root.configure(bg=BG)

        self.old_file = None
        self.new_file = None
        self.q = queue.Queue()

        self.old_photos = []
        self.new_photos = []
        self.old_y = PAGE_GAP
        self.new_y = PAGE_GAP
        self.sync_offset_y = 0.0

        self.setup_ui()
        self.process_queue()

    # ─────────────────────────────────────────────────────────────────────────
    # UI SETUP WITH 3 SCROLLBARS
    # ─────────────────────────────────────────────────────────────────────────

    def setup_ui(self):
        style = ttk.Style()
        style.theme_use('default')
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

        self.btn_old = tk.Button(top_bar, text="📂 Old PDF", bg=SURFACE2, fg="#C9D1D9", font=("Arial", 10),
                                  relief=tk.FLAT, command=self.select_old, padx=14, pady=6, cursor="hand2",
                                  highlightbackground=BORDER, highlightthickness=1, activebackground=BORDER)
        self.btn_old.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_new = tk.Button(top_bar, text="📂 New PDF", bg=SURFACE2, fg="#C9D1D9", font=("Arial", 10),
                                  relief=tk.FLAT, command=self.select_new, padx=14, pady=6, cursor="hand2",
                                  highlightbackground=BORDER, highlightthickness=1, activebackground=BORDER)
        self.btn_new.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_run = tk.Button(top_bar, text="▶ Compare", bg="#238636", fg="white", font=("Arial", 10, "bold"),
                                  relief=tk.FLAT, command=self.run_diff, padx=18, pady=6, cursor="hand2",
                                  state=tk.DISABLED, activebackground="#2ea043", activeforeground="white")
        self.btn_run.pack(side=tk.LEFT, padx=10, pady=10)

        self.lbl_status = tk.Label(top_bar, text="Select both PDFs to begin.", bg=SURFACE, fg=SUBTEXT, font=("Arial", 10))
        self.lbl_status.pack(side=tk.RIGHT, padx=20, pady=15)

        pane_wrapper = tk.Frame(self.root, bg=BG)
        pane_wrapper.pack(fill=tk.BOTH, expand=True)

        self.sb_old = ttk.Scrollbar(pane_wrapper, orient="vertical", command=self._scroll_old_indep)
        self.sb_old.pack(side=tk.LEFT, fill=tk.Y)

        left_col = tk.Frame(pane_wrapper, bg=BG)
        left_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        left_header = tk.Frame(left_col, bg=SURFACE2, height=30, highlightbackground=BORDER, highlightthickness=1)
        left_header.pack(fill=tk.X)
        left_header.pack_propagate(False)
        tk.Label(left_header, text="OLD VERSION", bg=SURFACE2, fg="#E3B341", font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=14, pady=5)

        self.canvas_old = tk.Canvas(left_col, bg="#010409", highlightthickness=0, yscrollcommand=self._update_sbs_old)
        self.canvas_old.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.sb_center = ttk.Scrollbar(pane_wrapper, orient="vertical", command=self._scroll_sync)
        self.sb_center.pack(side=tk.LEFT, fill=tk.Y)

        self.sb_new = ttk.Scrollbar(pane_wrapper, orient="vertical", command=self._scroll_new_indep)
        self.sb_new.pack(side=tk.RIGHT, fill=tk.Y)

        right_col = tk.Frame(pane_wrapper, bg=BG, highlightbackground=BORDER, highlightthickness=1)
        right_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right_header = tk.Frame(right_col, bg=SURFACE2, height=30, highlightbackground=BORDER, highlightthickness=1)
        right_header.pack(fill=tk.X)
        right_header.pack_propagate(False)
        tk.Label(right_header, text="NEW VERSION", bg=SURFACE2, fg=ACCENT, font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=14, pady=5)

        self.canvas_new = tk.Canvas(right_col, bg="#010409", highlightthickness=0, yscrollcommand=self.sb_new.set)
        self.canvas_new.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for canvas in (self.canvas_old, self.canvas_new):
            canvas.bind("<MouseWheel>", self.on_mousewheel)
            canvas.bind("<Button-4>",   self.on_mousewheel)
            canvas.bind("<Button-5>",   self.on_mousewheel)

    # ─────────────────────────────────────────────────────────────────────────
    # 3-WAY SCROLLBAR & OFFSET LOGIC
    # ─────────────────────────────────────────────────────────────────────────

    def _update_offset(self):
        old_y = float(self.canvas_old.canvasy(0))
        new_y = float(self.canvas_new.canvasy(0))
        self.sync_offset_y = new_y - old_y

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
        target_new_y = float(self.canvas_old.canvasy(0)) + self.sync_offset_y
        self._scroll_canvas_to_y(self.canvas_new, target_new_y)

    def on_mousewheel(self, event):
        if event.num == 4: raw_delta = -1
        elif event.num == 5: raw_delta = 1
        else: raw_delta = int(-1 * (event.delta / 120))

        if event.widget == self.canvas_old:
            self.canvas_old.yview_scroll(raw_delta, "units")
            target_new_y = float(self.canvas_old.canvasy(0)) + self.sync_offset_y
            self._scroll_canvas_to_y(self.canvas_new, target_new_y)
        else:
            self.canvas_new.yview_scroll(raw_delta, "units")
            target_old_y = float(self.canvas_new.canvasy(0)) - self.sync_offset_y
            self._scroll_canvas_to_y(self.canvas_old, target_old_y)
        return "break"

    def _scroll_canvas_to_y(self, canvas: tk.Canvas, target_y: float):
        bbox = canvas.bbox("all")
        if not bbox: return
        total_h = bbox[3] - bbox[1]
        if total_h <= 0: return
        fraction = max(0.0, min(1.0, target_y / total_h))
        canvas.yview_moveto(fraction)

    # ─────────────────────────────────────────────────────────────────────────
    # FILE SELECTION
    # ─────────────────────────────────────────────────────────────────────────

    def select_old(self):
        path = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if path:
            self.old_file = path
            name = os.path.basename(path)
            self.btn_old.config(text=f"📄 {name[:19] + '…' if len(name) > 22 else name}", fg=ACCENT, highlightbackground=ACCENT)
            self.check_ready()

    def select_new(self):
        path = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if path:
            self.new_file = path
            name = os.path.basename(path)
            self.btn_new.config(text=f"📄 {name[:19] + '…' if len(name) > 22 else name}", fg=ACCENT, highlightbackground=ACCENT)
            self.check_ready()

    def check_ready(self):
        if self.old_file and self.new_file:
            self.btn_run.config(state=tk.NORMAL)

    def _set_status(self, msg, error=False, warn=False):
        color = ERROR_COLOR if error else (WARN_COLOR if warn else SUBTEXT)
        self.lbl_status.config(text=msg[:87] + "…" if len(msg) > 90 else msg, fg=color)

    # ─────────────────────────────────────────────────────────────────────────
    # RUN & CORE LOGIC
    # ─────────────────────────────────────────────────────────────────────────

    def run_diff(self):
        self.btn_run.config(state=tk.DISABLED)
        self.canvas_old.delete("all")
        self.canvas_new.delete("all")
        self.old_photos.clear()
        self.new_photos.clear()
        self.old_y = PAGE_GAP
        self.new_y = PAGE_GAP
        self.sync_offset_y = 0.0
        self.progress['value'] = 2
        self._set_status("Starting…")
        self.root.update_idletasks()
        threading.Thread(target=self.diff_worker, daemon=True).start()

    def extract_page_range(self, pdf_path, start_page, end_page):
        words = []
        with pdfplumber.open(pdf_path) as pdf:
            max_page = len(pdf.pages) - 1
            if start_page > max_page: return words
            end_page = min(end_page, max_page)
            for page_num in range(start_page, end_page + 1):
                try:
                    page = pdf.pages[page_num]
                    for w in page.extract_words():
                        words.append({
                            'text':   w['text'], 'page':   page_num,
                            'x0':     w['x0'],   'top':    w['top'],
                            'x1':     w['x1'],   'bottom': w['bottom'],
                        })
                except Exception as page_err:
                    self.q.put(('warn', f"Page {page_num + 1} could not be read: {page_err}"))
        return words

    def find_safe_seam(self, old_words, new_words):
        old_texts = [w['text'] for w in old_words]
        new_texts = [w['text'] for w in new_words]
        if not old_texts or not new_texts: return None
        scan_from = int(len(old_texts) * (1 - SEAM_SCAN_PCT))
        new_index = {}
        for j, text in enumerate(new_texts):
            if text not in new_index: new_index[text] = []
            new_index[text].append(j)
        best_seam = None
        for i in range(scan_from, len(old_texts)):
            candidates = new_index.get(old_texts[i])
            if not candidates: continue
            for j in candidates:
                run = 0
                while (i + run < len(old_texts) and j + run < len(new_texts) and old_texts[i + run] == new_texts[j + run]):
                    run += 1
                if run >= SEAM_MIN_RUN:
                    if not best_seam or (i + run) > best_seam['old_seam']:
                        best_seam = {'old_seam': i + run, 'new_seam': j + run}
        return best_seam

    def render_page(self, doc, page_idx, highlights, color):
        page    = doc[page_idx]
        bitmap  = page.render(scale=RENDER_SCALE)
        pil_img = bitmap.to_pil().convert("RGBA")
        overlay = Image.new('RGBA', pil_img.size, (255, 255, 255, 0))
        draw    = ImageDraw.Draw(overlay)
        for w in highlights:
            x0 = w['x0'] * RENDER_SCALE - 2
            y0 = w['top'] * RENDER_SCALE - 2
            x1 = w['x1'] * RENDER_SCALE + 2
            y1 = w['bottom'] * RENDER_SCALE + 2
            draw.rectangle([x0, y0, x1, y1], fill=color)
        return Image.alpha_composite(pil_img, overlay)

    def render_and_queue_pages(self, doc, start_page, end_page, highlights, color, queue_tag):
        if start_page > end_page: return
        for p in range(start_page, end_page + 1):
            if p >= len(doc): break
            page_highlights = [w for w in highlights if w['page'] == p]
            try:
                img = self.render_page(doc, p, page_highlights, color)
                self.q.put((queue_tag, img))
            except Exception as render_err:
                self.q.put(('warn', f"Page {p + 1} failed to render: {render_err}"))

    # ─────────────────────────────────────────────────────────────────────────
    # DIFF WORKER
    # ─────────────────────────────────────────────────────────────────────────

    def diff_worker(self):
        old_pdf_doc = new_pdf_doc = None
        try:
            # Try loading Kv C++ engine
            try:
                import Test.Kv_Text.kv_mechanism as kv_mechanism
                has_kv = True
            except ImportError:
                has_kv = False
                self.q.put(('warn', "kv_mechanism not found. Using pure difflib."))

            # ── normalize_pages: shift page numbers to 0-based ──
            # Defined here inside diff_worker so it has access to local scope
            def normalize_pages(words):
                if not words: return words
                min_page = min(w['page'] for w in words)
                return [{**w, 'page': w['page'] - min_page} for w in words]

            old_pdf_doc = pdfium.PdfDocument(self.old_file)
            new_pdf_doc = pdfium.PdfDocument(self.new_file)
            total_old_pages = len(old_pdf_doc)
            total_new_pages = len(new_pdf_doc)
            total_chunks = math.ceil(max(total_old_pages, total_new_pages) / CHUNK_PAGES)

            old_tail, new_tail = [], []
            total_removed, total_added = 0, 0

            for chunk in range(total_chunks):
                page_start   = chunk * CHUNK_PAGES
                old_page_end = min(page_start + CHUNK_PAGES - 1, total_old_pages - 1)
                new_page_end = min(page_start + CHUNK_PAGES - 1, total_new_pages - 1)
                is_last      = (chunk == total_chunks - 1)

                pct = int(5 + (chunk / total_chunks) * 85)
                self.q.put(('progress', pct))
                self.q.put(('status', f"Processing pages {page_start+1}–{max(old_page_end, new_page_end)+1}..."))

                # 1. EXTRACT
                try:
                    old_chunk_words = self.extract_page_range(self.old_file, page_start, old_page_end)
                    new_chunk_words = self.extract_page_range(self.new_file, page_start, new_page_end)
                except Exception as extract_err:
                    self.q.put(('warn', f"Chunk {chunk+1} extraction failed: {extract_err}"))
                    continue

                old_words = old_tail + old_chunk_words
                new_words = new_tail + new_chunk_words

                if not old_words and not new_words: continue

                commit_old_count = len(old_words)
                commit_new_count = len(new_words)

                # 2. SEAM — find safe cut point between chunks
                if not is_last:
                    seam = self.find_safe_seam(old_words, new_words)
                    if seam:
                        commit_old_count = seam['old_seam']
                        commit_new_count = seam['new_seam']
                    elif old_chunk_words or new_chunk_words:
                        old_tail, new_tail = old_words, new_words
                        continue

                old_commit = old_words[:commit_old_count]
                new_commit = new_words[:commit_new_count]

                # ── Calculate render page ranges from ORIGINAL page numbers ──
                # These must be saved BEFORE any normalization happens
                first_old_render = old_tail[0]['page'] if old_tail else page_start
                first_new_render = new_tail[0]['page'] if new_tail else page_start
                last_old_page    = old_commit[-1]['page'] if old_commit else page_start
                last_new_page = new_commit[-1]['page'] if new_commit else new_page_end

                # 3. LCS — first pass diff
                sm = difflib.SequenceMatcher(
                    None,
                    [w['text'] for w in old_commit],
                    [w['text'] for w in new_commit]
                )
                difflib_removed, difflib_added = [], []
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag in ('replace', 'delete'): difflib_removed.extend(old_commit[i1:i2])
                    if tag in ('replace', 'insert'): difflib_added.extend(new_commit[j1:j2])

                # 4. KV MECHANISM — second pass on leftovers
                if has_kv and (difflib_removed or difflib_added):
                    self.q.put(('status', f"Running Kv on chunk {chunk+1}..."))

                    # Save ORIGINAL page offsets before normalizing
                    old_min_page = min(w['page'] for w in difflib_removed) if difflib_removed else 0
                    new_min_page = min(w['page'] for w in difflib_added)   if difflib_added   else 0

                    # Pass 0-based page numbers into C++
                    kv_results = kv_mechanism.run_diff(
                        normalize_pages(difflib_removed),
                        normalize_pages(difflib_added)
                    )

                    # Restore ORIGINAL page numbers so render_and_queue_pages works
                    final_removed = [
                        {**w, 'page': w['page'] + old_min_page}
                        for w in kv_results.get("removed_words", [])
                    ]
                    final_added = [
                        {**w, 'page': w['page'] + new_min_page}
                        for w in kv_results.get("added_words", [])
                    ]
                else:
                    final_removed = difflib_removed
                    final_added   = difflib_added

                total_removed += len(final_removed)
                total_added   += len(final_added)

                # 5. RENDER — use page ranges saved before normalization
                self.q.put(('status', f"Rendering chunk {chunk+1}..."))
                self.render_and_queue_pages(
                    old_pdf_doc, first_old_render, last_old_page,
                    final_removed, REMOVED_COLOR, 'page_old'
                )
                self.render_and_queue_pages(
                    new_pdf_doc, first_new_render, last_new_page,
                    final_added, ADDED_COLOR, 'page_new'
                )

                old_tail = old_words[commit_old_count:]
                new_tail = new_words[commit_new_count:]

            self.q.put(('done', f"✅ Done — {total_removed} removed · {total_added} added"))

        except Exception as e:
            self.q.put(('error', f"{type(e).__name__}: {e}"))
            print(traceback.format_exc())
        finally:
            if old_pdf_doc:
                try: old_pdf_doc.close()
                except Exception: pass
            if new_pdf_doc:
                try: new_pdf_doc.close()
                except Exception: pass

    # ─────────────────────────────────────────────────────────────────────────
    # QUEUE PROCESSOR
    # ─────────────────────────────────────────────────────────────────────────

    def process_queue(self):
        try:
            while True:
                msg_type, data = self.q.get_nowait()

                if msg_type == 'status': self._set_status(data)
                elif msg_type == 'warn': self._set_status(data, warn=True)
                elif msg_type == 'progress': self.progress['value'] = data
                elif msg_type in ('page_old', 'page_new'):
                    is_old    = (msg_type == 'page_old')
                    canvas    = self.canvas_old if is_old else self.canvas_new
                    photos    = self.old_photos  if is_old else self.new_photos
                    current_y = self.old_y       if is_old else self.new_y

                    photo = ImageTk.PhotoImage(data)
                    photos.append(photo)
                    x_pos = canvas.winfo_width() // 2

                    canvas.create_rectangle(
                        x_pos - data.width // 2 - 1, current_y - 1,
                        x_pos + data.width // 2 + 1, current_y + data.height + 1,
                        outline=BORDER, fill=BORDER)
                    canvas.create_image(x_pos, current_y, image=photo, anchor="n")

                    new_y = current_y + data.height + PAGE_GAP
                    if is_old: self.old_y = new_y
                    else:      self.new_y = new_y

                    canvas.config(scrollregion=canvas.bbox("all"))

                elif msg_type == 'done':
                    self._set_status(data)
                    self.btn_run.config(state=tk.NORMAL)
                    self.progress['value'] = 100
                    self.root.after(2000, lambda: self.progress.configure(value=0))

                elif msg_type == 'error':
                    self._set_status(f"❌ {data}", error=True)
                    self.btn_run.config(state=tk.NORMAL)
                    self.progress['value'] = 0

        except queue.Empty: pass
        self.root.after(50, self.process_queue)


if __name__ == "__main__":
    root = tk.Tk()
    app = PDFDiffApp(root)
    root.mainloop()