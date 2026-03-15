"""
Layer 4 — Synchronized Dual-Pane Viewer  (Progressive Fix)
============================================================
The HTML version shows pages immediately because every `await` in the
for-loop is a genuine browser repaint yield.

The fix for Python:

  PROBLEM: The original pipeline() built ALL images for a chunk (paint is
  slow: 2× scale PIL rendering) then put ONE MsgChunk. But more critically,
  find_safe_seam + diff_segment + painting all blocked before anything was
  queued — identical to the HTML version never yielding.

  FIX: Split the background work into two stages per chunk:
    1. Extract + seam + diff  → queue MsgChunkReady (lightweight signal)
    2. Paint                  → queue MsgChunk (with images)

  But the REAL fix is simpler: the painting itself is fine where it is.
  The actual bug is that _poll() was correct but the pipeline was not
  yielding between chunks fast enough because Python's GIL means the
  background thread and Tkinter compete. The solution is to increase
  POLL_MS aggressiveness and ensure the background thread yields between
  the heavy paint step and the queue.put() — using a small sleep(0) to
  let the OS scheduler give the main thread a chance to run.

  Additionally: pre-resize images on the bg thread (already done) but
  use JPEG bytes transfer instead of PIL objects to cut cross-thread
  memory overhead on large pages.
"""

import io
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from pathlib import Path

from PIL import Image, ImageTk

from layer1_extraction import extract_page_range, get_page_count
from layer2_diff        import find_safe_seam, diff_segment
from layer3_paint       import paint_page_range_to_images, COLOR_REMOVED, COLOR_ADDED

# ── Constants ─────────────────────────────────────────────────────────────────
CHUNK_PAGES   = 10    # pages per processing batch
POLL_MS       = 30    # ms between queue polls (lowered from 50 → snappier)
DISPLAY_WIDTH = 560   # pane width; images pre-resized to this on bg thread

# ── Colors ────────────────────────────────────────────────────────────────────
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


# ── Queue message types ───────────────────────────────────────────────────────

class MsgProgress:
    def __init__(self, text, pct):
        self.text = text
        self.pct  = pct

class MsgChunk:
    """
    One chunk of display-ready images, transferred as JPEG bytes.
    Encoding to JPEG on the bg thread and decoding on the main thread
    is faster than passing large PIL objects across threads because it
    reduces the amount of data the main thread has to touch.
    """
    def __init__(self, old_jpegs, new_jpegs, removed, added):
        # old_jpegs / new_jpegs: list of (page_num, jpeg_bytes, w, h)
        self.old_jpegs = old_jpegs
        self.new_jpegs = new_jpegs
        self.removed   = removed
        self.added     = added

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


# ── PDFPane ───────────────────────────────────────────────────────────────────

class PDFPane(tk.Frame):
    """Single scrollable pane. append_images() adds pages live."""

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

    def append_jpegs(self, jpeg_list):
        """
        Decode JPEG bytes and draw onto canvas.
        Decoding is fast — the heavy PIL rendering was done on bg thread.
        """
        for _, jpeg_bytes, w, h in jpeg_list:
            img   = Image.open(io.BytesIO(jpeg_bytes))
            photo = ImageTk.PhotoImage(img)
            self._imgs.append(photo)          # keep reference alive
            self._cv.create_image(0, self._next_y, anchor="nw", image=photo)
            self._next_y += h + self._GAP
            self._count  += 1

        self._cv.config(scrollregion=(0, 0, DISPLAY_WIDTH, self._next_y))
        self._lbl.config(text=f"{self._count} pages")

    def _on_scroll(self, lo, hi):
        self._sb.set(lo, hi)
        if self._sync_cb:
            self._sync_cb(float(lo))

    def _wheel(self, e):
        self._cv.yview_scroll(-2 if (e.num == 4 or e.delta > 0) else 2, "units")

    def set_sync(self, cb):  self._sync_cb = cb
    def goto(self, frac):    self._cv.yview_moveto(frac)
    def fraction(self):      return float(self._cv.yview()[0])


# ── Background pipeline ───────────────────────────────────────────────────────

def _to_jpeg(page_images):
    """
    Resize to DISPLAY_WIDTH and encode as JPEG bytes.
    Returns list of (page_num, jpeg_bytes, width, height).
    Called on background thread only.
    """
    out = []
    for page_num, img in page_images:
        h = int(img.height * DISPLAY_WIDTH / img.width)
        resized = img.resize((DISPLAY_WIDTH, h), Image.BILINEAR)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=88)
        out.append((page_num, buf.getvalue(), DISPLAY_WIDTH, h))
    return out


