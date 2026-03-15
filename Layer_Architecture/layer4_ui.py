"""
Layer 4 — Synchronized Dual-Pane Viewer  (Zero-Latency Architecture)
======================================================================

THE REAL REASON previous versions still felt like "wait until done"
--------------------------------------------------------------------
Every previous fix used after(N, _poll) with N >= 16ms.

Background thread renders page in ~50-200ms, calls q.put() instantly.
But main thread is asleep in after() and won't wake for another 0-50ms.
On a fast machine with small PDFs, bg renders ALL pages before the
first poll fires. User sees: nothing... nothing... ALL pages at once.

JS HAS NO POLL INTERVAL
------------------------
JS `await` fires the continuation THE INSTANT the promise resolves.
Zero scheduler latency. The browser repaints between each page because
the microtask queue drains before the next render frame.

THE FIX: event_generate instead of after()
------------------------------------------
bg thread calls root.event_generate('<<PageReady>>', when='tail')
after every single q.put(). This wakes Tkinter IMMEDIATELY — typically
within 1ms — not on a 16-50ms timer.

_on_page_ready() draws ONE page and returns. Tkinter repaints.
Next <<PageReady>> arrives ~render-time later and draws the next page.

This is structurally identical to JS:
  render page → await (browser repaints) → render next page
  render page → event_generate (Tkinter repaints) → render next page
"""

import io
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from pathlib import Path

from PIL import Image, ImageTk, ImageDraw
import pypdfium2 as pdfium

from layer1_extraction import extract_page_range, get_page_count, WordObject
from layer2_diff        import find_safe_seam, diff_segment
from layer3_paint       import COLOR_REMOVED, COLOR_ADDED

CHUNK_PAGES   = 10
DISPLAY_WIDTH = 560
RENDER_SCALE  = 2.0

BG          = "#0D1117"
SURFACE     = "#161B22"
SURFACE2    = "#21262D"
BORDER      = "#30363D"
TEXT        = "#E6EDF3"
SUBTEXT     = "#8B949E"
ACCENT      = "#58A6FF"
REMOVED_COL = "#E3B341"
ADDED_COL   = "#58A6FF"
PANE_BG     = "#010409"


# ── Messages ──────────────────────────────────────────────────────────────────

class MsgProgress:
    def __init__(self, text, pct):
        self.text = text
        self.pct  = pct

class MsgPage:
    def __init__(self, side, jpeg_bytes, height, removed_count, added_count):
        self.side          = side
        self.jpeg_bytes    = jpeg_bytes
        self.height        = height
        self.removed_count = removed_count
        self.added_count   = added_count

class MsgDone:
    def __init__(self, removed, added, unchanged, old_pages, new_pages):
        self.removed   = removed
        self.added     = added
        self.unchanged = unchanged
        self.old_pages = old_pages
        self.new_pages = new_pages

class MsgError:
    def __init__(self, text):
        self.text = text


# ── Page renderer ─────────────────────────────────────────────────────────────

def _render_page_to_jpeg(pdf_path, page_num, highlight_words, color):
    doc    = pdfium.PdfDocument(pdf_path)
    page   = doc[page_num - 1]
    bitmap = page.render(scale=RENDER_SCALE)
    img    = bitmap.to_pil().convert("RGBA")
    iw, ih = img.size

    if highlight_words:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw    = ImageDraw.Draw(overlay)
        for word in highlight_words:
            px0 = int(word.bbox.x0 * RENDER_SCALE)
            py0 = int(word.bbox.y0 * RENDER_SCALE)
            px1 = int(word.bbox.x1 * RENDER_SCALE)
            py1 = int(word.bbox.y1 * RENDER_SCALE)
            pad = max(1, int(1.5 * RENDER_SCALE))
            draw.rectangle([
                max(0, px0 - pad), max(0, py0 - pad),
                min(iw, px1 + pad), min(ih, py1 + pad)
            ], fill=color)
        img = Image.alpha_composite(img, overlay)

    img = img.convert("RGB")
    dh  = int(ih * DISPLAY_WIDTH / iw)
    img = img.resize((DISPLAY_WIDTH, dh), Image.BILINEAR)
    page.close()
    doc.close()

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue(), dh


# ── Pipeline ──────────────────────────────────────────────────────────────────

