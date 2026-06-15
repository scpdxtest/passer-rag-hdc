"""
Step 7 (multi-judge): read metrics from N judge runs and emit a paper-ready
comparison + Table III with one Faithfulness row per judge and a full
inter-judge correlation matrix.

Reads:
  --judge LABEL=PATH   one per judge, repeat the flag. PATH points at the
                       metrics CSV that 03_compute_metrics.py wrote for
                       that judge. LABEL is the column / row label that
                       appears in the analysis text and Table III.

  --questions          questions.csv (for subset bookkeeping)
  --chroma + --collections — for the smoking-gun row
  --out                analysis text output
  --table3             paste-ready Table III

Usage (3 judges):
    python3 07_compare_judges.py \\
        --judge "Mistral 7B"=metrics_mistral.csv \\
        --judge "Llama 3.1 70B"=metrics_llama70b.csv \\
        --judge "Gemma 31B"=metrics_gemma.csv \\
        --questions questions.csv \\
        --chroma http://92.247.133.89:63140 \\
        --collections python_tutorial python_tut_N \\
        --out analysis_multi.txt \\
        --table3 table3_multi.md

(Also works for 2 judges if you only pass --judge twice; the correlation
matrix collapses to a single off-diagonal entry.)
"""
import argparse
import csv
import math
import statistics
import sys
import requests


def mcnemar_exact(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p_one = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * p_one)


def wilson_ci(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_mean_ci(values, n_resamples=10000, seed=42):
    if not values:
        return (float("nan"), float("nan"), float("nan"))
    import random
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * n_resamples)]
    hi = means[int(0.975 * n_resamples)]
    return (sum(values) / n, lo, hi)


