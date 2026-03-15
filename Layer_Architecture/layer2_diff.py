"""
Layer 2 — Index-Based LCS Diff + Safe Seam Finder
===================================================
Two responsibilities:

  1. compute_diff()      — full LCS diff on two word lists (small docs)
  2. find_safe_seam()    — finds the safe cut point at a chunk boundary
  3. diff_segment()      — LCS diff on a slice, returns word-object sets

The Safe Seam Problem
---------------------
When processing a 1000-page PDF in 10-page chunks, the last few words of
chunk N may be mid-sentence compared to the other PDF. Example:

  Old page 10 ends: "…Constitution, Laws and Hand Book of the Texas
                     Society of the Sons of the American Revolution, 1906"
  New page 10 ends: "…Constitution, Laws and Hand Book of the Texas
                     Society of the and doughter finace pringle Sons of"

If we cut exactly at page 10, the LCS sees "Sons of the" as the last
match and marks "American Revolution, 1906" as removed — WRONG, those
words continue on page 11 of the new PDF.

The safe seam finder scans BACKWARDS through the last SEAM_SCAN_TAIL
words of the chunk, looking for the last run of >= SEAM_MIN_RUN
consecutive identical words. That run is a position where both PDFs
are provably in sync. We commit everything before it and carry the
rest into the next chunk.
"""

import difflib
from dataclasses import dataclass
from layer1_extraction import WordObject


# ── Tuning constants ──────────────────────────────────────────────────────────
SEAM_MIN_RUN  = 6      # consecutive matching words required for a safe seam
SEAM_SCAN_PCT = 0.35   # scan the last 35% of the chunk for a seam


# ── LCS matcher ──────────────────────────────────────────────────────────────

class InsensitiveSequenceMatcher(difflib.SequenceMatcher):
    """
    Filters out very small matching islands (< threshold words) to avoid
    noisy, fragmented diff output. A 1-word match in the middle of a large
    changed block usually makes the diff worse, not better.
    """
    threshold = 2

    def get_matching_blocks(self):
        size = min(len(self.a), len(self.b))
        threshold = min(self.threshold, size / 4)
        actual = super().get_matching_blocks()
        return [m for m in actual if m[2] > threshold or not m[2]]


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class DiffResult:
    """Full-document diff result (used for small docs)."""
    removed_indices: set   # indices into OLD database
    added_indices:   set   # indices into NEW database
    stats:           dict

    def summary(self) -> str:
        s = self.stats
        return (f"Pages: {s['old_pages']} → {s['new_pages']}  |  "
                f"Words: {s['old_words']} → {s['new_words']}  |  "
                f"Removed: {s['removed']}  Added: {s['added']}  "
                f"Unchanged: {s['unchanged']}  "
                f"Change rate: {s['change_pct']}%")


@dataclass
class SeamResult:
    """
    Output of find_safe_seam().
    old_seam : index in old_words AFTER the matching run ends → commit [:old_seam]
    new_seam : index in new_words AFTER the matching run ends → commit [:new_seam]
    found    : False means no seam was found; caller should carry entire chunk forward
    """
    old_seam: int
    new_seam: int
    found:    bool


@dataclass
class SegmentDiffResult:
    """Output of diff_segment() — word objects instead of raw indices."""
    removed_words: set   # set of WordObject instances to highlight in OLD
    added_words:   set   # set of WordObject instances to highlight in NEW
    unchanged:     int


# ── Safe seam finder ─────────────────────────────────────────────────────────

