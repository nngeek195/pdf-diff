"""
Layer 2 — Diff + Safe Seam
============================
Uses difflib.SequenceMatcher — same as the working script.

SAFE SEAM FINDER
-----------------
Scans the last SEAM_SCAN_PCT of old_words for the rightmost run of
SEAM_MIN_RUN consecutive matching words in new_words.
Returns {'old_seam': int, 'new_seam': int} or None.

DIFF
-----
SequenceMatcher on word texts → removed_words, added_words as lists
of word dicts (with x0/top/x1/bottom coords intact for the painter).
"""

import difflib

SEAM_MIN_RUN  = 6
SEAM_SCAN_PCT = 0.35


def find_safe_seam(old_words: list[dict], new_words: list[dict]) -> dict | None:
    """
    Find the rightmost safe cut point near the end of the chunk.
    Returns {'old_seam': int, 'new_seam': int} or None if not found.
    """
    old_texts = [w['text'] for w in old_words]
    new_texts = [w['text'] for w in new_words]

    if not old_texts or not new_texts:
        return None

    scan_from = int(len(old_texts) * (1 - SEAM_SCAN_PCT))

    # Build lookup: word text → list of positions in new_texts
    new_index: dict[str, list[int]] = {}
    for j, text in enumerate(new_texts):
        new_index.setdefault(text, []).append(j)

    best_seam = None

    for i in range(scan_from, len(old_texts)):
        candidates = new_index.get(old_texts[i])
        if not candidates:
            continue
        for j in candidates:
            run = 0
            while (i + run < len(old_texts) and
                   j + run < len(new_texts) and
                   old_texts[i + run] == new_texts[j + run]):
                run += 1
            if run >= SEAM_MIN_RUN:
                if best_seam is None or (i + run) > best_seam['old_seam']:
                    best_seam = {'old_seam': i + run, 'new_seam': j + run}

    return best_seam


def diff_words(old_commit: list[dict],
               new_commit: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Diff two committed word lists.
    Returns (removed_words, added_words) — lists of word dicts.
    """
    sm = difflib.SequenceMatcher(
        None,
        [w['text'] for w in old_commit],
        [w['text'] for w in new_commit],
        autojunk=False,
    )

    removed_words: list[dict] = []
    added_words:   list[dict] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ('replace', 'delete'):
            removed_words.extend(old_commit[i1:i2])
        if tag in ('replace', 'insert'):
            added_words.extend(new_commit[j1:j2])

    return removed_words, added_words


if __name__ == '__main__':
    import sys
    from layer1_extraction import extract_page_range

    if len(sys.argv) < 3:
        print("Usage: python layer2_diff.py <old.pdf> <new.pdf>")
        sys.exit(1)

    old_words = extract_page_range(sys.argv[1], 0, 9)
    new_words = extract_page_range(sys.argv[2], 0, 9)
    removed, added = diff_words(old_words, new_words)
    total = len(old_words) + len(new_words)
    pct   = round(100 * (len(removed) + len(added)) / max(total, 1), 1)
    print(f"Old: {len(old_words)} words  New: {len(new_words)} words")
    print(f"Removed: {len(removed)}  Added: {len(added)}  Change: {pct}%")