def pipeline(old_path, new_path, q: queue.Queue):
    """
    Background thread. Identical logic to the original but:

    1. Uses _to_jpeg() instead of raw PIL — smaller objects cross thread boundary
    2. Calls q.put(MsgChunk(...)) as soon as a chunk is painted, before
       starting the next chunk's extraction. This is the key change:
       the main thread gets the chunk immediately and can repaint while
       the background thread is already working on the next extraction.
    3. The pipeline sleeps(0) after each q.put to yield the GIL and let
       Tkinter's after() callbacks actually fire.
    """
    import time

    try:
        old_total    = get_page_count(old_path)
        new_total    = get_page_count(new_path)
        max_pages    = max(old_total, new_total)
        total_chunks = (max_pages + CHUNK_PAGES - 1) // CHUNK_PAGES

        old_tail = [];  new_tail = []
        old_off  = 0;   new_off  = 0
        total_removed = total_added = total_unchanged = 0

        for chunk in range(total_chunks):
            p_start = chunk * CHUNK_PAGES + 1
            old_end = min(p_start + CHUNK_PAGES - 1, old_total)
            new_end = min(p_start + CHUNK_PAGES - 1, new_total)
            is_last = (chunk == total_chunks - 1)

            pct = int(5 + chunk / total_chunks * 88)
            q.put(MsgProgress(
                f"Pages {p_start}–{max(old_end, new_end)} of {max_pages}  "
                f"(chunk {chunk+1}/{total_chunks})",
                pct
            ))

            # ── Layer 1: Extract ─────────────────────────────────────
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

            # ── Layer 2a: Safe seam ──────────────────────────────────
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

            # ── Layer 2b: Diff ───────────────────────────────────────
            seg = diff_segment(old_commit, new_commit)
            total_removed   += len(seg.removed_words)
            total_added     += len(seg.added_words)
            total_unchanged += seg.unchanged

            # ── Layer 3: Paint → JPEG bytes ──────────────────────────
            fo = old_commit[0].page_number;  lo = old_commit[-1].page_number
            fn = new_commit[0].page_number;  ln = new_commit[-1].page_number

            old_imgs = paint_page_range_to_images(
                old_path, old_commit, seg.removed_words, COLOR_REMOVED, fo, lo)
            new_imgs = paint_page_range_to_images(
                new_path, new_commit, seg.added_words,   COLOR_ADDED,   fn, ln)

            # Encode to JPEG on bg thread — decode is trivial on main thread
            old_jpegs = _to_jpeg(old_imgs)
            new_jpegs = _to_jpeg(new_imgs)

            # ── Queue chunk ──────────────────────────────────────────
            # Put the chunk BEFORE starting next iteration so Tkinter
            # can begin rendering while we extract the next chunk.
            q.put(MsgChunk(
                old_jpegs,
                new_jpegs,
                len(seg.removed_words),
                len(seg.added_words),
            ))

            # Yield GIL so Tkinter's after(POLL_MS, _poll) can fire.
            # Without this sleep(0), the bg thread can monopolise the GIL
            # and the main thread's poll callback never runs until the
            # entire pipeline finishes — identical to the "wait until done"
            # behaviour you observed.
            time.sleep(0)

            del old_commit, new_commit, old_chunk, new_chunk
            del old_imgs, new_imgs, old_jpegs, new_jpegs

        q.put(MsgDone(total_removed, total_added, total_unchanged,
                      old_total, new_total))

    except Exception as exc:
        import traceback; traceback.print_exc()
        q.put(MsgError(str(exc)))