def pipeline(old_path, new_path, q, notify_fn):
    """
    notify_fn() is called after every q.put().
    It calls event_generate on the root window — wakes Tkinter instantly.
    """
    def put(msg):
        q.put(msg)
        notify_fn()

    try:
        old_total    = get_page_count(old_path)
        new_total    = get_page_count(new_path)
        max_pages    = max(old_total, new_total)
        total_chunks = (max_pages + CHUNK_PAGES - 1) // CHUNK_PAGES

        old_tail = []
        new_tail = []
        old_off = new_off = 0
        total_removed = total_added = total_unchanged = 0

        for chunk in range(total_chunks):
            p_start = chunk * CHUNK_PAGES + 1
            old_end = min(p_start + CHUNK_PAGES - 1, old_total)
            new_end = min(p_start + CHUNK_PAGES - 1, new_total)
            is_last = (chunk == total_chunks - 1)

            pct = int(5 + chunk / total_chunks * 88)
            put(MsgProgress(
                f"Pages {p_start}-{max(old_end,new_end)} of {max_pages} "
                f"(chunk {chunk+1}/{total_chunks})", pct))

            old_chunk = extract_page_range(old_path, p_start, old_end, old_off) \
                        if p_start <= old_total else []
            new_chunk = extract_page_range(new_path, p_start, new_end, new_off) \
                        if p_start <= new_total else []
            old_off += len(old_chunk)
            new_off += len(new_chunk)

            old_words = old_tail + old_chunk
            new_words = new_tail + new_chunk
            if not old_words and not new_words:
                continue

            if not is_last:
                seam = find_safe_seam(old_words, new_words)
                if not seam.found:
                    old_tail, new_tail = old_words, new_words
                    continue
                old_commit = old_words[:seam.old_seam]
                new_commit = new_words[:seam.new_seam]
                old_tail   = old_words[seam.old_seam:]
                new_tail   = new_words[seam.new_seam:]
            else:
                old_commit, new_commit = old_words, new_words
                old_tail = new_tail = []

            if not old_commit and not new_commit:
                continue

            seg = diff_segment(old_commit, new_commit)
            total_removed   += len(seg.removed_words)
            total_added     += len(seg.added_words)
            total_unchanged += seg.unchanged

            old_by_page = {}
            for w in old_commit:
                if w in seg.removed_words:
                    old_by_page.setdefault(w.page_number, []).append(w)

            new_by_page = {}
            for w in new_commit:
                if w in seg.added_words:
                    new_by_page.setdefault(w.page_number, []).append(w)

            old_pages = list(range(old_commit[0].page_number,
                                   old_commit[-1].page_number + 1)) if old_commit else []
            new_pages = list(range(new_commit[0].page_number,
                                   new_commit[-1].page_number + 1)) if new_commit else []

            # ── ONE PAGE AT A TIME, notify after each ─────────────────
            # After put(), notify_fn() fires event_generate immediately.
            # Main thread draws this page and returns to event loop.
            # Tkinter repaints. Then we render the next page.
            # Structurally identical to JS's await-per-page loop.
            for i in range(max(len(old_pages), len(new_pages))):
                if i < len(old_pages):
                    pg = old_pages[i]
                    if pg <= old_total:
                        jpeg, h = _render_page_to_jpeg(
                            old_path, pg, old_by_page.get(pg, []), COLOR_REMOVED)
                        put(MsgPage('old', jpeg, h,
                                    len(seg.removed_words), len(seg.added_words)))

                if i < len(new_pages):
                    pg = new_pages[i]
                    if pg <= new_total:
                        jpeg, h = _render_page_to_jpeg(
                            new_path, pg, new_by_page.get(pg, []), COLOR_ADDED)
                        put(MsgPage('new', jpeg, h,
                                    len(seg.removed_words), len(seg.added_words)))

            del old_commit, new_commit, old_chunk, new_chunk

        put(MsgDone(total_removed, total_added, total_unchanged,
                    old_total, new_total))

    except Exception as exc:
        import traceback; traceback.print_exc()
        put(MsgError(str(exc)))


# ── PDFPane ───────────────────────────────────────────────────────────────────

