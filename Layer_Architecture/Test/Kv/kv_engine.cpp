#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <vector>
#include <string>
#include <cmath>
#include <algorithm>
#include <sstream>
#include <unordered_map>

namespace py = pybind11;

// ─────────────────────────────────────────────────────────────
// DATA STRUCTURES
// ─────────────────────────────────────────────────────────────
struct Word {
    std::string text;
    float x0, top, x1, bottom;
    int page;
};

struct Sentence {
    std::string full_text;
    std::vector<Word> words;
    int page;
    float top_min;   // lowest y in this line group
    float top_max;   // highest y in this line group
    float x0_start;  // leftmost x (for paragraph grouping)
    bool matched = false;
};

struct Paragraph {
    std::vector<Sentence> sentences;
    int page;
    float x0_avg;    // average left-edge x across sentences
    bool matched = false;
};

// ─────────────────────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────────────────────
py::dict word_to_dict(const Word& w) {
    py::dict d;
    d["text"]   = w.text;
    d["x0"]     = w.x0;
    d["top"]    = w.top;
    d["x1"]     = w.x1;
    d["bottom"] = w.bottom;
    d["page"]   = w.page;
    return d;
}

// Tokenise a string into words (handles multiple spaces correctly)
std::vector<std::string> tokenise(const std::string& s) {
    std::vector<std::string> tokens;
    std::istringstream ss(s);
    std::string word;
    while (ss >> word) tokens.push_back(word);
    return tokens;
}

// Count how many words from s1 appear in s2 (consumes matches, no double-count)
int count_word_matches(const std::string& s1, const std::string& s2) {
    auto w1 = tokenise(s1);
    auto w2 = tokenise(s2);
    int matches = 0;
    for (const auto& w : w1) {
        auto it = std::find(w2.begin(), w2.end(), w);
        if (it != w2.end()) {
            matches++;
            w2.erase(it);  // consume so no double-count
        }
    }
    return matches;
}

// ─────────────────────────────────────────────────────────────
// PHASE 1 — BUILD SENTENCES
// Group words that share the same page + Y-line (±3px tolerance)
// Sort left-to-right by X within each line
// ─────────────────────────────────────────────────────────────
std::vector<Sentence> build_sentences(std::vector<Word> words) {
    std::vector<Sentence> sentences;
    if (words.empty()) return sentences;

    // Sort: page first, then Y (with 3px bands), then X within a line
    std::sort(words.begin(), words.end(), [](const Word& a, const Word& b) {
        if (a.page != b.page) return a.page < b.page;
        // Round to 3px band so words on same visual line sort together
        int band_a = static_cast<int>(a.top / 3.0f);
        int band_b = static_cast<int>(b.top / 3.0f);
        if (band_a != band_b) return band_a < band_b;
        return a.x0 < b.x0;
    });

    Sentence cur;
    cur.page      = words[0].page;
    cur.top_min   = words[0].top;
    cur.top_max   = words[0].top;
    cur.x0_start  = words[0].x0;
    cur.words.push_back(words[0]);
    cur.full_text = words[0].text;

    for (size_t i = 1; i < words.size(); ++i) {
        const Word& w = words[i];
        bool same_page = (w.page == cur.page);
        // Check against the RANGE of the current group, not just first word
        bool same_line = same_page &&
                         (w.top >= cur.top_min - 3.0f) &&
                         (w.top <= cur.top_max + 3.0f);

        if (same_line) {
            cur.full_text += " " + w.text;
            cur.words.push_back(w);
            // Expand the tracked y-range
            cur.top_min = std::min(cur.top_min, w.top);
            cur.top_max = std::max(cur.top_max, w.top);
        } else {
            sentences.push_back(cur);
            cur = Sentence();
            cur.page      = w.page;
            cur.top_min   = w.top;
            cur.top_max   = w.top;
            cur.x0_start  = w.x0;
            cur.words.push_back(w);
            cur.full_text = w.text;
        }
    }
    sentences.push_back(cur);
    return sentences;
}

