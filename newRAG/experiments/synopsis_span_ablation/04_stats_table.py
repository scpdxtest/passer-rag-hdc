"""
Step 7: paired statistical tests + Table III emission.

For each binary metric (recall_at_10, synopsis_recall_at_10, hedged):
    McNemar's exact test on the H vs N paired samples within a subset, with
    Bonferroni correction across the 3 binary metrics within each subset row.

For faithfulness:
    Wilcoxon signed-rank test on per-question H vs N paired faithfulness scores.
    95 % bootstrap CI on the mean of each config (10 000 resamples).

Also computes the smoking-gun row: mean `synopsis_input_chars` per config,
read from chunk metadata of the §synopsis chunks in each collection.

Usage:
    python3 04_stats_table.py \\
        --metrics metrics.csv \\
        --questions questions.csv \\
        --chroma http://92.247.133.89:63140 \\
        --collections python_tut_H python_tut_N \\
        --out analysis.txt \\
        --table3 table3.md

Reads:
    metrics.csv  — per (q_id × config) metric values
    questions.csv — for target_chapter / sub-subset bookkeeping
    chunk metadata via /collections/<id>/get — for synopsis_input_chars

Writes:
    analysis.txt — human-readable per-subset breakdown
    table3.md    — paste-ready replacement for the placeholder Table III in
                   paper_PaSSER_RAG_BdKCSE_ext.md
"""
import argparse
import csv
import math
import statistics
import sys
import requests


# -------- statistics --------------------------------------------------

