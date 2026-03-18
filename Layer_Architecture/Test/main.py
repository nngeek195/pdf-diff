import tkinter as tk
from tkinter import filedialog, ttk
import threading
import queue
import os
import math
import bisect
import traceback

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageTk, ImageDraw

# ── Theme ─────────────────────────────────────────────────────────────────────
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

RENDER_SCALE    = 1.5
PAGE_GAP        = 28
SCROLL_INTERVAL = 12
SCROLL_EASING   = 0.78
SCROLL_SPEED    = 0.06

# KV mechanism tuning
GAP_MULTIPLIER  = 2.5   # gap > avg_spacing * this  →  column/sequence break
ROW_TOLERANCE   = 0.55  # fraction of avg word height to treat as "same row"


# ═════════════════════════════════════════════════════════════════════════════
#  KV MECHANISM
# ═════════════════════════════════════════════════════════════════════════════

# ── Theme & constants (unchanged) ───────────────────────────────────────────
# ... (your existing constants stay exactly the same)

# ═════════════════════════════════════════════════════════════════════════════
# LINE CLUSTERING (replaces the old KV mechanism)
# ═════════════════════════════════════════════════════════════════════════════
def _page_metrics(words: list) -> tuple[float, float]:
    """Kept for compatibility – only row_tol is now used."""
    if not words:
        return 60.0, 8.0
    heights = [w['bottom'] - w['top'] for w in words]
    avg_h = sum(heights) / len(heights)
    row_tol = avg_h * ROW_TOLERANCE
    return 60.0, row_tol   # col_break no longer needed


def build_kv_sequences(words: list) -> list[list[dict]]:
    """
    Robust reading-order reconstruction using Line Clustering.
    Returns list of sequences (each sequence = one logical paragraph/column).
    """
    if not words:
        return []

    _, row_tol = _page_metrics(words)
    heights = [w['bottom'] - w['top'] for w in words]
    avg_h = sum(heights) / len(heights) if heights else 10.0

    # 1. Group words into horizontal lines
    sorted_words = sorted(words, key=lambda w: (w['top'], w['x0']))
    lines: list[list[dict]] = []
    if sorted_words:
        current = [sorted_words[0]]
        for w in sorted_words[1:]:
            if abs(w['top'] - current[0]['top']) <= row_tol * 1.2:
                current.append(w)
            else:
                current.sort(key=lambda ww: ww['x0'])
                lines.append(current)
                current = [w]
        current.sort(key=lambda ww: ww['x0'])
        lines.append(current)

    if not lines:
        return []

    # Accurate vertical gaps (top of next line – bottom of previous line)
    line_tops = [min(w['top'] for w in line) for line in lines]
    line_bottoms = [max(w['bottom'] for w in line) for line in lines]
    v_gaps = [line_tops[i] - line_bottoms[i-1] for i in range(1, len(lines))]

    avg_vgap = sum(v_gaps) / len(v_gaps) if v_gaps else 0
    block_gap = max(avg_vgap * GAP_MULTIPLIER, avg_h * 2.5)

    # 2. Group lines into blocks (paragraphs)
    blocks: list[list[list[dict]]] = []
    current_block = [lines[0]]
    for i in range(1, len(lines)):
        if v_gaps[i-1] <= block_gap:
            current_block.append(lines[i])
        else:
            blocks.append(current_block)
            current_block = [lines[i]]
    blocks.append(current_block)

    # 3. For each block: detect columns + flatten to sequences
    sequences: list[list[dict]] = []
    for block_lines in blocks:
        if not block_lines:
            continue

        # Get left margin of each line
        line_lefts = [line[0]['x0'] for line in block_lines]

        # Sort lines by left x for column detection
        sorted_block = sorted(block_lines, key=lambda line: line[0]['x0'])
        sorted_lefts = [line[0]['x0'] for line in sorted_block]

        # Column break threshold
        if len(sorted_block) > 1:
            x_gaps = [sorted_lefts[k+1] - sorted_lefts[k] for k in range(len(sorted_lefts)-1)]
            avg_xgap = sum(x_gaps) / len(x_gaps)
            col_break_x = max(avg_xgap * GAP_MULTIPLIER, avg_h * 4.0)
        else:
            col_break_x = 9999

        # Split into column groups
        col_groups = []
        current_col = [sorted_block[0]]
        for nxt in sorted_block[1:]:
            if nxt[0]['x0'] - current_col[-1][0]['x0'] > col_break_x:
                col_groups.append(current_col)
                current_col = [nxt]
            else:
                current_col.append(nxt)
        col_groups.append(current_col)

        # For each column: sort lines by y (top-to-bottom) and flatten words
        for col_lines in col_groups:
            col_lines_y = sorted(col_lines, key=lambda ln: ln[0]['top'])
            seq = []
            for ln in col_lines_y:
                seq.extend(ln)          # words already left-to-right
            if seq:
                sequences.append(seq)

    return sequences