def find_safe_seam(
    old_words: list[WordObject],
    new_words: list[WordObject],
) -> SeamResult:
    """
    Scan the tail of old_words/new_words and find the last run of
    SEAM_MIN_RUN consecutive matching words. Returns SeamResult with
    the commit boundary for each side.

    The scan starts at (1 - SEAM_SCAN_PCT) * len(old_words) to avoid
    scanning the entire chunk (which would be expensive and unnecessary —
    any seam near the middle is fine too).

    If no seam is found, returns SeamResult(found=False).
    """
    old_texts = [w.text for w in old_words]
    new_texts = [w.text for w in new_words]

    scan_from = int(len(old_texts) * (1 - SEAM_SCAN_PCT))

    # Build reverse lookup: word text → list of positions in new_texts
    new_index: dict[str, list[int]] = {}
    for j, text in enumerate(new_texts):
        new_index.setdefault(text, []).append(j)

    best: SeamResult | None = None

    for i in range(scan_from, len(old_texts)):
        candidates = new_index.get(old_texts[i], [])
        for j in candidates:
            # Measure run length from (i, j)
            run = 0
            while (i + run < len(old_texts) and
                   j + run < len(new_texts) and
                   old_texts[i + run] == new_texts[j + run]):
                run += 1

            if run >= SEAM_MIN_RUN:
                # We want the rightmost qualifying seam — maximises what we commit
                end_old = i + run
                end_new = j + run
                if best is None or end_old > best.old_seam:
                    best = SeamResult(old_seam=end_old, new_seam=end_new, found=True)

    return best if best is not None else SeamResult(old_seam=0, new_seam=0, found=False)


# ── Segment diff (used per chunk) ─────────────────────────────────────────────

def diff_segment(
    old_words: list[WordObject],
    new_words: list[WordObject],
) -> SegmentDiffResult:
    """
    Run LCS diff on two lists of WordObjects and return sets of WordObjects
    to highlight (not raw indices — the chunked pipeline works with object
    identity so it does not need global index tracking here).
    """
    old_texts = [w.text for w in old_words]
    new_texts = [w.text for w in new_words]

    matcher = InsensitiveSequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
    opcodes = matcher.get_opcodes()

    removed_words: set[WordObject] = set()
    added_words:   set[WordObject] = set()
    unchanged = 0

    for op, i1, i2, j1, j2 in opcodes:
        if op == "equal":
            unchanged += i2 - i1
        elif op == "delete":
            removed_words.update(old_words[i1:i2])
        elif op == "insert":
            added_words.update(new_words[j1:j2])
        elif op == "replace":
            removed_words.update(old_words[i1:i2])
            added_words.update(new_words[j1:j2])

    return SegmentDiffResult(
        removed_words=removed_words,
        added_words=added_words,
        unchanged=unchanged,
    )


# ── Full-document diff (small docs / testing) ─────────────────────────────────

def compute_diff(
    old_db: list[WordObject],
    new_db: list[WordObject],
) -> DiffResult:
    """
    Run the LCS diff on two complete word databases.
    Returns a DiffResult with global index sets.
    """
    old_texts = [w.text for w in old_db]
    new_texts = [w.text for w in new_db]

    matcher = InsensitiveSequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
    opcodes = matcher.get_opcodes()

    removed_indices: set[int] = set()
    added_indices:   set[int] = set()
    unchanged_count = 0

    for op, i1, i2, j1, j2 in opcodes:
        if op == "equal":
            unchanged_count += i2 - i1
        elif op == "delete":
            for w in old_db[i1:i2]: removed_indices.add(w.index)
        elif op == "insert":
            for w in new_db[j1:j2]: added_indices.add(w.index)
        elif op == "replace":
            for w in old_db[i1:i2]: removed_indices.add(w.index)
            for w in new_db[j1:j2]: added_indices.add(w.index)

    old_pages   = max((w.page_number for w in old_db), default=0)
    new_pages   = max((w.page_number for w in new_db), default=0)
    total       = len(old_texts) + len(new_texts)
    change_pct  = round(100 * (len(removed_indices) + len(added_indices)) / max(total, 1), 1)

    return DiffResult(
        removed_indices=removed_indices,
        added_indices=added_indices,
        stats={
            "old_words":  len(old_texts),
            "new_words":  len(new_texts),
            "removed":    len(removed_indices),
            "added":      len(added_indices),
            "unchanged":  unchanged_count,
            "change_pct": change_pct,
            "old_pages":  old_pages,
            "new_pages":  new_pages,
        },
    )


if __name__ == "__main__":
    import sys
    from layer1_extraction import extract_word_database

    if len(sys.argv) < 3:
        print("Usage: python layer2_diff.py <old.pdf> <new.pdf>")
        sys.exit(1)

    old_db = extract_word_database(sys.argv[1])
    new_db = extract_word_database(sys.argv[2])
    result = compute_diff(old_db, new_db)
    print(result.summary())