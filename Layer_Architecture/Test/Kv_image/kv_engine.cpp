#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <vector>
#include <string>
#include <cmath>
#include <algorithm>
#include <sstream>
#include <unordered_map>
#include <unordered_set>

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
    float top_min;
    float top_max;
    float x0_start;
    bool matched = false;
};

struct Paragraph {
    std::vector<Sentence> sentences;
    int page;
    float x0_avg;
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

std::vector<std::string> tokenise(const std::string& s) {
    std::vector<std::string> tokens;
    std::istringstream ss(s);
    std::string word;
    while (ss >> word) tokens.push_back(word);
    return tokens;
}

int count_word_matches(const std::string& s1, const std::string& s2) {
    auto w1 = tokenise(s1);
    auto w2 = tokenise(s2);
    int matches = 0;
    for (const auto& w : w1) {
        auto it = std::find(w2.begin(), w2.end(), w);
        if (it != w2.end()) {
            matches++;
            w2.erase(it);
        }
    }
    return matches;
}

// ─────────────────────────────────────────────────────────────
// PHASE 1 & 2 — Build Sentences & Paragraphs (your original code)
// ─────────────────────────────────────────────────────────────
std::vector<Sentence> build_sentences(std::vector<Word> words) {
    std::vector<Sentence> sentences;
    if (words.empty()) return sentences;

    std::sort(words.begin(), words.end(), [](const Word& a, const Word& b) {
        if (a.page != b.page) return a.page < b.page;
        int band_a = static_cast<int>(a.top / 3.0f);
        int band_b = static_cast<int>(b.top / 3.0f);
        if (band_a != band_b) return band_a < band_b;
        return a.x0 < b.x0;
    });

    Sentence cur;
    cur.page = words[0].page; cur.top_min = cur.top_max = words[0].top;
    cur.x0_start = words[0].x0; cur.words.push_back(words[0]); cur.full_text = words[0].text;

    for (size_t i = 1; i < words.size(); ++i) {
        const Word& w = words[i];
        bool same_line = (w.page == cur.page) &&
                         (w.top >= cur.top_min - 3.0f) && (w.top <= cur.top_max + 3.0f);
        if (same_line) {
            cur.full_text += " " + w.text;
            cur.words.push_back(w);
            cur.top_min = std::min(cur.top_min, w.top);
            cur.top_max = std::max(cur.top_max, w.top);
        } else {
            sentences.push_back(cur);
            cur = Sentence();
            cur.page = w.page; cur.top_min = cur.top_max = w.top;
            cur.x0_start = w.x0; cur.words.push_back(w); cur.full_text = w.text;
        }
    }
    sentences.push_back(cur);
    return sentences;
}

std::vector<Paragraph> build_paragraphs(const std::vector<Sentence>& sentences) {
    const float X_PARA_THRESHOLD = 20.0f;
    const float Y_LINE_GAP = 18.0f;

    std::vector<Paragraph> paragraphs;
    if (sentences.empty()) return paragraphs;

    Paragraph cur_para;
    cur_para.page = sentences[0].page;
    cur_para.x0_avg = sentences[0].x0_start;
    cur_para.sentences.push_back(sentences[0]);

    for (size_t i = 1; i < sentences.size(); ++i) {
        const Sentence& prev = sentences[i - 1];
        const Sentence& curr = sentences[i];
        float x_diff = std::abs(curr.x0_start - prev.x0_start);
        float y_gap  = curr.top_min - prev.top_max;
        bool same_pg = (curr.page == cur_para.page);

        if (same_pg && x_diff < X_PARA_THRESHOLD && y_gap < Y_LINE_GAP && y_gap >= 0) {
            cur_para.sentences.push_back(curr);
            float n = static_cast<float>(cur_para.sentences.size());
            cur_para.x0_avg = cur_para.x0_avg * ((n-1)/n) + curr.x0_start / n;
        } else {
            paragraphs.push_back(cur_para);
            cur_para = Paragraph();
            cur_para.page = curr.page;
            cur_para.x0_avg = curr.x0_start;
            cur_para.sentences.push_back(curr);
        }
    }
    paragraphs.push_back(cur_para);
    return paragraphs;
}

std::string paragraph_text(const Paragraph& p) {
    std::string t;
    for (const auto& s : p.sentences) {
        if (!t.empty()) t += " ";
        t += s.full_text;
    }
    return t;
}

// ─────────────────────────────────────────────────────────────
// PHASE 3 — SMART WORD-LEVEL DIFF (Latest & Final Version)
// ─────────────────────────────────────────────────────────────
void match_paragraphs(std::vector<Paragraph>& old_paras,
                      std::vector<Paragraph>& new_paras,
                      py::list& removed_py,
                      py::list& added_py) {

    const float PARA_SIMILAR_THRESHOLD = 0.55f;   // tune to 0.60 if you want stricter pairing

    auto jaccard = [&](const std::string& a, const std::string& b) -> float {
        auto wa = tokenise(a); auto wb = tokenise(b);
        if (wa.empty() || wb.empty()) return 0.0f;
        std::unordered_set<std::string> set_b(wb.begin(), wb.end());
        int inter = 0;
        for (const auto& w : wa) if (set_b.count(w)) inter++;
        int uni = wa.size() + wb.size() - inter;
        return static_cast<float>(inter) / uni;
    };

    // 1. Pair similar paragraphs
    std::vector<std::pair<int,int>> paired;
    for (int i = 0; i < (int)old_paras.size(); ++i) {
        if (old_paras[i].matched) continue;
        float best = -1.0f; int best_j = -1;
        for (int j = 0; j < (int)new_paras.size(); ++j) {
            if (new_paras[j].matched) continue;
            float sim = jaccard(paragraph_text(old_paras[i]), paragraph_text(new_paras[j]));
            if (sim > best) { best = sim; best_j = j; }
        }
        if (best_j != -1 && best >= PARA_SIMILAR_THRESHOLD) {
            old_paras[i].matched = true;
            new_paras[best_j].matched = true;
            paired.emplace_back(i, best_j);
        }
    }

    // 2. Pure deletions
    for (const auto& op : old_paras) {
        if (op.matched) continue;
        for (const auto& sent : op.sentences)
            for (const auto& w : sent.words)
                removed_py.append(word_to_dict(w));
    }

    // 3. Pure additions
    for (const auto& np : new_paras) {
        if (np.matched) continue;
        for (const auto& sent : np.sentences)
            for (const auto& w : sent.words)
                added_py.append(word_to_dict(w));
    }

    // 4. Paired paragraphs → highlight ONLY changed words
    for (auto& p : paired) {
        int oi = p.first, nj = p.second;

        std::unordered_set<std::string> old_set, new_set;

        for (const auto& sent : old_paras[oi].sentences)
            for (const auto& w : sent.words) old_set.insert(w.text);

        for (const auto& sent : new_paras[nj].sentences)
            for (const auto& w : sent.words) new_set.insert(w.text);

        // Deleted words (only in old)
        for (const auto& sent : old_paras[oi].sentences) {
            for (const auto& w : sent.words) {
                if (new_set.find(w.text) == new_set.end())
                    removed_py.append(word_to_dict(w));
            }
        }

        // Added words (only in new)
        for (const auto& sent : new_paras[nj].sentences) {
            for (const auto& w : sent.words) {
                if (old_set.find(w.text) == old_set.end())
                    added_py.append(word_to_dict(w));
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────
// ENTRY POINT
// ─────────────────────────────────────────────────────────────
py::dict run_kv_diff(std::vector<py::dict> old_dicts,
                     std::vector<py::dict> new_dicts) {

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

    // Phase 1 & 2
    auto old_sents = build_sentences(old_words);
    auto new_sents = build_sentences(new_words);
    auto old_paras = build_paragraphs(old_sents);
    auto new_paras = build_paragraphs(new_sents);

    // Phase 3 — Smart word-level diff
    py::list removed_py, added_py;
    match_paragraphs(old_paras, new_paras, removed_py, added_py);

    py::dict results;
    results["removed_words"] = removed_py;
    results["added_words"]   = added_py;
    return results;
}

PYBIND11_MODULE(kv_mechanism, m) {
    m.doc() = "Kv spatial PDF diffing engine - word-level highlight";
    m.def("run_diff", &run_kv_diff, "Run the Kv spatial diffing engine");
}