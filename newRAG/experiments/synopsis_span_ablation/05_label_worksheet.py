"""
Step 3 helper #2: turn top100_H.json + top100_N.json into a human-readable
labelling worksheet. For each question prints the most promising candidates
(synopsis chunks first, then body) in compact form, side-by-side per config.

You then read this worksheet, decide which chunks are gold-relevant, and
type the IDs back into questions.csv's `gold_chunk_ids`, `gold_synopsis_id`,
and `reference_answer` columns.

Usage:
    python3 05_label_worksheet.py \\
        --top100-h top100_H.json \\
        --top100-n top100_N.json \\
        --questions questions.csv \\
        --out label_worksheet.txt
"""
import argparse
import json
import csv
import sys


def load(path):
    with open(path, encoding="utf-8") as f:
        return {q["q_id"]: q for q in json.load(f)}


def short(s, n=180):
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top100-h", required=True)
    ap.add_argument("--top100-n", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--show-top", type=int, default=10,
                    help="Show top-K per config (default 10).")
    args = ap.parse_args()

    H = load(args.top100_h)
    N = load(args.top100_n)
    with open(args.questions, newline="", encoding="utf-8") as f:
        questions = list(csv.DictReader(f))

    lines = []
    def W(s=""): lines.append(s)

    W("=" * 88)
    W("LABELLING WORKSHEET — §V.B synopsis-span ablation")
    W("=" * 88)
    W()
    W("For each question:")
    W("  1) For SUMMARISATION questions, find the §synopsis chunk for the target")
    W("     chapter in H's hits. Its `id` goes into questions.csv `gold_synopsis_id`.")
    W("     ALSO copy it into `gold_chunk_ids` (it's a relevant chunk).")
    W("  2) Identify 1–2 supporting body chunks (any rank, any config) and add their")
    W("     IDs to `gold_chunk_ids`, comma-separated.")
    W("  3) For CONTROL questions, find the 1–3 most relevant body chunks across")
    W("     both configs. Add their IDs to `gold_chunk_ids`.")
    W("  4) Write a 1–3 sentence `reference_answer` from the gold chunks.")
    W()
    W("Chunk IDs in this worksheet are abbreviated for readability — paste the")
    W("FULL ID into the CSV (use the bracketed full id, not the abbreviated one).")
    W()

    for q in questions:
        qid = q["q_id"]
        W()
        W("─" * 88)
        W(f"Q{qid:>2}  [{q['subset']}]  target_chapter={q.get('target_chapter','—'):>2}")
        W(f"     {q['q_text']}")
        W("─" * 88)

        for label, src in [("H (python_tutorial)", H), ("N (python_tut_N)", N)]:
            hits = (src.get(qid) or {}).get("hits") or []
            if not hits:
                W(f"  {label}: (no hits)")
                continue
            W(f"  {label}:")
            # Synopsis chunks in top-K first (most informative for labelling)
            top = hits[: args.show_top]
            syns = [h for h in top if (h.get("section") == "synopsis")]
            bods = [h for h in top if (h.get("section") != "synopsis")]
            for h in syns:
                ic = h.get("synopsis_input_chars")
                ic_str = f"input={ic}c" if ic is not None else ""
                W(f"    [rank {h['rank']:>2}] §synopsis (chap.idx={h.get('synopsized_section_idx')}, p.{h.get('page_from')}-{h.get('page_to')}, {ic_str})")
                W(f"      id = {h['id']}")
                W(f"      {short(h['doc_snippet'])}")
            for h in bods[:5]:   # cap body chunks to keep worksheet readable
                W(f"    [rank {h['rank']:>2}] §{h.get('section')} (p.{h.get('page_from')}-{h.get('page_to')})")
                W(f"      id = {h['id']}")
                W(f"      {short(h['doc_snippet'])}")
        W()

    with open(args.out, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {args.out} ({len(questions)} questions)", file=sys.stderr)


if __name__ == "__main__":
    main()