class PDFPane(tk.Frame):
    def __init__(self, master, label, label_color, **kw):
        super().__init__(master, bg=BG, **kw)
        self._imgs    = []
        self._next_y  = 12
        self._GAP     = 10
        self._count   = 0
        self._sync_cb = None

        hdr = tk.Frame(self, bg=SURFACE2, height=30)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text=f"  {label}", bg=SURFACE2, fg=label_color,
                 font=("Courier", 10, "bold")).pack(side="left", padx=8)
        self._lbl = tk.Label(hdr, text="", bg=SURFACE2, fg=SUBTEXT,
                             font=("Helvetica", 9))
        self._lbl.pack(side="right", padx=10)

        wrap = tk.Frame(self, bg=PANE_BG)
        wrap.pack(fill="both", expand=True)

        self._sb = tk.Scrollbar(wrap, orient="vertical", bg=SURFACE2,
                                troughcolor=SURFACE2, activebackground=ACCENT)
        self._sb.pack(side="right", fill="y")

        self._cv = tk.Canvas(wrap, bg=PANE_BG, highlightthickness=0,
                             yscrollcommand=self._on_scroll)
        self._cv.pack(side="left", fill="both", expand=True)
        self._sb.config(command=self._cv.yview)
        self._cv.bind("<MouseWheel>", self._wheel)
        self._cv.bind("<Button-4>",   self._wheel)
        self._cv.bind("<Button-5>",   self._wheel)

    def clear(self):
        self._cv.delete("all")
        self._imgs.clear()
        self._next_y = 12
        self._count  = 0
        self._cv.config(scrollregion=(0, 0, DISPLAY_WIDTH, 1))
        self._lbl.config(text="")

    def append_page(self, jpeg_bytes, height):
        img   = Image.open(io.BytesIO(jpeg_bytes))
        photo = ImageTk.PhotoImage(img)
        self._imgs.append(photo)
        self._cv.create_image(0, self._next_y, anchor="nw", image=photo)

        # Save the current scroll position in PIXELS before we grow the canvas.
        # Tkinter normally preserves scroll *fraction* when scrollregion changes,
        # which shifts existing pages as the total height grows. Saving and
        # restoring the pixel offset keeps already-visible pages rock-steady.
        old_total  = max(self._next_y, 1)
        top_frac   = self._cv.yview()[0]
        top_pixels = top_frac * old_total          # where the viewport top is now

        self._next_y += height + self._GAP
        self._count  += 1
        self._cv.config(scrollregion=(0, 0, DISPLAY_WIDTH, self._next_y))

        # Restore exact pixel position so nothing moves
        self._cv.yview_moveto(top_pixels / self._next_y)
        self._lbl.config(text=f"{self._count} pages")

    def _on_scroll(self, lo, hi):
        self._sb.set(lo, hi)
        if self._sync_cb:
            # Pass absolute pixel offset — fractions shift during loading
            top_pixels = float(lo) * self._next_y
            self._sync_cb(top_pixels)

    def _wheel(self, e):
        self._cv.yview_scroll(-2 if (e.num == 4 or e.delta > 0) else 2, "units")

    def set_sync(self, cb):  self._sync_cb = cb

    def goto_pixels(self, px):
        """Scroll to absolute pixel offset. Stable regardless of canvas height."""
        if self._next_y > 0:
            self._cv.yview_moveto(px / self._next_y)