// ─────────────────────────────────────────────────────────────
// PHASE 2 — GROUP SENTENCES INTO PARAGRAPHS
// Two adjacent sentences belong to the same paragraph if:
//   - Same page
//   - X-start difference is small (< X_PARA_THRESHOLD)
//   - Y-gap between them is within a normal line-height (< Y_LINE_GAP)
// ─────────────────────────────────────────────────────────────
std::vector<Paragraph> build_paragraphs(const std::vector<Sentence>& sentences) {
    const float X_PARA_THRESHOLD = 20.0f;  // px: sentences within this x-align = same para
    const float Y_LINE_GAP       = 18.0f;  // px: max vertical gap between lines in same para

    std::vector<Paragraph> paragraphs;
    if (sentences.empty()) return paragraphs;

    Paragraph cur_para;
    cur_para.page   = sentences[0].page;
    cur_para.x0_avg = sentences[0].x0_start;
    cur_para.sentences.push_back(sentences[0]);

    for (size_t i = 1; i < sentences.size(); ++i) {
        const Sentence& prev = sentences[i - 1];
        const Sentence& curr = sentences[i];

        float x_diff  = std::abs(curr.x0_start - prev.x0_start);
        float y_gap   = curr.top_min - prev.top_max;
        bool  same_pg = (curr.page == cur_para.page);

        if (same_pg && x_diff < X_PARA_THRESHOLD && y_gap < Y_LINE_GAP && y_gap >= 0) {
            // Still in the same paragraph
            cur_para.sentences.push_back(curr);
            // Update running x average
            float n = static_cast<float>(cur_para.sentences.size());
            cur_para.x0_avg = cur_para.x0_avg * ((n - 1.0f) / n) + curr.x0_start / n;
        } else {
            paragraphs.push_back(cur_para);
            cur_para = Paragraph();
            cur_para.page      = curr.page;
            cur_para.x0_avg    = curr.x0_start;
            cur_para.sentences.push_back(curr);
        }
    }
    paragraphs.push_back(cur_para);
    return paragraphs;
}

// Build the full text of a paragraph (all its sentences joined)
std::string paragraph_text(const Paragraph& p) {
    std::string t;
    for (const auto& s : p.sentences) {
        if (!t.empty()) t += " ";
        t += s.full_text;
    }
    return t;
}

