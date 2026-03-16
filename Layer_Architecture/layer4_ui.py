"""
Layer 4 — Synchronized Dual-Pane Viewer
=========================================
UI is identical to the working script.
Internal structure uses clean 4-layer imports.

THREADING
----------
Single background thread runs the chunked pipeline.
pdfium.PdfDocument opened once per PDF, kept alive for entire run.
No parallel rendering (avoids pypdfium2 thread-safety issues).

SCROLL SYNC
------------
Fractional sync: yview()[0] fraction mirrors JS scrollTop/scrollHeight.
rAF-style debounce via after(0) prevents ping-pong.
"""

import math
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from pathlib import Path

import pypdfium2 as pdfium
from PIL import ImageTk

from layer1_extraction import extract_page_range, get_page_count
from layer2_diff        import find_safe_seam, diff_words
from layer3_paint       import render_page_range, REMOVED_COLOR, ADDED_COLOR, RENDER_SCALE

# ── Constants ─────────────────────────────────────────────────────────────────
CHUNK_PAGES = 10

# ── Theme ─────────────────────────────────────────────────────────────────────
BG       = "#0D1117"
SURFACE  = "#161B22"
SURFACE2 = "#21262D"
BORDER   = "#30363D"
TEXT     = "#E6EDF3"
SUBTEXT  = "#8B949E"
ACCENT   = "#58A6FF"


# ── Background pipeline ───────────────────────────────────────────────────────

def pipeline(old_path: str, new_path: str,
             q: queue.Queue, notify_fn) -> None:
    """
    Chunked diff pipeline running on a background thread.

    For each chunk:
      1. Extract words (Layer 1 — pdfplumber, 0-based pages)
      2. Find safe seam + commit (Layer 2)
      3. Diff committed words (Layer 2)
      4. Render committed pages with highlights (Layer 3 — pdfium)
      5. Post each rendered page image to the queue
    """
    old_doc = new_doc = None
    try:
        # Open pdfium docs once — kept open for whole run (single thread, safe)
        old_doc = pdfium.PdfDocument(old_path)
        new_doc = pdfium.PdfDocument(new_path)

        total_old = len(old_doc)
        total_new = len(new_doc)
        total_chunks = math.ceil(max(total_old, total_new) / CHUNK_PAGES)

        old_tail: list[dict] = []
        new_tail: list[dict] = []
        total_removed = total_added = 0

        for chunk in range(total_chunks):
            # 0-based page range for this chunk
            page_start   = chunk * CHUNK_PAGES
            old_page_end = min(page_start + CHUNK_PAGES - 1, total_old - 1)
            new_page_end = min(page_start + CHUNK_PAGES - 1, total_new - 1)
            is_last      = (chunk == total_chunks - 1)

            pct = int(5 + (chunk / total_chunks) * 90)
            q.put(('progress', pct));       notify_fn()
            q.put(('status',
                   f"Pages {page_start+1}–{max(old_page_end, new_page_end)+1} "
                   f"of {max(total_old, total_new)}  "
                   f"(chunk {chunk+1}/{total_chunks})")); notify_fn()

            # ── Layer 1: Extract ──────────────────────────────────────
            old_chunk = extract_page_range(old_path, page_start, old_page_end) \
                        if page_start <= total_old - 1 else []
            new_chunk = extract_page_range(new_path, page_start, new_page_end) \
                        if page_start <= total_new - 1 else []

            old_words = old_tail + old_chunk
            new_words = new_tail + new_chunk

            if not old_words and not new_words:
                continue

            # ── Layer 2a: Safe seam ───────────────────────────────────
            commit_old = len(old_words)
            commit_new = len(new_words)

            if not is_last:
                seam = find_safe_seam(old_words, new_words)
                if seam:
                    commit_old = seam['old_seam']
                    commit_new = seam['new_seam']
                elif old_chunk or new_chunk:
                    # No seam found — carry entire chunk forward
                    old_tail, new_tail = old_words, new_words
                    continue

            old_commit = old_words[:commit_old]
            new_commit = new_words[:commit_new]

            if not old_commit and not new_commit:
                continue

            # ── Layer 2b: Diff ────────────────────────────────────────
            removed_words, added_words = diff_words(old_commit, new_commit)
            total_removed += len(removed_words)
            total_added   += len(added_words)

            # ── Layer 3: Render committed pages ───────────────────────
            last_old_page = old_commit[-1]['page'] if old_commit else page_start - 1
            last_new_page = new_commit[-1]['page'] if new_commit else page_start - 1

            first_old_render = old_tail[0]['page'] if old_tail else page_start
            first_new_render = new_tail[0]['page'] if new_tail else page_start

            for _p, img in render_page_range(
                    old_doc, first_old_render, last_old_page,
                    removed_words, REMOVED_COLOR):
                q.put(('page_old', img)); notify_fn()

            for _p, img in render_page_range(
                    new_doc, first_new_render, last_new_page,
                    added_words, ADDED_COLOR):
                q.put(('page_new', img)); notify_fn()

            # ── Carry tails ───────────────────────────────────────────
            old_tail = old_words[commit_old:]
            new_tail = new_words[commit_new:]

        q.put(('done', f"Done  —  {total_removed} removed · {total_added} added"))
        notify_fn()

    except Exception as exc:
        import traceback; traceback.print_exc()
        q.put(('error', str(exc))); notify_fn()
    finally:
        if old_doc: old_doc.close()
        if new_doc: new_doc.close()


