"""
Step 5: end-to-end RAG answer generation per (question × config). Uses the
top-10 chunks recorded by 01_run_retrieval.py as context, runs the same chat
system prompt the React app uses, and records the generated answer.

Also computes the per-answer hedging-rate indicator: a binary flag set if the
answer matches the regex {not mentioned|unclear|cannot determine|no information|
not specified|not explicitly|does not (mention|specify|provide)}.

Usage:
    python3 02_run_answers.py \\
        --retrieval retrieval.csv \\
        --questions questions.csv \\
        --chroma http://92.247.133.89:63140 \\
        --ollama http://195.230.127.226:11850 \\
        --llm mistral:latest \\
        --out answers.csv

The output CSV columns:
    q_id, config, subset, target_chapter,
    answer_text, hedged, context_chars

The system prompt mirrors the one in src/component/chatNewRAG.js so the answer-
generation conditions match the production chat behaviour as closely as
possible.
"""
import argparse
import csv
import re
import sys
import requests


SYSTEM_PROMPT_TEMPLATE = """You are an expert assistant answering questions about a technical document using the provided context.

Rules:
- Answer ONLY from the context excerpts below. If the context is insufficient, say so plainly — do not invent facts.
- When you cite, include the chunk reference in brackets (e.g. [chunk 3 §section p.4]).
- Be concise. Quote the document verbatim where it strengthens the answer.

Question: {question}

Context excerpts:
----
{context}
----

Answer:"""

HEDGE_RX = re.compile(
    r"\b(not mentioned|unclear|cannot determine|no information|"
    r"not specified|not explicitly|does not (mention|specify|provide|state|"
    r"include|address|cover|elaborate|discuss))\b",
    re.IGNORECASE)


def get_collection_id(chroma_url, name):
    r = requests.get(
        f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/databases/default_database/collections",
        timeout=15)
    r.raise_for_status()
    for c in r.json():
        if c.get("name") == name:
            return c["id"]
    raise RuntimeError(f"collection {name!r} not found")


def fetch_chunk_docs(chroma_url, coll_id, ids):
    """Pull document + metadata for a list of chunk IDs (top-10 of one query)."""
    if not ids:
        return []
    r = requests.post(
        f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/databases/default_database/collections/{coll_id}/get",
        json={"ids": ids, "include": ["metadatas", "documents"]},
        timeout=30)
    r.raise_for_status()
    j = r.json()
    by_id = {}
    for i, _id in enumerate(j.get("ids", [])):
        by_id[_id] = {
            "doc": (j.get("documents") or [])[i] if i < len(j.get("documents") or []) else "",
            "meta": (j.get("metadatas") or [])[i] if i < len(j.get("metadatas") or []) else {},
        }
    # Preserve original order of `ids`
    return [by_id.get(_id, {"doc": "", "meta": {}}) for _id in ids]


def build_context(chunks):
    parts = []
    for i, ch in enumerate(chunks):
        meta = ch.get("meta") or {}
        pid = meta.get("paper_id", "?")
        sec = meta.get("section", "?")
        pf  = meta.get("page_from", "?")
        pt  = meta.get("page_to", "?")
        pg  = f"{pf}-{pt}" if pf and pt and pf != pt else pf
        parts.append(f"[chunk {i+1} | P:{pid} §{sec} p.{pg}]\n{ch.get('doc', '')}")
    return "\n\n".join(parts)


def ollama_generate(ollama_url, model, prompt, timeout=180):
    r = requests.post(
        ollama_url.rstrip("/") + "/api/generate",
        json={"model": model, "prompt": prompt, "stream": False,
              "options": {"temperature": 0.1, "num_predict": 400}},
        timeout=timeout)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--retrieval", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--chroma", required=True)
    ap.add_argument("--ollama", required=True)
    ap.add_argument("--llm", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--collections", nargs="+", default=["python_tut_H", "python_tut_N"],
                    help="Collection names matching the config labels (H, N).")
    args = ap.parse_args()

    # Build {config: collection_id}. Anything not ending in _N / -N is treated
    # as H (so the legacy `python_tutorial` collection plays the H role).
    coll_ids = {}
    for cname in args.collections:
        cid = get_collection_id(args.chroma, cname)
        cfg = "N" if cname.endswith("_N") or cname.endswith("-N") else "H"
        coll_ids[cfg] = cid
        print(f"  {cfg} -> {cname} = {cid}", file=sys.stderr)

    with open(args.questions, newline="", encoding="utf-8") as f:
        q_by_id = {row["q_id"]: row for row in csv.DictReader(f)}

    with open(args.retrieval, newline="", encoding="utf-8") as f:
        retrieval_rows = list(csv.DictReader(f))

    rows = []
    for rr in retrieval_rows:
        qid = rr["q_id"]
        cfg = rr["config"]
        q = q_by_id.get(qid, {})
        qtext = q.get("q_text", "")
        ids = [x.strip() for x in (rr.get("topk_chunk_ids") or "").split(",") if x.strip()]
        coll_id = coll_ids.get(cfg)
        if not coll_id:
            print(f"q{qid:>3} {cfg}: no collection mapped, skipping", file=sys.stderr)
            continue

        chunks = fetch_chunk_docs(args.chroma, coll_id, ids)
        ctx = build_context(chunks)
        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            question=qtext,
            context=ctx or "(no chunks retrieved — answer cautiously)")

        try:
            answer = ollama_generate(args.ollama, args.llm, prompt)
        except Exception as e:
            print(f"q{qid:>3} {cfg}: ! LLM error: {e}", file=sys.stderr)
            answer = f"[LLM ERROR: {e}]"

        hedged = 1 if HEDGE_RX.search(answer) else 0
        rows.append({
            "q_id": qid,
            "config": cfg,
            "subset": rr.get("subset", ""),
            "target_chapter": rr.get("target_chapter", ""),
            "answer_text": answer,
            "hedged": hedged,
            "context_chars": len(ctx),
        })
        print(f"q{qid:>3} {cfg}: hedged={hedged} ctx={len(ctx):>5} answer={answer[:60]!r}",
              file=sys.stderr)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "q_id", "config", "subset", "target_chapter",
            "answer_text", "hedged", "context_chars",
        ])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\nwrote {args.out} ({len(rows)} rows)", file=sys.stderr)


if __name__ == "__main__":
    main()
