import tkinter as tk
from tkinter import filedialog, ttk
import threading
import queue
import os
import difflib
import traceback

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageTk, ImageDraw

# ── Theme ──────────────────────────────────────────────────────────────────
BG            = "#0D1117"
SURFACE       = "#161B22"
SURFACE2      = "#21262D"
BORDER        = "#30363D"
TEXT          = "#E6EDF3"
SUBTEXT       = "#8B949E"
ACCENT        = "#58A6FF"
ADDED_COLOR   = (79,  195, 247, 115)
REMOVED_COLOR = (255, 213,  79, 115)
ERROR_COLOR   = "#F85149"
WARN_COLOR    = "#E3B341"

RENDER_SCALE  = 1.5
PAGE_GAP      = 28
CHUNK_PAGES   = 10

# Seam probe settings
SEQ_START     = 7   # start probe length
SEQ_HARD_MIN  = 4   # absolute minimum — below this never anchor
SEQ_AMBIG_MAX = 2   # if short match appears more than this many times → skip


# ─────────────────────────────────────────────────────────────────────────────
#  SEAM FINDER
#
#  Scans the TAIL of old_buf (last 30%) and searches for each position's
#  word sequence inside new_buf. Probes shrink from SEQ_START down to
#  SEQ_HARD_MIN. Short matches are checked for ambiguity.
#  Returns the deepest (furthest-committing) valid anchor found.
# ─────────────────────────────────────────────────────────────────────────────

def find_seam(old_words: list, new_words: list) -> dict | None:
    """
    Returns {'old_end': int, 'new_end': int} exclusive indices, or None.
    old_end / new_end = everything before these is safe to diff+render.
    """
    old_texts = [w['text'] for w in old_words]
    new_texts = [w['text'] for w in new_words]

    if not old_texts or not new_texts:
        return None

    # Index: word → [positions in new]
    new_pos: dict[str, list[int]] = {}
    for j, t in enumerate(new_texts):
        new_pos.setdefault(t, []).append(j)

    # Only scan the tail of old (last 30%)
    scan_start = max(0, int(len(old_texts) * 0.70))
    best = None

    for i in range(scan_start, len(old_texts)):
        for probe_len in range(SEQ_START, SEQ_HARD_MIN - 1, -1):
            if i + probe_len > len(old_texts):
                continue

            probe = old_texts[i : i + probe_len]
            candidates = new_pos.get(probe[0], [])
            matches = [j for j in candidates
                       if new_texts[j : j + probe_len] == probe]

            if not matches:
                continue

            # At minimum probe length: check ambiguity
            if probe_len == SEQ_HARD_MIN and len(matches) > SEQ_AMBIG_MAX:
                break   # too ambiguous at this i, advance i

            # Valid anchor — keep the one that commits the most old words
            candidate = {'old_end': i + probe_len, 'new_end': matches[0] + probe_len}
            if best is None or candidate['old_end'] > best['old_end']:
                best = candidate
            break   # found best probe_len for this i, move to next i

    return best


# ─────────────────────────────────────────────────────────────────────────────
#  APP
# ─────────────────────────────────────────────────────────────────────────────

class PDFDiffApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Pro PDF Diff Engine")
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

    # ── UI (unchanged from original) ──────────────────────────────────────────

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

        self.btn_old = tk.Button(
            top_bar, text="📂 Old PDF", bg=SURFACE2, fg="#C9D1D9",
            font=("Arial", 10), relief=tk.FLAT, command=self.select_old,
            padx=14, pady=6, cursor="hand2",
            highlightbackground=BORDER, highlightthickness=1,
            activebackground=BORDER)
        self.btn_old.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_new = tk.Button(
            top_bar, text="📂 New PDF", bg=SURFACE2, fg="#C9D1D9",
            font=("Arial", 10), relief=tk.FLAT, command=self.select_new,
            padx=14, pady=6, cursor="hand2",
            highlightbackground=BORDER, highlightthickness=1,
            activebackground=BORDER)
        self.btn_new.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_run = tk.Button(
            top_bar, text="▶ Compare", bg="#238636", fg="white",
            font=("Arial", 10, "bold"), relief=tk.FLAT, command=self.run_diff,
            padx=18, pady=6, cursor="hand2", state=tk.DISABLED,
            activebackground="#2ea043", activeforeground="white")
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
        left_header = tk.Frame(left_col, bg=SURFACE2, height=30,
                               highlightbackground=BORDER, highlightthickness=1)
        left_header.pack(fill=tk.X)
        left_header.pack_propagate(False)
        tk.Label(left_header, text="OLD VERSION", bg=SURFACE2,
                 fg="#E3B341", font=("Arial", 9, "bold")).pack(
                     side=tk.LEFT, padx=14, pady=5)

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
        right_header = tk.Frame(right_col, bg=SURFACE2, height=30,
                                highlightbackground=BORDER, highlightthickness=1)
        right_header.pack(fill=tk.X)
        right_header.pack_propagate(False)
        tk.Label(right_header, text="NEW VERSION", bg=SURFACE2,
                 fg=ACCENT, font=("Arial", 9, "bold")).pack(
                     side=tk.LEFT, padx=14, pady=5)

        self.canvas_new = tk.Canvas(right_col, bg="#010409", highlightthickness=0,
                                    yscrollcommand=self.sb_new.set)
        self.canvas_new.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for canvas in (self.canvas_old, self.canvas_new):
            canvas.bind("<MouseWheel>", self.on_mousewheel)
            canvas.bind("<Button-4>",   self.on_mousewheel)
            canvas.bind("<Button-5>",   self.on_mousewheel)

    # ── Scrolling (unchanged from original) ───────────────────────────────────

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
        if   event.num == 4: delta = -1
        elif event.num == 5: delta =  1
        else:                delta = int(-1 * (event.delta / 120))
        if event.widget == self.canvas_old:
            self.canvas_old.yview_scroll(delta, "units")
            self._scroll_canvas_to_y(
                self.canvas_new,
                float(self.canvas_old.canvasy(0)) + self.sync_offset_y)
        else:
            self.canvas_new.yview_scroll(delta, "units")
            self._scroll_canvas_to_y(
                self.canvas_old,
                float(self.canvas_new.canvasy(0)) - self.sync_offset_y)
        return "break"

    def _scroll_canvas_to_y(self, canvas, target_y):
        bbox = canvas.bbox("all")
        if not bbox: return
        h = bbox[3] - bbox[1]
        if h <= 0: return
        canvas.yview_moveto(max(0.0, min(1.0, target_y / h)))

    # ── File selection ────────────────────────────────────────────────────────

    def select_old(self):
        p = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if p:
            self.old_file = p
            n = os.path.basename(p)
            self.btn_old.config(
                text=f"📄 {n[:19]+'…' if len(n)>22 else n}",
                fg=ACCENT, highlightbackground=ACCENT)
            self.check_ready()

    def select_new(self):
        p = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if p:
            self.new_file = p
            n = os.path.basename(p)
            self.btn_new.config(
                text=f"📄 {n[:19]+'…' if len(n)>22 else n}",
                fg=ACCENT, highlightbackground=ACCENT)
            self.check_ready()

    def check_ready(self):
        if self.old_file and self.new_file:
            self.btn_run.config(state=tk.NORMAL)

    def _set_status(self, msg, error=False, warn=False):
        color = ERROR_COLOR if error else (WARN_COLOR if warn else SUBTEXT)
        self.lbl_status.config(
            text=msg[:87]+'…' if len(msg) > 90 else msg, fg=color)

    # ── Run ───────────────────────────────────────────────────────────────────

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
        threading.Thread(target=self._worker, daemon=True).start()

    # ── Extraction ────────────────────────────────────────────────────────────

    def _extract(self, path: str, start: int, end: int) -> list:
        """Extract word dicts from pages [start..end] inclusive (0-based)."""
        words = []
        with pdfplumber.open(path) as pdf:
            end = min(end, len(pdf.pages) - 1)
            if start > end:
                return words
            for pn in range(start, end + 1):
                try:
                    for w in pdf.pages[pn].extract_words():
                        words.append({
                            'text':   w['text'],
                            'page':   pn,
                            'x0':     w['x0'],
                            'top':    w['top'],
                            'x1':     w['x1'],
                            'bottom': w['bottom'],
                        })
                except Exception as e:
                    self.q.put(('warn', f"Page {pn+1} read error: {e}"))
        return words

    def _pull(self, path: str, buf: list, cur: int, total: int) -> tuple:
        """Append next CHUNK_PAGES worth of words to buf. Returns (buf, new_cur)."""
        if cur >= total:
            return buf, cur
        end = min(cur + CHUNK_PAGES - 1, total - 1)
        return buf + self._extract(path, cur, end), min(cur + CHUNK_PAGES, total)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render_page(self, doc, pn: int, highlights: list, color: tuple) -> Image.Image:
        bmp = doc[pn].render(scale=RENDER_SCALE)
        img = bmp.to_pil().convert("RGBA")
        ov  = Image.new('RGBA', img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(ov)
        for w in highlights:
            draw.rectangle([
                w['x0']     * RENDER_SCALE - 2,
                w['top']    * RENDER_SCALE - 2,
                w['x1']     * RENDER_SCALE + 2,
                w['bottom'] * RENDER_SCALE + 2,
            ], fill=color)
        return Image.alpha_composite(img, ov)

    @staticmethod
    def _pages_in_order(words: list) -> list:
        """Unique page indices from words list, preserving document order."""
        seen, out = set(), []
        for w in words:
            if w['page'] not in seen:
                seen.add(w['page'])
                out.append(w['page'])
        return out

    # ── Commit: diff + render, skip already-rendered pages ───────────────────

    def _commit(self,
                old_doc, new_doc,
                old_words: list, new_words: list,
                old_done: set, new_done: set) -> tuple:
        """
        Diff the two word lists, render each page exactly once,
        push to queue progressively. Returns (n_removed, n_added).
        old_done / new_done track already-rendered page indices (updated here).
        """
        if not old_words and not new_words:
            return 0, 0

        sm = difflib.SequenceMatcher(
            None,
            [w['text'] for w in old_words],
            [w['text'] for w in new_words],
            autojunk=False,
        )
        removed, added = [], []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op in ('replace', 'delete'): removed.extend(old_words[i1:i2])
            if op in ('replace', 'insert'): added.extend(new_words[j1:j2])

        # Per-page highlight maps
        old_hl: dict[int, list] = {}
        for w in removed: old_hl.setdefault(w['page'], []).append(w)

        new_hl: dict[int, list] = {}
        for w in added:   new_hl.setdefault(w['page'], []).append(w)

        # Render OLD pages
        for pn in self._pages_in_order(old_words):
            if pn in old_done or pn >= len(old_doc):
                continue
            try:
                img = self._render_page(old_doc, pn, old_hl.get(pn, []), REMOVED_COLOR)
                self.q.put(('page_old', img))
                old_done.add(pn)
            except Exception as e:
                self.q.put(('warn', f"Render OLD p{pn+1}: {e}"))

        # Render NEW pages
        for pn in self._pages_in_order(new_words):
            if pn in new_done or pn >= len(new_doc):
                continue
            try:
                img = self._render_page(new_doc, pn, new_hl.get(pn, []), ADDED_COLOR)
                self.q.put(('page_new', img))
                new_done.add(pn)
            except Exception as e:
                self.q.put(('warn', f"Render NEW p{pn+1}: {e}"))

        return len(removed), len(added)

    # ── Worker thread ─────────────────────────────────────────────────────────

    def _worker(self):
        old_doc = new_doc = None
        try:
            old_doc = pdfium.PdfDocument(self.old_file)
            new_doc = pdfium.PdfDocument(self.new_file)

            total_old = len(old_doc)
            total_new = len(new_doc)

            old_cur = new_cur = 0
            old_buf: list = []
            new_buf: list = []
            old_done: set = set()   # pages already rendered on old side
            new_done: set = set()   # pages already rendered on new side

            total_removed = total_added = 0

            # Pull first batch from both sides
            old_buf, old_cur = self._pull(self.old_file, old_buf, old_cur, total_old)
            new_buf, new_cur = self._pull(self.new_file, new_buf, new_cur, total_new)

            while old_buf or new_buf:

                pct = int(5 + 90 * max(
                    old_cur / max(total_old, 1),
                    new_cur / max(total_new, 1)))
                self.q.put(('progress', min(pct, 95)))
                self.q.put(('status',
                    f"OLD {old_cur}/{total_old} pages · NEW {new_cur}/{total_new} pages"))

                both_done = (old_cur >= total_old and new_cur >= total_new)

                if both_done:
                    # No more pages to pull — commit everything left
                    r, a = self._commit(old_doc, new_doc,
                                        old_buf, new_buf, old_done, new_done)
                    total_removed += r
                    total_added   += a
                    break

                seam = find_seam(old_buf, new_buf)

                if seam:
                    # Commit up to the anchor
                    r, a = self._commit(
                        old_doc, new_doc,
                        old_buf[: seam['old_end']],
                        new_buf[: seam['new_end']],
                        old_done, new_done)
                    total_removed += r
                    total_added   += a

                    # Drop committed words, keep remainder
                    old_buf = old_buf[seam['old_end']:]
                    new_buf = new_buf[seam['new_end']:]

                    # Refill whichever side ran dry
                    if not old_buf and old_cur < total_old:
                        old_buf, old_cur = self._pull(
                            self.old_file, old_buf, old_cur, total_old)
                    if not new_buf and new_cur < total_new:
                        new_buf, new_cur = self._pull(
                            self.new_file, new_buf, new_cur, total_new)

                else:
                    # No seam yet — pull more pages to give the algorithm more context.
                    # Pull NEW first (drain NEW until old words find a match), then OLD.
                    pulled = False
                    if new_cur < total_new:
                        new_buf, new_cur = self._pull(
                            self.new_file, new_buf, new_cur, total_new)
                        pulled = True
                    elif old_cur < total_old:
                        old_buf, old_cur = self._pull(
                            self.old_file, old_buf, old_cur, total_old)
                        pulled = True

                    if not pulled:
                        # Truly exhausted — force-commit remainder
                        r, a = self._commit(old_doc, new_doc,
                                            old_buf, new_buf, old_done, new_done)
                        total_removed += r
                        total_added   += a
                        break

            self.q.put(('done',
                f"✅ Done — {total_removed} removed · {total_added} added"))

        except Exception as e:
            self.q.put(('error',
                f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
        finally:
            for doc in (old_doc, new_doc):
                if doc:
                    try: doc.close()
                    except Exception: pass

    # ── Queue processor (unchanged from original) ─────────────────────────────

    def process_queue(self):
        try:
            while True:
                msg_type, data = self.q.get_nowait()

                if msg_type == 'status':
                    self._set_status(data)
                elif msg_type == 'warn':
                    self._set_status(data, warn=True)
                elif msg_type == 'progress':
                    self.progress['value'] = data

                elif msg_type in ('page_old', 'page_new'):
                    is_old    = (msg_type == 'page_old')
                    canvas    = self.canvas_old if is_old else self.canvas_new
                    photos    = self.old_photos  if is_old else self.new_photos
                    current_y = self.old_y       if is_old else self.new_y

                    photo = ImageTk.PhotoImage(data)
                    photos.append(photo)
                    x_pos = canvas.winfo_width() // 2

                    canvas.create_rectangle(
                        x_pos - data.width  // 2 - 1, current_y - 1,
                        x_pos + data.width  // 2 + 1, current_y + data.height + 1,
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

        except queue.Empty:
            pass
        self.root.after(50, self.process_queue)


if __name__ == "__main__":
    root = tk.Tk()
    app = PDFDiffApp(root)
    root.mainloop()