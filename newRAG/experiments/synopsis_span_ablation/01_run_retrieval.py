"""
Step 4: top-10 retrieval per (question × config). Records chunk IDs, section
types, and the binary Recall@10 / Synopsis-Recall@10 outcomes by comparing
top-10 IDs against the gold_chunk_ids and gold_synopsis_id columns of
questions.csv.

Usage:
    python3 01_run_retrieval.py \\
        --questions questions.csv \\
        --collections python_tut_H python_tut_N \\
        --chroma http://92.247.133.89:63140 \\
        --ollama http://195.230.127.226:11850 \\
        --embed-model mxbai-embed-large \\
        --out retrieval.csv

The output CSV columns:
    q_id, config, subset, target_chapter,
    topk_chunk_ids, topk_sections, synopsis_in_topk,
    recall_at_10, synopsis_recall_at_10

Both recall fields are 0/1 indicators. synopsis_recall_at_10 is blank for
control questions.
"""
import argparse
import csv
import sys
import requests


def embed(ollama_url, model, text, timeout=60):
    r = requests.post(ollama_url.rstrip("/") + "/api/embeddings",
                      json={"model": model, "prompt": text}, timeout=timeout)
    r.raise_for_status()
    emb = r.json().get("embedding")
    if not emb:
        raise RuntimeError("empty embedding")
    return emb


def get_collection_id(chroma_url, name):
    r = requests.get(
        f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/databases/default_database/collections",
        timeout=15)
    r.raise_for_status()
    for c in r.json():
        if c.get("name") == name:
            return c["id"]
    raise RuntimeError(f"collection {name!r} not found")


def query_top10(chroma_url, coll_id, embedding):
    r = requests.post(
        f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/databases/default_database/collections/{coll_id}/query",
        json={"query_embeddings": [embedding], "n_results": 10,
              "include": ["metadatas"]},
        timeout=60)
    r.raise_for_status()
    return r.json()


def parse_gold(field):
    """Comma-separated → list of stripped IDs."""
    if not field:
        return []
    return [x.strip() for x in field.split(",") if x.strip()]


def config_label(name):
    """Map collection name to short config label (H or N).

    The naïve ingest is always named with an `_N` / `-N` suffix; treat any
    other collection name as the hierarchy-aware (H) side. This lets the
    legacy `python_tutorial` collection from the very first ingest play
    the H role without a rename.
    """
    if name.endswith("_N") or name.endswith("-N"):
        return "N"
    return "H"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--collections", nargs="+", required=True,
                    help="One or more collection names (H, N)")
    ap.add_argument("--chroma", required=True)
    ap.add_argument("--ollama", required=True)
    ap.add_argument("--embed-model", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    coll_ids = {}
    for name in args.collections:
        coll_ids[name] = get_collection_id(args.chroma, name)
        print(f"  {name:20s} -> {coll_ids[name]}", file=sys.stderr)

    with open(args.questions, newline="", encoding="utf-8") as f:
        questions = list(csv.DictReader(f))

    rows = []
    for q in questions:
        qid = q["q_id"]
        text = q["q_text"]
        subset = q["subset"]
        gold_chunks = set(parse_gold(q.get("gold_chunk_ids", "")))
        # gold_synopsis_id is comma-separated: typically the H synopsis
        # chunk's id AND the N synopsis chunk's id (which differ by both
        # source-hash and the `-naive` suffix). Either one counts as a
        # successful synopsis recall, because both refer to the same
        # semantic concept: "the synopsis chunk for the target chapter".
        gold_synopsis_set = set(parse_gold(q.get("gold_synopsis_id", "")))

        if not gold_chunks:
            print(f"q{qid:>3} WARN: gold_chunk_ids is empty — Recall@10 will be 0",
                  file=sys.stderr)

        try:
            emb = embed(args.ollama, args.embed_model, text)
        except Exception as e:
            print(f"q{qid:>3} ! embed failed: {e}", file=sys.stderr)
            continue

        for cname in args.collections:
            try:
                res = query_top10(args.chroma, coll_ids[cname], emb)
            except Exception as e:
                print(f"q{qid:>3} {cname}: ! query failed: {e}", file=sys.stderr)
                continue
            ids       = (res.get("ids")       or [[]])[0]
            metadatas = (res.get("metadatas") or [[]])[0]
            sections  = [(m or {}).get("section", "") for m in metadatas]
            topset    = set(ids)
            recall    = 1 if (topset & gold_chunks) else 0
            syn_recall = ""
            if subset == "summarisation":
                syn_recall = 1 if (gold_synopsis_set & topset) else 0
            synopsis_in_topk = sum(1 for s in sections if s == "synopsis")

            rows.append({
                "q_id": qid,
                "config": config_label(cname),
                "subset": subset,
                "target_chapter": q.get("target_chapter", ""),
                "topk_chunk_ids": ",".join(ids),
                "topk_sections": ",".join(sections),
                "synopsis_in_topk": synopsis_in_topk,
                "recall_at_10": recall,
                "synopsis_recall_at_10": syn_recall,
            })

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "q_id", "config", "subset", "target_chapter",
            "topk_chunk_ids", "topk_sections", "synopsis_in_topk",
            "recall_at_10", "synopsis_recall_at_10",
        ])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\nwrote {args.out} ({len(rows)} rows)", file=sys.stderr)


if __name__ == "__main__":
    main()
