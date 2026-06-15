"""
Step 6: per-question metric computation. Joins retrieval.csv and answers.csv
on (q_id, config), pulls reference_answer from questions.csv, runs the LLM
judge for Faithfulness, and writes metrics.csv.

Recall@10 and Synopsis-Recall@10 are carried forward from retrieval.csv;
hedged is carried forward from answers.csv. The new column is `faithfulness`,
a score in [0, 1] from a Mistral-judge that asks: "given the gold reference
answer and the model's answer, what fraction of the model's content is
supported by — and consistent with — the reference?".

Usage:
    python3 03_compute_metrics.py \\
        --questions questions.csv \\
        --retrieval retrieval.csv \\
        --answers answers.csv \\
        --ollama http://195.230.127.226:11850 \\
        --llm mistral:latest \\
        --out metrics.csv

Output columns:
    q_id, config, subset, target_chapter,
    recall_at_10, synopsis_recall_at_10, hedged, faithfulness
"""
import argparse
import csv
import re
import sys
import requests


JUDGE_PROMPT = """You are scoring the faithfulness of an AI-generated answer to a reference answer.

Question: {question}

REFERENCE ANSWER (gold):
{reference}

AI-GENERATED ANSWER:
{answer}

Faithfulness is defined as the proportion of statements in the AI-generated
answer that are SUPPORTED by — and consistent with — the reference answer.
Statements that the reference neither corroborates nor contradicts count as
not supported.

Output your decision in EXACTLY this format on its own line, before any
explanation:

FINAL_SCORE: <a single decimal in [0,1], two decimal places>

If the AI-generated answer is empty or pure refusal text (e.g. "I don't know"),
output FINAL_SCORE: 0.00.

You MAY add a brief justification on later lines, but the FINAL_SCORE line MUST
come first. Begin your response with "FINAL_SCORE:" — nothing before it.
"""

# Primary parse: look for "FINAL_SCORE: X" anywhere in the response.
FINAL_SCORE_RX = re.compile(r"FINAL[_\s]?SCORE\s*[:=]\s*(\d?\.\d+|[01](?:\.0+)?)",
                            re.IGNORECASE)
# Fallback: first number in the response that looks like a [0,1] score.
SCORE_RX = re.compile(r"(\d?\.\d+|[01](?:\.0+)?)")


def _extract_response(j):
    """Combine response + thinking fields. Some reasoning models (gemma4,
    deepseek-r1) emit chain-of-thought to a separate 'thinking' field
    that Ollama strips from 'response'. We accept either / both, prefer
    'response' if non-empty, else fall back to 'thinking'."""
    resp = (j.get("response") or "").strip()
    if resp:
        return resp
    # /api/chat
    msg = (j.get("message") or {})
    if msg.get("content"):
        return msg["content"].strip()
    # Reasoning models — look at the thinking field as a last resort;
    # FINAL_SCORE: may have been emitted inside the chain-of-thought.
    think = (j.get("thinking") or "").strip()
    if not think and msg.get("thinking"):
        think = msg["thinking"].strip()
    return think


def ollama_generate(ollama_url, model, prompt, timeout=600):
    # num_predict=2048 to give reasoning models (gemma4) enough budget
    # to finish chain-of-thought AND emit the final visible answer.
    # think=False asks Ollama to disable hidden reasoning entirely
    # (supported on newer Ollama versions; ignored if not supported).
    r = requests.post(
        ollama_url.rstrip("/") + "/api/generate",
        json={"model": model, "prompt": prompt, "stream": False,
              "think": False,
              "options": {"temperature": 0.0, "num_predict": 2048}},
        timeout=timeout)
    r.raise_for_status()
    j = r.json()
    return _extract_response(j), j


def ollama_chat(ollama_url, model, prompt, timeout=600):
    r = requests.post(
        ollama_url.rstrip("/") + "/api/chat",
        json={"model": model,
              "messages": [{"role": "user", "content": prompt}],
              "stream": False,
              "think": False,
              "options": {"temperature": 0.0, "num_predict": 2048}},
        timeout=timeout)
    r.raise_for_status()
    j = r.json()
    return _extract_response(j), j


