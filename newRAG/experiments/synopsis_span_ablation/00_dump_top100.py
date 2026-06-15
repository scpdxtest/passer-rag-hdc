"""
Step 3 helper: dump top-100 retrievals per question against a Chroma collection,
so you can manually identify gold-relevant chunks (and the gold §synopsis chunk
for summarisation questions). Output is JSON with full chunk metadata + first
500 chars of each chunk's document so you can read and label without round-
tripping to the vector store.

Run twice — once per collection (H and N) — and union the relevant-chunk IDs
when you fill in `gold_chunk_ids` in questions.csv.

Usage:
    python3 00_dump_top100.py \\
        --collection python_tut_H \\
        --chroma http://92.247.133.89:63140 \\
        --ollama http://195.230.127.226:11850 \\
        --embed-model mxbai-embed-large \\
        --questions questions.csv \\
        --out top100_H.json
"""
import argparse
import csv
import json
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


def query(chroma_url, coll_id, embedding, n=100):
    r = requests.post(
        f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/databases/default_database/collections/{coll_id}/query",
        json={"query_embeddings": [embedding], "n_results": n,
              "include": ["metadatas", "documents", "distances"]},
        timeout=60)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--chroma", required=True)
    ap.add_argument("--ollama", required=True)
    ap.add_argument("--embed-model", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--snippet-chars", type=int, default=500)
    args = ap.parse_args()

    print(f"resolving collection {args.collection!r}…", file=sys.stderr)
    coll_id = get_collection_id(args.chroma, args.collection)
    print(f"  id = {coll_id}", file=sys.stderr)

    with open(args.questions, newline="", encoding="utf-8") as f:
        questions = list(csv.DictReader(f))

    out = []
    for q in questions:
        qid = q["q_id"]
        text = q["q_text"]
        print(f"q{qid:>3} {text[:60]}…", file=sys.stderr)
        try:
            emb = embed(args.ollama, args.embed_model, text)
            res = query(args.chroma, coll_id, emb, n=args.n)
        except Exception as e:
            print(f"  ! error: {e}", file=sys.stderr)
            out.append({"q_id": qid, "q_text": text, "error": str(e)})
            continue
        ids       = (res.get("ids")       or [[]])[0]
        metadatas = (res.get("metadatas") or [[]])[0]
        documents = (res.get("documents") or [[]])[0]
        distances = (res.get("distances") or [[]])[0]
        hits = []
        for i, _id in enumerate(ids):
            meta = (metadatas[i] if i < len(metadatas) else {}) or {}
            doc  = documents[i] if i < len(documents) else ""
            dist = distances[i] if i < len(distances) else None
            hits.append({
                "rank": i + 1,
                "id": _id,
                "distance": dist,
                "section": meta.get("section"),
                "synopsized_section": meta.get("synopsized_section"),
                "synopsized_section_idx": meta.get("synopsized_section_idx"),
                "page_from": meta.get("page_from"),
                "page_to": meta.get("page_to"),
                "synopsis_naive": meta.get("synopsis_naive"),
                "synopsis_input_chars": meta.get("synopsis_input_chars"),
                "doc_snippet": (doc or "")[:args.snippet_chars],
            })
        out.append({
            "q_id": qid, "subset": q["subset"], "target_chapter": q.get("target_chapter"),
            "q_text": text, "hits": hits,
        })

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nwrote {args.out} ({len(out)} questions × up to {args.n} hits)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