def wilcoxon_signed_rank(diffs):
    diffs = [d for d in diffs if d != 0]
    n = len(diffs)
    if n == 0:
        return 1.0
    abs_diffs = sorted([(abs(d), i, d) for i, d in enumerate(diffs)])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs_diffs[j + 1][0] == abs_diffs[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[abs_diffs[k][1]] = avg
        i = j + 1
    W_plus  = sum(r for r, d in zip(ranks, diffs) if d > 0)
    W_minus = sum(r for r, d in zip(ranks, diffs) if d < 0)
    W = min(W_plus, W_minus)
    if n >= 20:
        mu = n * (n + 1) / 4
        sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
        z = (W - mu) / sigma
        from math import erf, sqrt
        return 2 * (1 - 0.5 * (1 + erf(abs(z) / sqrt(2))))
    p = 0
    total = 2 ** n
    for mask in range(total):
        w_plus = sum(ranks[k] for k in range(n) if (mask >> k) & 1)
        if w_plus <= W or w_plus >= sum(ranks) - W:
            p += 1
    return min(1.0, p / total)


def pearson_r(x, y):
    if len(x) < 2 or len(x) != len(y):
        return float("nan")
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = math.sqrt(sum((a - mx) ** 2 for a in x))
    dy = math.sqrt(sum((b - my) ** 2 for b in y))
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


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


def faithfulness_pairs(metrics_rows):
    """{q_id: (H_faith, N_faith)} skipping blanks/non-numeric."""
    out = {}
    for m in metrics_rows:
        try:
            v = float(m.get("faithfulness", "") or "nan")
        except ValueError:
            continue
        if math.isnan(v):
            continue
        cur = out.get(m["q_id"], [None, None])
        if   m["config"] == "H": cur[0] = v
        elif m["config"] == "N": cur[1] = v
        out[m["q_id"]] = cur
    return {q: tuple(v) for q, v in out.items() if v[0] is not None and v[1] is not None}


def faithfulness_by_qcfg(metrics_rows):
    out = {}
    for m in metrics_rows:
        try:
            v = float(m.get("faithfulness", "") or "nan")
        except ValueError:
            v = float("nan")
        if math.isnan(v):
            continue
        out[(m["q_id"], m["config"])] = v
    return out


def parse_judge_arg(s):
    """LABEL=PATH"""
    if "=" not in s:
        raise argparse.ArgumentTypeError(
            f"expected LABEL=PATH, got {s!r}")
    label, path = s.split("=", 1)
    return (label.strip(), path.strip())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--judge", action="append", type=parse_judge_arg, required=True,
                    help="LABEL=PATH for one judge's metrics CSV; repeat for each judge.")
    ap.add_argument("--questions", required=True)
    ap.add_argument("--chroma", required=True)
    ap.add_argument("--collections", nargs="+",
                    default=["python_tutorial", "python_tut_N"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--table3", required=True)
    args = ap.parse_args()

    judges = []   # list of (label, rows)
    for label, path in args.judge:
        with open(path, newline="", encoding="utf-8") as f:
            judges.append((label, list(csv.DictReader(f))))
    if len(judges) < 2:
        print("warning: only 1 judge provided; no inter-judge stats will be emitted",
              file=sys.stderr)

    with open(args.questions, newline="", encoding="utf-8") as f:
        q_by_id = {r["q_id"]: r for r in csv.DictReader(f)}

    # Smoking-gun row
    print("fetching smoking-gun chunk metadata…", file=sys.stderr)
    sg = {}
    for cname in args.collections:
        cfg = "N" if cname.endswith("_N") or cname.endswith("-N") else "H"
        cid = get_collection_id(args.chroma, cname)
        chars = mean_synopsis_input_chars(args.chroma, cid)
        sg[cfg] = (sum(chars)/len(chars), statistics.median(chars),
                   min(chars), max(chars), len(chars)) if chars else None

    # Use the first judge's CSV for the non-faithfulness columns (recall@10,
    # hedging, etc) — those don't depend on the judge and are identical
    # across all metrics files by construction.
    primary_rows = judges[0][1]
    summ = [m for m in primary_rows if m["subset"] == "summarisation"]
    ctrl = [m for m in primary_rows if m["subset"] == "control"]
    n_summ = len(set(r["q_id"] for r in summ))
    n_ctrl = len(set(r["q_id"] for r in ctrl))

    lines = []
    def W(s=""): lines.append(s)

    W("=" * 80)
    W(f"§V.B — Multi-judge re-analysis ({len(judges)} judges)")
    for label, _ in judges:
        W(f"  · {label}")
    W("=" * 80)
    W()
    W("SMOKING-GUN ROW (mean synopsis_input_chars per config)")
    W("-" * 80)
    for cfg in ("H", "N"):
        if sg.get(cfg):
            mean, med, mn, mx, n = sg[cfg]
            W(f"  {cfg}: n={n:>3}  mean={mean:>9.1f}  median={med:>7.1f}  range=[{mn}..{mx}]")
        else:
            W(f"  {cfg}: (no synopsis chunks found)")
    W()

    # ---- Per-subset Faithfulness for each judge ----
    for label, rows in [("summarisation", summ), ("control", ctrl)]:
        ids = sorted(set(r["q_id"] for r in rows))
        W("=" * 80)
        W(f"SUBSET: {label}  (n={len(ids)} questions)")
        W("=" * 80)
        # Faithfulness per judge
        for jlabel, jrows in judges:
            jsubset = [r for r in jrows
                       if r["subset"] == label and r["q_id"] in set(ids)]
            pairs = faithfulness_pairs(jsubset)
            if not pairs:
                W(f"  Faithfulness [{jlabel:18s}]: (no pairs)")
                continue
            H = [h for h, n in pairs.values()]
            N = [n for h, n in pairs.values()]
            mH, lH, uH = bootstrap_mean_ci(H)
            mN, lN, uN = bootstrap_mean_ci(N)
            diffs = [h - n for h, n in pairs.values()]
            p = wilcoxon_signed_rank(diffs)
            W(f"  Faithfulness [{jlabel:18s}]: "
              f"H = {mH:.3f} [{lH:.3f}, {uH:.3f}]  "
              f"N = {mN:.3f} [{lN:.3f}, {uN:.3f}]  "
              f"Wilcoxon p={p:.4f}")
        # Inter-judge correlation matrix (this subset only)
        if len(judges) >= 2:
            byq = []   # per judge: {(q,cfg): v}
            for _, jrows in judges:
                byq.append(faithfulness_by_qcfg([r for r in jrows if r["subset"] == label]))
            common = set.intersection(*[set(d.keys()) for d in byq])
            common = sorted(common)
            if len(common) >= 2:
                W()
                W(f"  inter-judge correlation matrix (Pearson r, n={len(common)} per-(q,cfg) matches):")
                header = "    " + " " * 24 + "  ".join(f"{j:>22s}" for j, _ in judges)
                W(header)
                for i, (li, _) in enumerate(judges):
                    cells = []
                    for k, (_, _) in enumerate(judges):
                        if i == k:
                            cells.append("   —   ")
                        else:
                            x = [byq[i][c] for c in common]
                            y = [byq[k][c] for c in common]
                            r = pearson_r(x, y)
                            cells.append(f"{r:7.3f}")
                    W(f"    {li:24s}" + "  ".join(f"{c:>22s}" for c in cells))
        W()

    # ---- Non-judge binary metrics (same for all judges) ----
    W("=" * 80)
    W("Non-judge metrics (identical across judges)")
    W("=" * 80)
    for metric_col, label in [("recall_at_10", "Recall@10"),
                              ("synopsis_recall_at_10", "Synopsis-Recall@10"),
                              ("hedged", "Hedging Rate")]:
        for subset_label, rows in [("summarisation", summ), ("control", ctrl)]:
            pairs = {}
            for r in rows:
                try:
                    v = int(r.get(metric_col, ""))
                except (ValueError, TypeError):
                    continue
                cur = pairs.get(r["q_id"], [None, None])
                if   r["config"] == "H": cur[0] = v
                elif r["config"] == "N": cur[1] = v
                pairs[r["q_id"]] = cur
            pairs = {q: tuple(v) for q, v in pairs.items()
                     if v[0] is not None and v[1] is not None}
            if not pairs:
                continue
            n = len(pairs)
            k_H = sum(1 for h, _ in pairs.values() if h == 1)
            k_N = sum(1 for _, nv in pairs.values() if nv == 1)
            ci_H = wilson_ci(k_H, n)
            ci_N = wilson_ci(k_N, n)
            b = sum(1 for h, nv in pairs.values() if h == 1 and nv == 0)
            c = sum(1 for h, nv in pairs.values() if h == 0 and nv == 1)
            p = mcnemar_exact(b, c)
            W(f"  {label:22s} [{subset_label:13s}]: "
              f"H = {k_H}/{n} ({k_H/n*100:5.1f}%) [{ci_H[0]*100:5.1f}, {ci_H[1]*100:5.1f}]  "
              f"N = {k_N}/{n} ({k_N/n*100:5.1f}%) [{ci_N[0]*100:5.1f}, {ci_N[1]*100:5.1f}]  "
              f"McNemar p={p:.4f}")
    W()

    with open(args.out, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {args.out}", file=sys.stderr)

    # ---- table3_multi.md ----
    def fmt_pct(k, n, ci):
        if n == 0:
            return "—"
        return f"{k/n*100:.1f}% [{ci[0]*100:.1f}, {ci[1]*100:.1f}]"

    def fmt_float(m, lo, hi):
        return f"{m:.2f} [{lo:.2f}, {hi:.2f}]"

    def binary_row(rows, col):
        pairs = {}
        for r in rows:
            try:
                v = int(r.get(col, ""))
            except (ValueError, TypeError):
                continue
            cur = pairs.get(r["q_id"], [None, None])
            if   r["config"] == "H": cur[0] = v
            elif r["config"] == "N": cur[1] = v
            pairs[r["q_id"]] = cur
        pairs = {q: tuple(v) for q, v in pairs.items()
                 if v[0] is not None and v[1] is not None}
        if not pairs:
            return "—", "—", "—"
        n = len(pairs)
        k_H = sum(1 for h, _ in pairs.values() if h == 1)
        k_N = sum(1 for _, nv in pairs.values() if nv == 1)
        b = sum(1 for h, nv in pairs.values() if h == 1 and nv == 0)
        c = sum(1 for h, nv in pairs.values() if h == 0 and nv == 1)
        p = mcnemar_exact(b, c)
        return (fmt_pct(k_N, n, wilson_ci(k_N, n)),
                fmt_pct(k_H, n, wilson_ci(k_H, n)),
                f"{p:.3f}")

    def faith_row(judge_rows, subset_label):
        rows = [r for r in judge_rows if r["subset"] == subset_label]
        pairs = faithfulness_pairs(rows)
        if not pairs:
            return "—", "—", "—"
        H = [h for h, n in pairs.values()]
        N = [n for h, n in pairs.values()]
        mH, lH, uH = bootstrap_mean_ci(H)
        mN, lN, uN = bootstrap_mean_ci(N)
        diffs = [h - n for h, n in pairs.values()]
        p = wilcoxon_signed_rank(diffs)
        return fmt_float(mN, lN, uN), fmt_float(mH, lH, uH), f"{p:.3f}"

    r10s_N, r10s_H, r10s_p     = binary_row(summ, "recall_at_10")
    r10c_N, r10c_H, r10c_p     = binary_row(ctrl, "recall_at_10")
    sr10_N, sr10_H, sr10_p     = binary_row(summ, "synopsis_recall_at_10")
    heds_N, heds_H, heds_p     = binary_row(summ, "hedged")
    hedc_N, hedc_H, hedc_p     = binary_row(ctrl, "hedged")

    sg_H = sg.get("H"); sg_N = sg.get("N")
    sg_H_str = f"{sg_H[0]:.0f} (mean, n={sg_H[4]})" if sg_H else "—"
    sg_N_str = f"{sg_N[0]:.0f} (mean, n={sg_N[4]})" if sg_N else "—"

    judge_rows_md = []
    for jlabel, jrows in judges:
        jN, jH, jp = faith_row(jrows, "summarisation")
        judge_rows_md.append(
            f"| Faithfulness — {jlabel} (mean ± 95 % CI) | Summarisation ({n_summ}) | {jN} | {jH} | {jp} |"
        )

    # Inter-judge agreement note (Pearson r matrix, summarisation, summary)
    if len(judges) >= 2:
        byq = []
        for _, jrows in judges:
            byq.append(faithfulness_by_qcfg([r for r in jrows if r["subset"] == "summarisation"]))
        common = set.intersection(*[set(d.keys()) for d in byq])
        common = sorted(common)
        if len(common) >= 2:
            rs = []
            for i in range(len(judges)):
                for k in range(i + 1, len(judges)):
                    x = [byq[i][c] for c in common]
                    y = [byq[k][c] for c in common]
                    rs.append(f"{judges[i][0]} vs {judges[k][0]}: r={pearson_r(x, y):.2f}")
            inter_str = "; ".join(rs) + f" (n={len(common)})"
        else:
            inter_str = "—"
    else:
        inter_str = "n/a"

    table3 = f"""**TABLE III.** RESULTS — HIERARCHY-AWARE SYNOPSIS SPAN EXPERIMENT, PYTHON TUTORIAL, N = {n_summ + n_ctrl} QUESTIONS, MISTRAL 7B READER, {len(judges)} JUDGES SPANNING SIZE CLASSES.

| Metric | Subset (n) | N (naïve) | H (hierarchy-aware) | p (McNemar / Wilcoxon) |
|---|---|:-:|:-:|:-:|
| Recall@10 | Summarisation ({n_summ}) | {r10s_N} | {r10s_H} | {r10s_p} |
| Recall@10 | Control ({n_ctrl}) | {r10c_N} | {r10c_H} | {r10c_p} |
| Synopsis-Recall@10 | Summarisation ({n_summ}) | {sr10_N} | {sr10_H} | {sr10_p} |
| Hedging Rate | Summarisation ({n_summ}) | {heds_N} | {heds_H} | {heds_p} |
| Hedging Rate | Control ({n_ctrl}) | {hedc_N} | {hedc_H} | {hedc_p} |
""" + "\n".join(judge_rows_md) + f"""

Inter-judge agreement (Faithfulness, Summarisation subset, Pearson r): {inter_str}

Synopsis input size (mean chars), N = {sg_N_str}; H = {sg_H_str}.
"""
    with open(args.table3, "w") as f:
        f.write(table3)
    print(f"wrote {args.table3}", file=sys.stderr)
    print("\n--- analysis_multi.txt ---\n", file=sys.stderr)
    print("\n".join(lines), file=sys.stderr)


if __name__ == "__main__":
    main()