# ── App ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF Diff")
        self.geometry("1200x820")
        self.minsize(800, 500)
        self.configure(bg=BG)

        self._old_path = None
        self._new_path = None
        self._queue    = None
        self._syncing  = False
        self._removed  = 0
        self._added    = 0
        self._draining = False

        # Virtual event — bg thread fires this to wake Tkinter with zero latency
        self.bind('<<PageReady>>', self._on_page_ready)
        self._build()

    def _notify(self):
        """Thread-safe. Wakes Tkinter event loop immediately."""
        self.event_generate('<<PageReady>>', when='tail')

    def _on_page_ready(self, _=None):
        """
        Draw ONE page and return so Tkinter can repaint.
        Re-entrant calls are blocked by _draining flag.

        This is the Python equivalent of JS's await continuation:
        fires instantly when a page is ready, draws it, returns.
        """
        if self._draining:
            return
        self._draining = True

        try:
            msg = self._queue.get_nowait()
        except queue.Empty:
            self._draining = False
            return

        if isinstance(msg, MsgProgress):
            self._status.set(msg.text)
            self._prog_var.set(msg.pct)
            self._draining = False
            self._on_page_ready()   # progress is cheap, get next message

        elif isinstance(msg, MsgPage):
            pane = self._left if msg.side == 'old' else self._right
            pane.append_page(msg.jpeg_bytes, msg.height)
            if msg.side == 'old':
                self._removed = msg.removed_count
            else:
                self._added = msg.added_count
            self._stats.set(f"-{self._removed}  +{self._added}")
            # STOP HERE — return to event loop so Tkinter repaints
            self._draining = False

        elif isinstance(msg, MsgDone):
            self._prog.pack_forget()
            self._prog_var.set(0)
            self._run_btn.config(state="normal")
            self._status.set("Done  scroll either pane to sync both.")
            self._stats.set(
                f"Old: {msg.old_pages}p  New: {msg.new_pages}p    "
                f"-{msg.removed}  +{msg.added}  ={msg.unchanged}")
            self._draining = False

        elif isinstance(msg, MsgError):
            self._prog.pack_forget()
            self._run_btn.config(state="normal")
            self._status.set(f"Error: {msg.text}")
            self._draining = False

    def _build(self):
        bar = tk.Frame(self, bg=SURFACE, height=50)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tk.Label(bar, text="  PDF DIFF", bg=SURFACE, fg=TEXT,
                 font=("Courier", 13, "bold")).pack(side="left", pady=12)

        b = dict(bg=SURFACE2, fg=SUBTEXT, relief="flat", font=("Helvetica", 9),
                 cursor="hand2", activebackground=BORDER, activeforeground=TEXT,
                 padx=10, pady=3)

        self._old_btn = tk.Button(bar, text="Old PDF", **b,
                                  command=lambda: self._pick("old"))
        self._old_btn.pack(side="left", padx=(18, 4), pady=10)

        self._new_btn = tk.Button(bar, text="New PDF", **b,
                                  command=lambda: self._pick("new"))
        self._new_btn.pack(side="left", padx=4, pady=10)

        self._run_btn = tk.Button(
            bar, text="Compare",
            bg=ACCENT, fg="#0D1117", relief="flat",
            font=("Helvetica", 10, "bold"), cursor="hand2",
            activebackground="#79B8FF", padx=14, pady=3,
            command=self._run, state="disabled")
        self._run_btn.pack(side="left", padx=(10, 0), pady=10)

        self._status = tk.StringVar(value="Select both PDF files to begin.")
        tk.Label(bar, textvariable=self._status,
                 bg=SURFACE, fg=SUBTEXT, font=("Helvetica", 9)
                 ).pack(side="left", padx=14)

        self._stats = tk.StringVar(value="")
        tk.Label(bar, textvariable=self._stats,
                 bg=SURFACE, fg=SUBTEXT, font=("Helvetica", 9)
                 ).pack(side="right", padx=14)

        self._prog_var = tk.DoubleVar(value=0)
        self._prog = ttk.Progressbar(self, variable=self._prog_var, maximum=100)
        s = ttk.Style(self)
        s.theme_use("default")
        s.configure("TProgressbar", troughcolor=SURFACE2, background=ACCENT, thickness=3)

        body = tk.Frame(self, bg=BORDER)
        body.pack(fill="both", expand=True)

        self._left  = PDFPane(body, "OLD VERSION", REMOVED_COL)
        self._left.pack(side="left", fill="both", expand=True)
        tk.Frame(body, bg=BORDER, width=2).pack(side="left", fill="y")
        self._right = PDFPane(body, "NEW VERSION", ADDED_COL)
        self._right.pack(side="right", fill="both", expand=True)

        self._left.set_sync(self._lsync)
        self._right.set_sync(self._rsync)

        leg = tk.Frame(self, bg=SURFACE2, height=28)
        leg.pack(fill="x", side="bottom")
        leg.pack_propagate(False)
        for col, lbl in [(REMOVED_COL, "Removed"), (ADDED_COL, "Added")]:
            row = tk.Frame(leg, bg=SURFACE2)
            row.pack(side="left", padx=14, pady=6)
            tk.Label(row, text="  ", bg=col, width=2).pack(side="left", padx=(0, 5))
            tk.Label(row, text=lbl, bg=SURFACE2, fg=TEXT,
                     font=("Helvetica", 9)).pack(side="left")
        tk.Label(leg, text="Event-driven: pages appear instantly as rendered",
                 bg=SURFACE2, fg=SUBTEXT, font=("Helvetica", 8)
                 ).pack(side="right", padx=14)

    def _pick(self, side):
        p = filedialog.askopenfilename(
            title=f"Select {'Old' if side=='old' else 'New'} PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if not p:
            return
        name  = Path(p).name
        short = name[:20] + "..." if len(name) > 20 else name
        if side == "old":
            self._old_path = p
            self._old_btn.config(text=short, fg=REMOVED_COL)
        else:
            self._new_path = p
            self._new_btn.config(text=short, fg=ADDED_COL)
        if self._old_path and self._new_path:
            self._run_btn.config(state="normal")
            self._status.set("Ready  click Compare.")

    def _run(self):
        self._run_btn.config(state="disabled")
        self._left.clear()
        self._right.clear()
        self._stats.set("")
        self._removed = 0
        self._added   = 0
        self._prog.pack(fill="x")
        self._prog_var.set(0)

        self._queue = queue.Queue()
        threading.Thread(
            target=pipeline,
            args=(self._old_path, self._new_path, self._queue, self._notify),
            daemon=True
        ).start()

    def _lsync(self, px):
        if self._syncing: return
        self._syncing = True
        self._right.goto_pixels(px)
        self._syncing = False

    def _rsync(self, px):
        if self._syncing: return
        self._syncing = True
        self._left.goto_pixels(px)
        self._syncing = False


if __name__ == "__main__":
    App().mainloop()