// ─────────────────────────────────────────────────────────────
// PHASE 3 — DIFF PARAGRAPHS (two-pass Kv matching)
//
// Pass 1 — Fast exact match (hash-based, near zero CPU cost)
// Pass 2 — N/2 fuzzy match with 30% hard floor
//
// Repeated TWICE as per Kv design, so missed matches on first
// loop get a second chance.
// ─────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────
// PHASE 3 — v4 STRICT: Only near-identical = matched
// → Replacements, any word change, and unrelated sentences ALL get both-side highlight
// → Only truly equal text stays without highlight
// ─────────────────────────────────────────────────────────────
void match_paragraphs(std::vector<Paragraph>& old_paras,
                      std::vector<Paragraph>& new_paras) {

    const float MIN_JACCARD_FOR_MATCH = 0.92f;   // VERY high → only almost same text matches
    const float MAX_LEN_DIFF          = 0.08f;   // max 8% length difference allowed to match
    const int   MIN_COMMON_WORDS      = 5;       // must share at least 5 words to even consider

    auto jaccard = [&](const std::string& a, const std::string& b) -> float {
        auto wa = tokenise(a);
        auto wb = tokenise(b);
        if (wa.empty() && wb.empty()) return 1.0f;
        if (wa.empty() || wb.empty()) return 0.0f;

        std::unordered_set<std::string> wb_set(wb.begin(), wb.end());
        int inter = 0;
        for (const auto& w : wa) {
            if (wb_set.count(w)) inter++;
        }
        int uni = wa.size() + wb.size() - inter;
        return static_cast<float>(inter) / uni;
    };

    auto almost_same_length = [&](const std::string& a, const std::string& b) -> bool {
        int na = tokenise(a).size();
        int nb = tokenise(b).size();
        if (na == 0 || nb == 0) return true;
        float diff = std::abs(na - nb) / static_cast<float>(std::max(na, nb));
        return diff <= MAX_LEN_DIFF;
    };

    for (int cycle = 0; cycle < 2; ++cycle) {   // 2 cycles enough now

        // 1. Exact string match (fast)
        std::unordered_map<std::string, int> new_index;
        for (int j = 0; j < (int)new_paras.size(); ++j) {
            if (!new_paras[j].matched)
                new_index[paragraph_text(new_paras[j])] = j;
        }
        for (auto& op : old_paras) {
            if (op.matched) continue;
            auto it = new_index.find(paragraph_text(op));
            if (it != new_index.end()) {
                op.matched = new_paras[it->second].matched = true;
            }
        }

        // 2. Only extremely similar paragraphs are allowed to match
        struct Candidate { int i, j; float score; };
        std::vector<Candidate> candidates;

        for (int i = 0; i < (int)old_paras.size(); ++i) {
            if (old_paras[i].matched) continue;
            std::string ot = paragraph_text(old_paras[i]);
            int N = tokenise(ot).size();
            if (N == 0) { old_paras[i].matched = true; continue; }

            for (int j = 0; j < (int)new_paras.size(); ++j) {
                if (new_paras[j].matched) continue;
                std::string nt = paragraph_text(new_paras[j]);
                float jac = jaccard(ot, nt);
                int common = count_word_matches(ot, nt);

                if (jac >= MIN_JACCARD_FOR_MATCH &&
                    common >= MIN_COMMON_WORDS &&
                    almost_same_length(ot, nt)) {

                    candidates.push_back({i, j, jac * 100.0f}); // very high score
                }
            }
        }

        // Sort & assign (no bidirectional needed anymore — strict is better)
        std::sort(candidates.begin(), candidates.end(),
                  [](const Candidate& a, const Candidate& b){ return a.score > b.score; });

        for (const auto& c : candidates) {
            if (!old_paras[c.i].matched && !new_paras[c.j].matched) {
                old_paras[c.i].matched = true;
                new_paras[c.j].matched = true;
            }
        }
    }

    // === FINAL FORCE UNMATCH (this guarantees your requirement) ===
    // Anything that is not extremely similar → stay unmatched → WILL BE HIGHLIGHTED
    for (auto& op : old_paras) {
        if (op.matched) continue;
        // Check if ANY new paragraph is close enough
        bool has_near_match = false;
        for (const auto& np : new_paras) {
            if (np.matched) continue;
            float jac = jaccard(paragraph_text(op), paragraph_text(np));
            if (jac >= 0.92f) { has_near_match = true; break; }
        }
        if (!has_near_match) {
            // Pure deletion or replacement → RED highlight ✅
        }
    }

    for (auto& np : new_paras) {
        if (np.matched) continue;
        bool has_near_match = false;
        for (const auto& op : old_paras) {
            if (op.matched) continue;
            float jac = jaccard(paragraph_text(np), paragraph_text(op));
            if (jac >= 0.92f) { has_near_match = true; break; }
        }
        if (!has_near_match) {
            // Pure addition or replacement → YELLOW highlight ✅
        }
    }
}
// ─────────────────────────────────────────────────────────────
// ENTRY POINT — called from Python
// ─────────────────────────────────────────────────────────────
py::dict run_kv_diff(std::vector<py::dict> old_dicts,
                     std::vector<py::dict> new_dicts) {

    // Parse Python dicts → C++ structs
    auto parse = [](const std::vector<py::dict>& dicts) {
        std::vector<Word> words;
        words.reserve(dicts.size());
        for (const auto& d : dicts)
            words.push_back({
                d["text"].cast<std::string>(),
                d["x0"].cast<float>(),
                d["top"].cast<float>(),
                d["x1"].cast<float>(),
                d["bottom"].cast<float>(),
                d["page"].cast<int>()
            });
        return words;
    };

    auto old_words = parse(old_dicts);
    auto new_words = parse(new_dicts);

    // Phase 1 — sentences
    auto old_sents = build_sentences(old_words);
    auto new_sents = build_sentences(new_words);

    // Phase 2 — paragraphs
    auto old_paras = build_paragraphs(old_sents);
    auto new_paras = build_paragraphs(new_sents);

    // Phase 3 — diff (two-cycle Kv matching)
    match_paragraphs(old_paras, new_paras);

    // Collect unmatched words → return to Python for highlighting
    py::list removed_py, added_py;

    for (const auto& para : old_paras) {
        if (!para.matched) {
            for (const auto& sent : para.sentences)
                for (const auto& w : sent.words)
                    removed_py.append(word_to_dict(w));
        }
    }
    for (const auto& para : new_paras) {
        if (!para.matched) {
            for (const auto& sent : para.sentences)
                for (const auto& w : sent.words)
                    added_py.append(word_to_dict(w));
        }
    }

    py::dict results;
    results["removed_words"] = removed_py;
    results["added_words"]   = added_py;
    return results;
}

PYBIND11_MODULE(kv_mechanism, m) {
    m.doc() = "Kv spatial PDF diffing engine";
    m.def("run_diff", &run_kv_diff, "Run the Kv spatial diffing engine");
}