# ── Main application ──────────────────────────────────────────────────────────

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
        self._poll_id  = None
        self._syncing  = False

        self._build()

    def _build(self):
        # top bar
        bar = tk.Frame(self, bg=SURFACE, height=50)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tk.Label(bar, text="  PDF DIFF", bg=SURFACE, fg=TEXT,
                 font=("Courier", 13, "bold")).pack(side="left", pady=12)

        b = dict(bg=SURFACE2, fg=SUBTEXT, relief="flat", font=("Helvetica", 9),
                 cursor="hand2", activebackground=BORDER, activeforeground=TEXT,
                 padx=10, pady=3)

        self._old_btn = tk.Button(bar, text="📂 Old PDF", **b,
                                  command=lambda: self._pick("old"))
        self._old_btn.pack(side="left", padx=(18, 4), pady=10)

        self._new_btn = tk.Button(bar, text="📂 New PDF", **b,
                                  command=lambda: self._pick("new"))
        self._new_btn.pack(side="left", padx=4, pady=10)

        self._run_btn = tk.Button(
            bar, text="▶  Compare",
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

        # progress bar (hidden until run)
        self._prog_var = tk.DoubleVar(value=0)
        self._prog = ttk.Progressbar(self, variable=self._prog_var, maximum=100)
        s = ttk.Style(self); s.theme_use("default")
        s.configure("TProgressbar", troughcolor=SURFACE2,
                    background=ACCENT, thickness=3)

        # dual panes
        body = tk.Frame(self, bg=BORDER)
        body.pack(fill="both", expand=True)

        self._left  = PDFPane(body, "OLD VERSION", REMOVED_COL)
        self._left.pack(side="left", fill="both", expand=True)
        tk.Frame(body, bg=BORDER, width=2).pack(side="left", fill="y")
        self._right = PDFPane(body, "NEW VERSION", ADDED_COL)
        self._right.pack(side="right", fill="both", expand=True)

        self._left.set_sync(self._lsync)
        self._right.set_sync(self._rsync)

        # legend
        leg = tk.Frame(self, bg=SURFACE2, height=28)
        leg.pack(fill="x", side="bottom")
        leg.pack_propagate(False)
        for col, lbl in [(REMOVED_COL, "Removed"), (ADDED_COL, "Added")]:
            row = tk.Frame(leg, bg=SURFACE2)
            row.pack(side="left", padx=14, pady=6)
            tk.Label(row, text="  ", bg=col, width=2).pack(side="left", padx=(0, 5))
            tk.Label(row, text=lbl, bg=SURFACE2, fg=TEXT,
                     font=("Helvetica", 9)).pack(side="left")
        tk.Label(leg,
                 text=f"{CHUNK_PAGES} pages per chunk — pages appear as each chunk completes",
                 bg=SURFACE2, fg=SUBTEXT, font=("Helvetica", 8)
                 ).pack(side="right", padx=14)

    def _pick(self, side):
        p = filedialog.askopenfilename(
            title=f"Select {'Old' if side=='old' else 'New'} PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if not p: return
        name  = Path(p).name
        short = name[:20] + "…" if len(name) > 20 else name
        if side == "old":
            self._old_path = p
            self._old_btn.config(text=f"📄 {short}", fg=REMOVED_COL)
        else:
            self._new_path = p
            self._new_btn.config(text=f"📄 {short}", fg=ADDED_COL)
        if self._old_path and self._new_path:
            self._run_btn.config(state="normal")
            self._status.set("Ready — click ▶ Compare.")

    def _run(self):
        self._run_btn.config(state="disabled")
        self._left.clear()
        self._right.clear()
        self._stats.set("")
        self._prog.pack(fill="x")
        self._prog_var.set(0)

        if self._poll_id:
            self.after_cancel(self._poll_id)
            self._poll_id = None

        self._queue = queue.Queue()
        threading.Thread(
            target=pipeline,
            args=(self._old_path, self._new_path, self._queue),
            daemon=True
        ).start()

        self._poll_id = self.after(POLL_MS, self._poll)

    def _poll(self):
        """
        Process ONE MsgChunk per call then return — Tkinter repaints in between.

        Progress messages are cheap so we drain those in a tight loop.
        Error and Done messages are terminal — no reschedule after them.
        """
        self._poll_id = None

        while True:
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                # Nothing ready — come back after POLL_MS
                self._poll_id = self.after(POLL_MS, self._poll)
                return

            if isinstance(msg, MsgProgress):
                self._status.set(msg.text)
                self._prog_var.set(msg.pct)
                # cheap — keep draining progress messages without returning

            elif isinstance(msg, MsgChunk):
                # Decode JPEG bytes and render onto both canvases
                self._left.append_jpegs(msg.old_jpegs)
                self._right.append_jpegs(msg.new_jpegs)
                self._stats.set(f"−{msg.removed}  +{msg.added}")

                # ── STOP HERE — one chunk drawn, yield to event loop ──
                # This is the Python equivalent of JS's `await renderPageRange()`.
                # Tkinter will repaint the new pages before _poll fires again.
                self._poll_id = self.after(POLL_MS, self._poll)
                return

            elif isinstance(msg, MsgDone):
                self._prog.pack_forget()
                self._prog_var.set(0)
                self._run_btn.config(state="normal")
                self._status.set("✅ Done — scroll either pane to sync both.")
                self._stats.set(
                    f"Old: {msg.old_pages}p  New: {msg.new_pages}p  │  "
                    f"−{msg.removed}  +{msg.added}  ={msg.unchanged}")
                return

            elif isinstance(msg, MsgError):
                self._prog.pack_forget()
                self._run_btn.config(state="normal")
                self._status.set(f"❌ {msg.text}")
                return

    def _lsync(self, frac):
        if self._syncing: return
        self._syncing = True
        self._right.goto(frac)
        self._syncing = False

    def _rsync(self, frac):
        if self._syncing: return
        self._syncing = True
        self._left.goto(frac)
        self._syncing = False


if __name__ == "__main__":
    App().mainloop()