def mcnemar_exact(b, c):
    """McNemar's exact two-sided test on a 2x2 paired-discordant table.

    b = count of pairs (H wins, N loses)
    c = count of pairs (N wins, H loses)
    (concordant pairs are ignored — McNemar's design)

    Returns p-value, two-sided.
    """
    n = b + c
    if n == 0:
        return 1.0
    # Binomial(n, 0.5) two-sided exact p
    k = min(b, c)
    # P(X <= k) under Binom(n, 0.5)
    p_one = sum(_binom(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * p_one)


def _binom(n, k):
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def wilcoxon_signed_rank(diffs):
    """Two-sided Wilcoxon signed-rank test. Returns p (approximate normal
    approximation; for n < 25 we fall back to a small-sample exact via the
    sign-rank distribution). Diffs are H - N per question."""
    diffs = [d for d in diffs if d != 0]
    n = len(diffs)
    if n == 0:
        return 1.0
    abs_diffs = sorted([(abs(d), i, d) for i, d in enumerate(diffs)])
    # rank with ties averaged
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs_diffs[j + 1][0] == abs_diffs[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            _, orig_i, _ = abs_diffs[k]
            ranks[orig_i] = avg_rank
        i = j + 1
    W_plus = sum(r for r, d in zip(ranks, diffs) if d > 0)
    W_minus = sum(r for r, d in zip(ranks, diffs) if d < 0)
    W = min(W_plus, W_minus)
    if n >= 20:
        mu = n * (n + 1) / 4
        sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
        z = (W - mu) / sigma
        # two-sided p via normal approximation
        from math import erf, sqrt
        p = 2 * (1 - 0.5 * (1 + erf(abs(z) / sqrt(2))))
        return p
    # small-sample exact: enumerate all 2^n sign patterns
    p = 0
    total = 2 ** n
    for mask in range(total):
        w_plus = sum(ranks[k] for k in range(n) if (mask >> k) & 1)
        if w_plus <= W or w_plus >= sum(ranks) - W:
            p += 1
    return min(1.0, p / total)


def wilson_ci(k, n, z=1.96):
    """Wilson 95 % CI for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half   = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_mean_ci(values, n_resamples=10000, seed=42):
    """Bias-corrected mean + 95 % bootstrap CI. Deterministic via fixed seed."""
    if not values:
        return (float("nan"), float("nan"), float("nan"))
    import random
    rng = random.Random(seed)
    means = []
    n = len(values)
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * n_resamples)]
    hi = means[int(0.975 * n_resamples)]
    return (sum(values) / n, lo, hi)


# -------- chunk-metadata reader ----------------------------------------

def get_collection_id(chroma_url, name):
    r = requests.get(
        f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/databases/default_database/collections",
        timeout=15)
    r.raise_for_status()
    for c in r.json():
        if c.get("name") == name:
            return c["id"]
    raise RuntimeError(f"collection {name!r} not found")


def mean_synopsis_input_chars(chroma_url, coll_id):
    """Average synopsis_input_chars across all synopsis chunks in a collection."""
    r = requests.post(
        f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/databases/default_database/collections/{coll_id}/get",
        json={"limit": 1000, "where": {"section": "synopsis"},
              "include": ["metadatas"]},
        timeout=60)
    r.raise_for_status()
    j = r.json()
    chars = []
    for m in (j.get("metadatas") or []):
        v = (m or {}).get("synopsis_input_chars")
        if isinstance(v, (int, float)):
            chars.append(int(v))
    return chars


# -------- main ----------------------------------------------------------

def by_pair(metrics, metric_col):
    """Build {q_id: (H_val, N_val)} for a given metric column, skipping blanks."""
    out = {}
    for m in metrics:
        q = m["q_id"]
        v = m.get(metric_col)
        try:
            v = int(v) if v not in ("", None) else None
        except (ValueError, TypeError):
            v = None
        if v is None:
            continue
        cfg = m["config"]
        cur = out.get(q, [None, None])
        if   cfg == "H":  cur[0] = v
        elif cfg == "N":  cur[1] = v
        out[q] = cur
    return {q: tuple(v) for q, v in out.items() if v[0] is not None and v[1] is not None}


def by_pair_float(metrics, metric_col):
    out = {}
    for m in metrics:
        q = m["q_id"]
        v = m.get(metric_col)
        try:
            v = float(v) if v not in ("", None) else None
        except (ValueError, TypeError):
            v = None
        if v is None:
            continue
        cfg = m["config"]
        cur = out.get(q, [None, None])
        if   cfg == "H":  cur[0] = v
        elif cfg == "N":  cur[1] = v
        out[q] = cur
    return {q: tuple(v) for q, v in out.items() if v[0] is not None and v[1] is not None}


def mcnemar_paired(pairs):
    """pairs: dict q_id -> (h_val, n_val) of 0/1. Returns (b, c, p_value)."""
    b = sum(1 for h, n in pairs.values() if h == 1 and n == 0)
    c = sum(1 for h, n in pairs.values() if h == 0 and n == 1)
    return b, c, mcnemar_exact(b, c)


def fmt_recall(pairs):
    """Return formatted (rate_H, rate_N, CI_H, CI_N)."""
    n = len(pairs)
    k_H = sum(1 for h, _ in pairs.values() if h == 1)
    k_N = sum(1 for _, nv in pairs.values() if nv == 1)
    return (k_H, k_N, n,
            wilson_ci(k_H, n),
            wilson_ci(k_N, n))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metrics", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--chroma", required=True)
    ap.add_argument("--collections", nargs="+",
                    default=["python_tut_H", "python_tut_N"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--table3", required=True)
    args = ap.parse_args()

    # Load metrics + question chapter map
    with open(args.metrics, newline="", encoding="utf-8") as f:
        metrics = list(csv.DictReader(f))
    with open(args.questions, newline="", encoding="utf-8") as f:
        q_by_id = {row["q_id"]: row for row in csv.DictReader(f)}

    # Smoking-gun: mean synopsis_input_chars per config
    print("fetching chunk metadata for smoking-gun row…", file=sys.stderr)
    smoking_gun = {}
    for cname in args.collections:
        # Anything not ending in _N / -N is treated as H.
        cfg = "N" if cname.endswith("_N") or cname.endswith("-N") else "H"
        cid = get_collection_id(args.chroma, cname)
        chars = mean_synopsis_input_chars(args.chroma, cid)
        if chars:
            mean = sum(chars) / len(chars)
            med  = statistics.median(chars)
            mn, mx = min(chars), max(chars)
            smoking_gun[cfg] = {"mean": mean, "median": med, "min": mn, "max": mx, "n": len(chars)}
        else:
            smoking_gun[cfg] = None

    # Split per subset
    summ = [m for m in metrics if m["subset"] == "summarisation"]
    ctrl = [m for m in metrics if m["subset"] == "control"]

    def chapters_with_subsections(target_chapter_str):
        # Chapter 1 (Whetting Your Appetite) and Chapter 13 (What Now?) have no
        # \d+\.\d+ sub-sections in the Python Tutorial 3.7.0 source. Everything
        # else is sub-sectioned for the purposes of this experiment.
        try:
            n = int(target_chapter_str)
        except (ValueError, TypeError):
            return True   # treat unknown as sub-sectioned
        return n not in (1, 13, 14)

    summ_sub = [m for m in summ
                if chapters_with_subsections(q_by_id.get(m["q_id"], {}).get("target_chapter"))]
    summ_nosub = [m for m in summ
                  if not chapters_with_subsections(q_by_id.get(m["q_id"], {}).get("target_chapter"))]

    # ---------- analysis.txt ----------
    out_lines = []
    def emit(s=""): out_lines.append(s)

    emit("=" * 72)
    emit("§V.B — Hierarchy-Aware vs Naïve Synopsis Span Ablation")
    emit("Python Tutorial 3.7.0, manual profile, mistral:latest reader + judge")
    emit("=" * 72)
    emit()
    emit("SMOKING-GUN ROW (mean synopsis_input_chars per config)")
    emit("-" * 72)
    for cfg in ("H", "N"):
        sg = smoking_gun.get(cfg)
        if sg:
            emit(f"  {cfg}: n={sg['n']:>3}  mean={sg['mean']:>9.1f}  "
                 f"median={sg['median']:>7.1f}  range=[{sg['min']}..{sg['max']}]")
        else:
            emit(f"  {cfg}: (no synopsis chunks found)")
    emit()

    def emit_block(label, rows):
        emit("=" * 72)
        emit(f"SUBSET: {label}  (n={len(set(m['q_id'] for m in rows))} questions)")
        emit("=" * 72)

        # binary metrics
        for metric_col, metric_label in [
            ("recall_at_10",          "Recall@10"),
            ("synopsis_recall_at_10", "Synopsis-Recall@10"),
            ("hedged",                "Hedging Rate"),
        ]:
            pairs = by_pair(rows, metric_col)
            if not pairs:
                emit(f"  {metric_label:22s}: (no pairs)")
                continue
            k_H, k_N, n, ci_H, ci_N = fmt_recall(pairs)
            b, c, p = mcnemar_paired(pairs)
            # Bonferroni across the 3 binary metrics per subset
            p_bonf = min(1.0, p * 3)
            emit(f"  {metric_label:22s}: "
                 f"H = {k_H}/{n} ({k_H/n*100:>5.1f}%) [{ci_H[0]*100:>5.1f}, {ci_H[1]*100:>5.1f}]  "
                 f"N = {k_N}/{n} ({k_N/n*100:>5.1f}%) [{ci_N[0]*100:>5.1f}, {ci_N[1]*100:>5.1f}]")
            emit(f"  {'':22s}  McNemar b={b} c={c}  p={p:.4f}  p_bonf={p_bonf:.4f}")

        # faithfulness
        fpairs = by_pair_float(rows, "faithfulness")
        if fpairs:
            H = [h for h, n in fpairs.values()]
            N = [n for h, n in fpairs.values()]
            mH, lH, uH = bootstrap_mean_ci(H)
            mN, lN, uN = bootstrap_mean_ci(N)
            diffs = [h - n for h, n in fpairs.values()]
            p = wilcoxon_signed_rank(diffs)
            emit(f"  {'Faithfulness':22s}: "
                 f"H = {mH:.3f} [{lH:.3f}, {uH:.3f}]  "
                 f"N = {mN:.3f} [{lN:.3f}, {uN:.3f}]")
            emit(f"  {'':22s}  Wilcoxon p={p:.4f}")
        else:
            emit(f"  {'Faithfulness':22s}: (no pairs)")
        emit()

    emit_block("summarisation (all)", summ)
    if summ_sub:
        emit_block("summarisation — sub-sectioned chapters", summ_sub)
    if summ_nosub:
        emit_block("summarisation — non-sub-sectioned chapters (within-experiment control)", summ_nosub)
    emit_block("control", ctrl)

    with open(args.out, "w") as f:
        f.write("\n".join(out_lines))
    print(f"wrote {args.out}", file=sys.stderr)

    # ---------- table3.md ----------
    def fmt_pct(k, n, ci):
        if n == 0:
            return "—"
        return f"{k/n*100:.1f}% [{ci[0]*100:.1f}, {ci[1]*100:.1f}]"

    def fmt_float(m, lo, hi):
        return f"{m:.2f} [{lo:.2f}, {hi:.2f}]"

    def get_row(subset_rows, metric_col):
        pairs = by_pair(subset_rows, metric_col)
        if not pairs:
            return "—", "—", "—"
        k_H, k_N, n, ci_H, ci_N = fmt_recall(pairs)
        _, _, p = mcnemar_paired(pairs)
        return fmt_pct(k_H, n, ci_H), fmt_pct(k_N, n, ci_N), f"{p:.3f}"

    def get_row_float(subset_rows):
        fpairs = by_pair_float(subset_rows, "faithfulness")
        if not fpairs:
            return "—", "—", "—"
        H = [h for h, n in fpairs.values()]
        N = [n for h, n in fpairs.values()]
        mH, lH, uH = bootstrap_mean_ci(H)
        mN, lN, uN = bootstrap_mean_ci(N)
        diffs = [h - n for h, n in fpairs.values()]
        p = wilcoxon_signed_rank(diffs)
        return fmt_float(mH, lH, uH), fmt_float(mN, lN, uN), f"{p:.3f}"

    sg_H = smoking_gun.get("H")
    sg_N = smoking_gun.get("N")
    sg_H_str = f"{sg_H['mean']:.0f} (mean, n={sg_H['n']})" if sg_H else "—"
    sg_N_str = f"{sg_N['mean']:.0f} (mean, n={sg_N['n']})" if sg_N else "—"

    n_summ = len(set(m["q_id"] for m in summ))
    n_ctrl = len(set(m["q_id"] for m in ctrl))

    h_r10, n_r10, p_r10                = get_row(summ, "recall_at_10")
    h_r10c, n_r10c, p_r10c             = get_row(ctrl, "recall_at_10")
    h_sr10, n_sr10, p_sr10             = get_row(summ, "synopsis_recall_at_10")
    h_hed, n_hed, p_hed                = get_row(summ, "hedged")
    h_hedc, n_hedc, p_hedc             = get_row(ctrl, "hedged")
    h_faith, n_faith, p_faith          = get_row_float(summ)

    table3 = f"""**TABLE III.** RESULTS — HIERARCHY-AWARE SYNOPSIS SPAN EXPERIMENT, PYTHON TUTORIAL, N = {n_summ + n_ctrl} QUESTIONS, MISTRAL 7B READER + JUDGE.

| Metric | Subset (n) | N (naïve) | H (hierarchy-aware) | p (McNemar / Wilcoxon) |
|---|---|:-:|:-:|:-:|
| Recall@10 | Summarisation ({n_summ}) | {n_r10} | {h_r10} | {p_r10} |
| Recall@10 | Control ({n_ctrl}) | {n_r10c} | {h_r10c} | {p_r10c} |
| Synopsis-Recall@10 | Summarisation ({n_summ}) | {n_sr10} | {h_sr10} | {p_sr10} |
| Hedging Rate | Summarisation ({n_summ}) | {n_hed} | {h_hed} | {p_hed} |
| Hedging Rate | Control ({n_ctrl}) | {n_hedc} | {h_hedc} | {p_hedc} |
| Faithfulness (mean ± 95 % CI) | Summarisation ({n_summ}) | {n_faith} | {h_faith} | {p_faith} |
| Synopsis input size (mean chars) | — | {sg_N_str} | {sg_H_str} | n/a |
"""
    with open(args.table3, "w") as f:
        f.write(table3)
    print(f"wrote {args.table3}", file=sys.stderr)
    print("\n--- analysis.txt ---\n", file=sys.stderr)
    print("\n".join(out_lines), file=sys.stderr)


if __name__ == "__main__":
    main()