# ── App ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("PDF Diff")
        self.geometry("1300x850")
        self.configure(bg=BG)

        self.old_file    = None
        self.new_file    = None
        self.q           = queue.Queue()
        self.is_syncing  = False

        # Image references (prevent GC)
        self.old_photos: list = []
        self.new_photos: list = []
        self.old_y = 28
        self.new_y = 28

        self._build_ui()
        self._poll_queue()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Progress bar
        style = ttk.Style()
        style.theme_use('default')
        style.configure("TProgressbar", thickness=3,
                        background=ACCENT, troughcolor=BG, borderwidth=0)
        self.progress = ttk.Progressbar(self, style="TProgressbar",
                                         orient="horizontal", mode="determinate")
        self.progress.pack(fill=tk.X, side=tk.TOP)

        # Top bar
        bar = tk.Frame(self, bg=SURFACE, height=56,
                       highlightbackground=BORDER, highlightthickness=1)
        bar.pack(fill=tk.X, side=tk.TOP)
        bar.pack_propagate(False)

        tk.Label(bar, text="⬛ PDF DIFF",
                 font=("Courier New", 14, "bold"),
                 bg=SURFACE, fg=TEXT).pack(side=tk.LEFT, padx=(20, 10), pady=15)

        btn_kw = dict(bg=SURFACE2, fg="#C9D1D9", font=("Arial", 10),
                      relief=tk.FLAT, padx=14, pady=6, cursor="hand2",
                      highlightbackground=BORDER, highlightthickness=1,
                      activebackground=BORDER)

        self.btn_old = tk.Button(bar, text="📂 Old PDF", **btn_kw,
                                  command=self._pick_old)
        self.btn_old.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_new = tk.Button(bar, text="📂 New PDF", **btn_kw,
                                  command=self._pick_new)
        self.btn_new.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_run = tk.Button(bar, text="▶ Compare",
                                  bg="#238636", fg="white",
                                  font=("Arial", 10, "bold"),
                                  relief=tk.FLAT, padx=18, pady=6,
                                  cursor="hand2", state=tk.DISABLED,
                                  activebackground="#2ea043",
                                  activeforeground="white",
                                  command=self._run)
        self.btn_run.pack(side=tk.LEFT, padx=10, pady=10)

        # Legend
        leg = tk.Frame(bar, bg=SURFACE)
        leg.pack(side=tk.LEFT, padx=20, pady=15)
        tk.Label(leg, text="", bg="#FFD54F", width=2, height=1).pack(side=tk.LEFT)
        tk.Label(leg, text="Removed", bg=SURFACE, fg=SUBTEXT,
                 font=("Arial", 9)).pack(side=tk.LEFT, padx=(5, 15))
        tk.Label(leg, text="", bg="#4FC3F7", width=2, height=1).pack(side=tk.LEFT)
        tk.Label(leg, text="Added", bg=SURFACE, fg=SUBTEXT,
                 font=("Arial", 9)).pack(side=tk.LEFT, padx=(5, 0))

        self.lbl_status = tk.Label(bar, text="Select both PDFs to begin.",
                                    bg=SURFACE, fg=SUBTEXT, font=("Arial", 10))
        self.lbl_status.pack(side=tk.RIGHT, padx=20)

        # Dual pane
        body = tk.Frame(self, bg=BG)
        body.pack(fill=tk.BOTH, expand=True)

        # Left pane
        left_col = tk.Frame(body, bg=BG)
        left_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lh = tk.Frame(left_col, bg=SURFACE2, height=30,
                      highlightbackground=BORDER, highlightthickness=1)
        lh.pack(fill=tk.X)
        lh.pack_propagate(False)
        tk.Label(lh, text="OLD VERSION", bg=SURFACE2, fg="#E3B341",
                 font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=14, pady=5)

        self.canvas_old = tk.Canvas(left_col, bg="#010409", highlightthickness=0)
        self.sb_old = tk.Scrollbar(left_col, orient=tk.VERTICAL,
                                    command=self.canvas_old.yview)
        self.canvas_old.configure(yscrollcommand=self._lscroll)
        self.sb_old.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas_old.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Right pane
        right_col = tk.Frame(body, bg=BG,
                              highlightbackground=BORDER, highlightthickness=1)
        right_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rh = tk.Frame(right_col, bg=SURFACE2, height=30,
                      highlightbackground=BORDER, highlightthickness=1)
        rh.pack(fill=tk.X)
        rh.pack_propagate(False)
        tk.Label(rh, text="NEW VERSION", bg=SURFACE2, fg=ACCENT,
                 font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=14, pady=5)

        self.canvas_new = tk.Canvas(right_col, bg="#010409", highlightthickness=0)
        self.sb_new = tk.Scrollbar(right_col, orient=tk.VERTICAL,
                                    command=self.canvas_new.yview)
        self.canvas_new.configure(yscrollcommand=self._rscroll)
        self.sb_new.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas_new.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Mousewheel binding
        for cv in (self.canvas_old, self.canvas_new):
            cv.bind("<MouseWheel>", self._wheel)
            cv.bind("<Button-4>",   self._wheel)
            cv.bind("<Button-5>",   self._wheel)

    # ── Scroll sync (fractional, rAF-style debounce) ─────────────────────────

    def _lscroll(self, lo, hi):
        self.sb_old.set(lo, hi)
        if not self.is_syncing:
            self.is_syncing = True
            self.canvas_new.yview_moveto(float(lo))
            self.after(0, self._clear_sync)

    def _rscroll(self, lo, hi):
        self.sb_new.set(lo, hi)
        if not self.is_syncing:
            self.is_syncing = True
            self.canvas_old.yview_moveto(float(lo))
            self.after(0, self._clear_sync)

    def _clear_sync(self):
        self.is_syncing = False

    def _wheel(self, event):
        if self.is_syncing:
            return "break"
        self.is_syncing = True

        if   event.num == 4: delta = -1
        elif event.num == 5: delta =  1
        else:
            delta = int(-1 * (event.delta / 120))
            if delta == 0:
                delta = -1 if event.delta > 0 else 1

        self.canvas_old.yview_scroll(delta, "units")
        self.canvas_new.yview_scroll(delta, "units")
        # Sync fractions after scroll
        lo, _ = self.canvas_old.yview()
        self.canvas_new.yview_moveto(lo)

        self.after(0, self._clear_sync)
        return "break"

    # ── File picking ──────────────────────────────────────────────────────────

    def _pick_old(self):
        p = filedialog.askopenfilename(
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if not p: return
        self.old_file = p
        name = Path(p).name
        short = name[:19] + "…" if len(name) > 22 else name
        self.btn_old.config(text=f"📄 {short}", fg=ACCENT,
                             highlightbackground=ACCENT)
        self._check_ready()

    def _pick_new(self):
        p = filedialog.askopenfilename(
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if not p: return
        self.new_file = p
        name = Path(p).name
        short = name[:19] + "…" if len(name) > 22 else name
        self.btn_new.config(text=f"📄 {short}", fg=ACCENT,
                             highlightbackground=ACCENT)
        self._check_ready()

    def _check_ready(self):
        if self.old_file and self.new_file:
            self.btn_run.config(state=tk.NORMAL)
            self.lbl_status.config(text="Ready — click ▶ Compare.")

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run(self):
        self.btn_run.config(state=tk.DISABLED)
        self.canvas_old.delete("all")
        self.canvas_new.delete("all")
        self.old_photos.clear()
        self.new_photos.clear()
        self.old_y = 28
        self.new_y = 28
        self.progress['value'] = 2
        self.update_idletasks()

        self.q = queue.Queue()
        threading.Thread(
            target=pipeline,
            args=(self.old_file, self.new_file, self.q, self._notify),
            daemon=True,
        ).start()

    def _notify(self):
        """Called from background thread — wake Tkinter event loop."""
        self.event_generate('<<PipelineUpdate>>', when='tail')

    # ── Queue drain ───────────────────────────────────────────────────────────

    def _poll_queue(self):
        """Drain the queue on a 50ms timer (fallback for missed events)."""
        self._drain()
        self.after(50, self._poll_queue)

    def _drain(self):
        try:
            while True:
                msg_type, data = self.q.get_nowait()
                self._handle(msg_type, data)
        except queue.Empty:
            pass

    def _handle(self, msg_type: str, data):
        if msg_type == 'status':
            self.lbl_status.config(text=data)

        elif msg_type == 'progress':
            self.progress['value'] = data

        elif msg_type in ('page_old', 'page_new'):
            is_old  = (msg_type == 'page_old')
            canvas  = self.canvas_old if is_old else self.canvas_new
            photos  = self.old_photos  if is_old else self.new_photos
            cur_y   = self.old_y       if is_old else self.new_y

            photo = ImageTk.PhotoImage(data)
            photos.append(photo)

            cx = canvas.winfo_width() // 2
            # Drop shadow / border rect
            canvas.create_rectangle(
                cx - data.width // 2 - 1, cur_y - 1,
                cx + data.width // 2 + 1, cur_y + data.height + 1,
                outline=BORDER, fill=BORDER)
            canvas.create_image(cx, cur_y, image=photo, anchor="n")

            new_y = cur_y + data.height + 28
            if is_old: self.old_y = new_y
            else:      self.new_y = new_y

            canvas.config(scrollregion=canvas.bbox("all"))

        elif msg_type == 'done':
            self.lbl_status.config(text=data)
            self.btn_run.config(state=tk.NORMAL)
            self.progress['value'] = 100
            self.after(2000, lambda: self.progress.configure(value=0))

        elif msg_type == 'error':
            self.lbl_status.config(text=f"Error: {data}")
            self.btn_run.config(state=tk.NORMAL)
            self.progress['value'] = 0


if __name__ == '__main__':
    App().mainloop()