# ═════════════════════════════════════════════════════════════════════════════
#  LCS DIFF
# ═════════════════════════════════════════════════════════════════════════════

def _lcs_length(a: list[str], b: list[str]) -> int:
    """Two-row LCS length only (memory efficient)."""
    n = len(b)
    prev = [0] * (n + 1)
    for ai in a:
        curr = [0] * (n + 1)
        for j, bj in enumerate(b, 1):
            curr[j] = prev[j-1] + 1 if ai == bj else max(prev[j], curr[j-1])
        prev = curr
    return prev[n]


def best_match(old_seq: list[dict], new_seqs: list[list[dict]]) -> list[dict]:
    """Return the new sequence with the highest LCS score vs old_seq."""
    if not new_seqs:
        return []
    ot = [w['text'] for w in old_seq]
    best_s, best_sc = new_seqs[0], -1.0
    for ns in new_seqs:
        nt = [w['text'] for w in ns]
        if not nt: continue
        sc = _lcs_length(ot, nt) / max(len(ot), len(nt), 1)
        if sc > best_sc:
            best_sc, best_s = sc, ns
    return best_s


def lcs_diff(old_seq: list[dict], new_seq: list[dict]) -> tuple[set, set]:
    """
    Full LCS backtrack.
    Returns (old_deleted_ids, new_added_ids) — id(word) sets not in LCS.
    """
    a = [w['text'] for w in old_seq]
    b = [w['text'] for w in new_seq]
    m, n = len(a), len(b)
    if m == 0 and n == 0:
        return set(), set()

    # Full DP table (page-sized, manageable)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            dp[i][j] = (dp[i-1][j-1] + 1 if a[i-1] == b[j-1]
                        else max(dp[i-1][j], dp[i][j-1]))

    # Backtrack
    old_lcs, new_lcs = set(), set()
    i, j = m, n
    while i > 0 and j > 0:
        if a[i-1] == b[j-1]:
            old_lcs.add(i-1); new_lcs.add(j-1)
            i -= 1; j -= 1
        elif dp[i-1][j] >= dp[i][j-1]:
            i -= 1
        else:
            j -= 1

    old_del = {id(old_seq[k]) for k in range(m) if k not in old_lcs}
    new_add = {id(new_seq[k]) for k in range(n) if k not in new_lcs}
    return old_del, new_add


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION
# ═════════════════════════════════════════════════════════════════════════════

class PDFDiffApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Pro PDF Diff Engine")
        self.root.geometry("1300x850")
        self.root.configure(bg=BG)

        self.old_file = self.new_file = None
        self.q = queue.Queue()

        self.old_photos: list = []
        self.new_photos: list = []
        self.old_y = PAGE_GAP
        self.new_y = PAGE_GAP

        self.is_syncing        = False
        self._old_page_tops:   list[float] = []
        self._new_page_tops:   list[float] = []
        self._vel_old:         float = 0.0
        self._vel_new:         float = 0.0
        self._scroll_after              = None
        self._full_status_text: str    = ""

        self.setup_ui()
        self.process_queue()

    # ── UI ────────────────────────────────────────────────────────────────────

    def setup_ui(self):
        style = ttk.Style()
        style.theme_use('default')
        style.configure("TProgressbar", thickness=3,
                        background=ACCENT, troughcolor=BG, borderwidth=0)
        self.progress = ttk.Progressbar(
            self.root, style="TProgressbar",
            orient="horizontal", mode="determinate")
        self.progress.pack(fill=tk.X, side=tk.TOP)

        top_bar = tk.Frame(self.root, bg=SURFACE, height=56,
                           highlightbackground=BORDER, highlightthickness=1)
        top_bar.pack(fill=tk.X, side=tk.TOP)
        top_bar.pack_propagate(False)

        tk.Label(top_bar, text="⬛ PDF DIFF",
                 font=("Courier New", 14, "bold"),
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

        legend = tk.Frame(top_bar, bg=SURFACE)
        legend.pack(side=tk.LEFT, padx=20, pady=15)
        tk.Label(legend, text="", bg="#FFD54F",
                 width=2, height=1).pack(side=tk.LEFT)
        tk.Label(legend, text="Removed", bg=SURFACE,
                 fg=SUBTEXT, font=("Arial", 9)).pack(side=tk.LEFT, padx=(5, 15))
        tk.Label(legend, text="", bg="#4FC3F7",
                 width=2, height=1).pack(side=tk.LEFT)
        tk.Label(legend, text="Added", bg=SURFACE,
                 fg=SUBTEXT, font=("Arial", 9)).pack(side=tk.LEFT, padx=(5, 0))

        self.lbl_status = tk.Label(
            top_bar, text="Select both PDFs to begin.",
            bg=SURFACE, fg=SUBTEXT, font=("Arial", 10), cursor="hand2")
        self.lbl_status.pack(side=tk.RIGHT, padx=20, pady=15)
        self.lbl_status.bind("<Button-1>", self._copy_status)

        # ── Pane wrapper: Left scrollbar | Old canvas | Center scrollbar |
        #                  New canvas | Right scrollbar  ─────────────────────
        pane = tk.Frame(self.root, bg=BG)
        pane.pack(fill=tk.BOTH, expand=True)

        # Left independent scrollbar (OLD only)
        self.sb_old = ttk.Scrollbar(pane, orient="vertical",
                                    command=self._scroll_old_indep)
        self.sb_old.pack(side=tk.LEFT, fill=tk.Y)

        # Old canvas column
        left_col = tk.Frame(pane, bg=BG)
        left_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lh = tk.Frame(left_col, bg=SURFACE2, height=30,
                      highlightbackground=BORDER, highlightthickness=1)
        lh.pack(fill=tk.X); lh.pack_propagate(False)
        tk.Label(lh, text="OLD VERSION", bg=SURFACE2,
                 fg="#E3B341", font=("Arial", 9, "bold")).pack(
                     side=tk.LEFT, padx=14, pady=5)
        self.canvas_old = tk.Canvas(left_col, bg="#010409", highlightthickness=0,
                                    yscrollcommand=self._update_sbs_old)
        self.canvas_old.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Center sync scrollbar (drives BOTH)
        self.sb_center = ttk.Scrollbar(pane, orient="vertical",
                                       command=self._scroll_sync)
        self.sb_center.pack(side=tk.LEFT, fill=tk.Y)

        # Right independent scrollbar (NEW only)
        self.sb_new = ttk.Scrollbar(pane, orient="vertical",
                                    command=self._scroll_new_indep)
        self.sb_new.pack(side=tk.RIGHT, fill=tk.Y)

        # New canvas column
        right_col = tk.Frame(pane, bg=BG,
                             highlightbackground=BORDER, highlightthickness=1)
        right_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rh = tk.Frame(right_col, bg=SURFACE2, height=30,
                      highlightbackground=BORDER, highlightthickness=1)
        rh.pack(fill=tk.X); rh.pack_propagate(False)
        tk.Label(rh, text="NEW VERSION", bg=SURFACE2,
                 fg=ACCENT, font=("Arial", 9, "bold")).pack(
                     side=tk.LEFT, padx=14, pady=5)
        self.canvas_new = tk.Canvas(right_col, bg="#010409", highlightthickness=0,
                                    yscrollcommand=self.sb_new.set)
        self.canvas_new.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for c in (self.canvas_old, self.canvas_new):
            c.bind("<MouseWheel>", self.on_mousewheel)
            c.bind("<Button-4>",   self.on_mousewheel)
            c.bind("<Button-5>",   self.on_mousewheel)

    # ── Scrollbar commands ────────────────────────────────────────────────────

    def _update_sbs_old(self, first, last):
        """Old canvas scrolled: update left + center scrollbars."""
        self.sb_old.set(first, last)
        self.sb_center.set(first, last)

    def _scroll_old_indep(self, *args):
        self.canvas_old.yview(*args)

    def _scroll_new_indep(self, *args):
        self.canvas_new.yview(*args)

    def _scroll_sync(self, *args):
        """Center scrollbar: scroll old, then align new by page."""
        self.canvas_old.yview(*args)
        oy = float(self.canvas_old.canvasy(0))
        ny = self._map_y(self._old_page_tops, self._new_page_tops, oy)
        self._goto_y(self.canvas_new, ny)

    # ── Scroll helpers ────────────────────────────────────────────────────────

    def _goto_y(self, canvas: tk.Canvas, y: float):
        bb = canvas.bbox("all")
        if not bb: return
        h = bb[3] - bb[1]
        if h > 0:
            canvas.yview_moveto(max(0.0, min(1.0, y / h)))

    def _map_y(self, src_tops, dst_tops, src_y):
        """
        Map absolute canvas-y from src to dst preserving intra-page offset.
        If the page exists in dst, lands on same relative position.
        If dst is shorter, extrapolates using estimated page height.
        """
        if not src_tops:
            return src_y
        idx    = max(0, bisect.bisect_right(src_tops, src_y) - 1)
        offset = src_y - src_tops[idx]
        if dst_tops:
            if idx < len(dst_tops):
                return dst_tops[idx] + offset
            ph = (dst_tops[1] - dst_tops[0]) if len(dst_tops) >= 2 else 700.0
            return dst_tops[-1] + (idx - len(dst_tops) + 1) * ph + offset
        return src_y

    # ── Smooth scroll ─────────────────────────────────────────────────────────

    def on_mousewheel(self, event):
        if self.is_syncing:
            return "break"
        self.is_syncing = True

        if   event.num == 4: d = -1
        elif event.num == 5: d =  1
        else:
            d = int(-1 * (event.delta / 120))
            if d == 0: d = -1 if event.delta > 0 else 1

        is_old = (event.widget == self.canvas_old)
        tops   = self._old_page_tops if is_old else self._new_page_tops
        ph     = (tops[1] - tops[0]) if len(tops) >= 2 else 700.0
        vel    = d * ph * SCROLL_SPEED

        if is_old: self._vel_old += vel
        else:      self._vel_new += vel

        if self._scroll_after is None:
            self._scroll_after = self.root.after(0, self._tick)

        self.root.after(10, lambda: setattr(self, 'is_syncing', False))
        return "break"

    def _tick(self):
        moving = False

        if abs(self._vel_old) > 0.5:
            cur = float(self.canvas_old.canvasy(0))
            ny  = cur + self._vel_old
            self._goto_y(self.canvas_old, ny)
            self._goto_y(self.canvas_new,
                         self._map_y(self._old_page_tops, self._new_page_tops, ny))
            self._vel_old *= SCROLL_EASING
            moving = True
        else:
            self._vel_old = 0.0

        if abs(self._vel_new) > 0.5:
            cur = float(self.canvas_new.canvasy(0))
            ny  = cur + self._vel_new
            self._goto_y(self.canvas_new, ny)
            self._goto_y(self.canvas_old,
                         self._map_y(self._new_page_tops, self._old_page_tops, ny))
            self._vel_new *= SCROLL_EASING
            moving = True
        else:
            self._vel_new = 0.0

        self._scroll_after = (
            self.root.after(SCROLL_INTERVAL, self._tick) if moving else None)

    # ── File selection ────────────────────────────────────────────────────────

    def select_old(self):
        p = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if not p: return
        e = self._validate(p)
        if e: self._set_status(f"Old PDF: {e}", error=True); return
        self.old_file = p
        n = os.path.basename(p)
        self.btn_old.config(
            text=f"📄 {n[:19]+'…' if len(n)>22 else n}",
            fg=ACCENT, highlightbackground=ACCENT)
        self.check_ready()

    def select_new(self):
        p = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if not p: return
        e = self._validate(p)
        if e: self._set_status(f"New PDF: {e}", error=True); return
        self.new_file = p
        n = os.path.basename(p)
        self.btn_new.config(
            text=f"📄 {n[:19]+'…' if len(n)>22 else n}",
            fg=ACCENT, highlightbackground=ACCENT)
        self.check_ready()

    def _validate(self, path):
        if not os.path.exists(path):      return "File not found."
        if os.path.getsize(path) == 0:    return "File is empty."
        try:
            with open(path, 'rb') as f:
                if f.read(5) != b'%PDF-': return "Not a valid PDF."
        except OSError as e:              return f"Cannot read: {e}"
        return None

    def check_ready(self):
        if self.old_file and self.new_file:
            self.btn_run.config(state=tk.NORMAL)
            self._set_status("Ready — click ▶ Compare.")

    def _set_status(self, msg, error=False, warn=False):
        self._full_status_text = msg
        color = ERROR_COLOR if error else (WARN_COLOR if warn else SUBTEXT)
        self.lbl_status.config(
            text=msg[:87]+'…' if len(msg) > 90 else msg, fg=color)

    def _copy_status(self, _=None):
        if self._full_status_text:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._full_status_text)

    # ── Run ───────────────────────────────────────────────────────────────────

    def run_diff(self):
        self.btn_run.config(state=tk.DISABLED)
        self.canvas_old.delete("all")
        self.canvas_new.delete("all")
        self.old_photos.clear()
        self.new_photos.clear()
        self.old_y = PAGE_GAP
        self.new_y = PAGE_GAP
        self._old_page_tops.clear()
        self._new_page_tops.clear()
        self._vel_old = self._vel_new = 0.0
        self.progress['value'] = 2
        self._set_status("Starting…")
        self.root.update_idletasks()
        threading.Thread(target=self._worker, daemon=True).start()

    # ── Rendering helper ──────────────────────────────────────────────────────

    def _render(self, doc, pn: int,
                highlights: list, color: tuple) -> Image.Image:
        bmp = doc[pn].render(scale=RENDER_SCALE)
        img = bmp.to_pil().convert("RGBA")
        if highlights:
            ov   = Image.new('RGBA', img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(ov)
            for w in highlights:
                draw.rectangle([
                    w['x0']     * RENDER_SCALE - 2,
                    w['top']    * RENDER_SCALE - 2,
                    w['x1']     * RENDER_SCALE + 2,
                    w['bottom'] * RENDER_SCALE + 2,
                ], fill=color)
            img = Image.alpha_composite(img, ov)
        return img

    # ── Worker thread ─────────────────────────────────────────────────────────

    def _worker(self):
        old_doc = new_doc = None
        try:
            old_doc = pdfium.PdfDocument(self.old_file)
            new_doc = pdfium.PdfDocument(self.new_file)
            total_old = len(old_doc)
            total_new = len(new_doc)
            total     = max(total_old, total_new)
            n_removed = n_added = 0

            for pn in range(total):
                self.q.put(('progress', int(5 + 90 * pn / total)))
                self.q.put(('status',   f"Page {pn+1} / {total}…"))

                # ── Step 1: Extract ───────────────────────────────────────────
                old_words = self._extract(self.old_file, pn) if pn < total_old else []
                new_words = self._extract(self.new_file, pn) if pn < total_new else []

                # ── Step 2: KV sequences ──────────────────────────────────────
                old_seqs = build_kv_sequences(old_words)
                new_seqs = build_kv_sequences(new_words)

                # ── Step 3: LCS diff ──────────────────────────────────────────
                # For every old sequence find best-matching new sequence, run LCS
                old_del_ids: set[int] = set()
                new_add_ids: set[int] = set()
                matched_new_seqs: list = []

                for os_ in old_seqs:
                    ns_ = best_match(os_, new_seqs)
                    matched_new_seqs.append(ns_)
                    d, a = lcs_diff(os_, ns_)
                    old_del_ids |= d
                    new_add_ids |= a

                # New sequences with no match → all words are added
                all_matched_new_ids = {id(w) for ns_ in matched_new_seqs
                                       for w in ns_}
                for ns_ in new_seqs:
                    for w in ns_:
                        if id(w) not in all_matched_new_ids:
                            new_add_ids.add(id(w))

                old_hl = [w for w in old_words if id(w) in old_del_ids]
                new_hl = [w for w in new_words if id(w) in new_add_ids]
                n_removed += len(old_hl)
                n_added   += len(new_hl)

                # ── Step 4: Render ────────────────────────────────────────────
                # Always emit a page for both sides (None = blank placeholder)
                if pn < total_old:
                    try:
                        self.q.put(('page_old', self._render(old_doc, pn,
                                                             old_hl, REMOVED_COLOR)))
                    except Exception as e:
                        self.q.put(('warn', f"Render OLD p{pn+1}: {e}"))
                        self.q.put(('page_old', None))
                else:
                    self.q.put(('page_old', None))

                if pn < total_new:
                    try:
                        self.q.put(('page_new', self._render(new_doc, pn,
                                                             new_hl, ADDED_COLOR)))
                    except Exception as e:
                        self.q.put(('warn', f"Render NEW p{pn+1}: {e}"))
                        self.q.put(('page_new', None))
                else:
                    self.q.put(('page_new', None))

            self.q.put(('done',
                        f"✅ Done — {n_removed} removed · {n_added} added"))

        except Exception as e:
            self.q.put(('error_full',
                        (f"{type(e).__name__}: {e}", traceback.format_exc())))
        finally:
            for doc in (old_doc, new_doc):
                if doc:
                    try: doc.close()
                    except Exception: pass

    def _extract(self, path: str, pn: int) -> list[dict]:
        """Extract word dicts for page pn. Returns [] on any error."""
        words = []
        try:
            with pdfplumber.open(path) as pdf:
                if pn < len(pdf.pages):
                    for w in pdf.pages[pn].extract_words():
                        words.append({
                            'text':   w['text'],
                            'page':   pn,
                            'x0':     float(w['x0']),
                            'top':    float(w['top']),
                            'x1':     float(w['x1']),
                            'bottom': float(w['bottom']),
                        })
        except Exception as e:
            self.q.put(('warn', f"Extract p{pn+1}: {e}"))
        return words

    # ── Queue processor ───────────────────────────────────────────────────────

    def process_queue(self):
        # We keep a reference image to size blank placeholders consistently
        self._last_img_size = (int(595 * RENDER_SCALE), int(842 * RENDER_SCALE))

        def _loop():
            try:
                while True:
                    kind, data = self.q.get_nowait()

                    if kind == 'status':
                        self._set_status(data)

                    elif kind == 'warn':
                        self._set_status(data, warn=True)

                    elif kind == 'progress':
                        self.progress['value'] = data

                    elif kind in ('page_old', 'page_new'):
                        is_old    = (kind == 'page_old')
                        canvas    = self.canvas_old if is_old else self.canvas_new
                        photos    = self.old_photos  if is_old else self.new_photos
                        cur_y     = self.old_y       if is_old else self.new_y
                        tops      = (self._old_page_tops if is_old
                                     else self._new_page_tops)

                        # Blank placeholder for pages beyond shorter PDF
                        if data is None:
                            w, h = self._last_img_size
                            data = Image.new("RGBA", (w, h), (13, 17, 23, 255))
                        else:
                            self._last_img_size = (data.width, data.height)

                        photo = ImageTk.PhotoImage(data)
                        photos.append(photo)
                        x = max(canvas.winfo_width() // 2,
                                data.width // 2 + 20)

                        tops.append(float(cur_y))

                        canvas.create_rectangle(
                            x - data.width  // 2 - 1, cur_y - 1,
                            x + data.width  // 2 + 1, cur_y + data.height + 1,
                            outline=BORDER, fill=BORDER)
                        canvas.create_image(x, cur_y, image=photo, anchor="n")

                        nxt = cur_y + data.height + PAGE_GAP
                        if is_old: self.old_y = nxt
                        else:      self.new_y = nxt
                        canvas.config(scrollregion=canvas.bbox("all"))

                    elif kind == 'done':
                        self._set_status(data)
                        self.btn_run.config(state=tk.NORMAL)
                        self.progress['value'] = 100
                        self.root.after(2000,
                            lambda: self.progress.configure(value=0))

                    elif kind == 'error':
                        self._set_status(f"❌ {data}", error=True)
                        self.btn_run.config(state=tk.NORMAL)
                        self.progress['value'] = 0

                    elif kind == 'error_full':
                        short, full = data
                        self._full_status_text = full
                        self.lbl_status.config(
                            text=f"❌ {short[:78]}…  (click to copy trace)",
                            fg=ERROR_COLOR)
                        self.btn_run.config(state=tk.NORMAL)
                        self.progress['value'] = 0

            except queue.Empty:
                pass
            self.root.after(50, _loop)

        _loop()


if __name__ == "__main__":
    root = tk.Tk()
    app = PDFDiffApp(root)
    root.mainloop()