def judge_with_retry(ollama_url, model, prompt, max_attempts=3):
    """Call ollama_generate; if the response is empty or unparseable,
    retry, and on the final attempt fall back to /api/chat. Returns
    (parsed_score_or_None, last_raw_response, last_diag_dict)."""
    last_raw = ""
    last_diag = {}
    for attempt in range(1, max_attempts + 1):
        if attempt < max_attempts:
            raw, j = ollama_generate(ollama_url, model, prompt)
        else:
            # final attempt: switch to /api/chat
            raw, j = ollama_chat(ollama_url, model, prompt)
        last_raw = raw
        last_diag = j
        score = parse_score(raw)
        if score is not None:
            return score, raw, j
        if attempt < max_attempts:
            print(f"   retry {attempt}/{max_attempts}: got {raw!r} "
                  f"(done_reason={j.get('done_reason')}, "
                  f"eval_count={j.get('eval_count')})",
                  file=sys.stderr)
    return None, last_raw, last_diag


def parse_score(raw):
    """Pluck a [0, 1] number out of the judge's raw text. Prefer the
    explicit FINAL_SCORE marker; fall back to the first decimal-looking
    number if the model emitted one but skipped the marker. Returns None
    if nothing plausible was emitted."""
    if not raw:
        return None
    m = FINAL_SCORE_RX.search(raw)
    if not m:
        m = SCORE_RX.search(raw)
    if not m:
        return None
    try:
        v = float(m.group(1))
        return max(0.0, min(1.0, v))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--retrieval", required=True)
    ap.add_argument("--answers", required=True)
    ap.add_argument("--ollama", required=True)
    ap.add_argument("--llm", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.questions, newline="", encoding="utf-8") as f:
        q_by_id = {row["q_id"]: row for row in csv.DictReader(f)}

    with open(args.retrieval, newline="", encoding="utf-8") as f:
        retrieval_rows = list(csv.DictReader(f))

    with open(args.answers, newline="", encoding="utf-8") as f:
        ans_by_key = {(row["q_id"], row["config"]): row for row in csv.DictReader(f)}

    rows = []
    for rr in retrieval_rows:
        qid = rr["q_id"]
        cfg = rr["config"]
        q = q_by_id.get(qid, {})
        ans = ans_by_key.get((qid, cfg), {})

        reference = (q.get("reference_answer") or "").strip()
        answer    = (ans.get("answer_text")   or "").strip()
        question  = q.get("q_text", "")

        if not reference:
            print(f"q{qid:>3} {cfg}: ! reference_answer is empty — faithfulness will be NaN",
                  file=sys.stderr)
            faith = ""
        elif not answer or answer.startswith("[LLM ERROR"):
            faith = 0.0
        else:
            prompt = JUDGE_PROMPT.format(
                question=question, reference=reference, answer=answer)
            try:
                faith, raw, diag = judge_with_retry(args.ollama, args.llm, prompt)
                if faith is None:
                    print(f"q{qid:>3} {cfg}: ! judge returned unparseable after retries "
                          f"(last={raw!r}, "
                          f"done_reason={diag.get('done_reason')}, "
                          f"eval_count={diag.get('eval_count')}, "
                          f"prompt_eval_count={diag.get('prompt_eval_count')})",
                          file=sys.stderr)
                    faith = ""
            except Exception as e:
                print(f"q{qid:>3} {cfg}: ! judge error: {e}", file=sys.stderr)
                faith = ""

        rows.append({
            "q_id": qid,
            "config": cfg,
            "subset": rr.get("subset", ""),
            "target_chapter": rr.get("target_chapter", ""),
            "recall_at_10": rr.get("recall_at_10", ""),
            "synopsis_recall_at_10": rr.get("synopsis_recall_at_10", ""),
            "hedged": ans.get("hedged", ""),
            "faithfulness": faith,
        })
        print(f"q{qid:>3} {cfg}: recall={rr.get('recall_at_10')} "
              f"syn_recall={rr.get('synopsis_recall_at_10')} "
              f"hedged={ans.get('hedged')} faith={faith}",
              file=sys.stderr)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "q_id", "config", "subset", "target_chapter",
            "recall_at_10", "synopsis_recall_at_10", "hedged", "faithfulness",
        ])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\nwrote {args.out} ({len(rows)} rows)", file=sys.stderr)


if __name__ == "__main__":
    main()
