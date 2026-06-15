# §V.B Synopsis-Span Ablation — Reproducible Runbook

This document captures the **as-run** procedure for the §V.B numerical
experiment, with every command, parameter, and expected result needed to
reproduce it on a fresh manual on a different topic. Distinct from
[README.md](./README.md), which documents the *planned* design only.

The procedure was first executed against the **Python Tutorial 3.7.0** on
2026-06-07, producing the Table III + on-chain Merkle anchor cited in
[paper_PaSSER_RAG_BdKCSE_ext.md](../../paper_PaSSER_RAG_BdKCSE_ext.md).
The actual artifacts from that run are preserved alongside this file —
treat them as reference values when calibrating a new run.

---

## 1. Scope

**What the experiment tests.** That `build_synopsis_records`' hierarchy-aware
re-spanning of chapter content (the §III fix) produces measurable retrieval
and answer-quality gains over the naïve span computation. Single isolated
variable: `synopsis_naive` ingestion option.

**What "good enough" looks like for a paper claim.**

- Smoking-gun row clearly demonstrates the variable is isolated
  (H ≫ N on `synopsis_input_chars`).
- All three judges agree on direction (H vs N) with consistent Wilcoxon p.
- Inter-judge Pearson r ≥ 0.5 on summarisation Faithfulness.
- On-chain Merkle root anchored; verification script returns `OK`.

**When to re-run.** A new manual on a different technical topic with a similar
structural pattern (chapters + numbered sub-sections). Examples that would
qualify: *The Rust Book*, *The Go Programming Language Specification*, an
ITU-T Recommendation, a vendor product manual, a textbook with `1.1 / 1.2`
nested headings. Examples that would NOT qualify: free-form narrative (use a
novel-profile experiment instead), purely procedural documents without
chapter structure.

---

## 2. Prerequisites

### Infrastructure

| Service | Where | How to verify |
|---|---|---|
| **gpuvm container** running on host s7b | ssh `195.230.127.226:58743` | `systemctl status gpuvm` from host |
| **Ollama** v0.24.x on V100 | `http://195.230.127.226:11850` | `curl -s http://195.230.127.226:11850/api/version` should return `{"version":"0.24.…"}` — **do NOT use v0.30+** (sm_70 incompatibility, see [[reference_gpuvm_log]]) |
| **Chroma** HTTP server | `http://92.247.133.89:63140` | `curl -s http://92.247.133.89:63140/api/v2/heartbeat` |
| **sraudit** smart contract | `blockchain2.uni-plovdiv.net:8033` | `curl -s http://blockchain2.uni-plovdiv.net:8033/v1/chain/get_account -d '{"account_name":"sraudit"}'` returns non-error |

### Models pulled on Ollama

```bash
curl -s http://195.230.127.226:11850/api/tags | python3 -m json.tool | grep -E '"name":|family'
```

Required tags:
| Model | Role | VRAM | Notes |
|---|---|---|---|
| `mxbai-embed-large` | Embeddings | ~1 GB | Used by Chroma ingestion |
| `mistral:latest` | Reader + judge 1 | ~5 GB Q4 | Mistral 7B |
| `llama3.1:70b` | Judge 2 | ~40 GB Q4 | Slow but viable on V100; ~30–40 min per judge pass |
| `gemma4:31b` | Judge 3 | ~20 GB Q4 | **Reasoning model** — needs special flags (see §6 below) |

### Python environment

Activate venv on the workstation that runs the scripts:
```bash
source ~/Downloads/testReact/.venv_ma_mac_arm64/bin/activate    # or your venv path
pip list | grep -E "^(requests|python-docx)"
```

Required: `requests ≥ 2.28`. Optional: `python-docx ≥ 1.2` (only needed to
regen the `.docx` paper). `pyntelope` may be present but broken (Pydantic
v1/v2 incompatibility) — chain submissions automatically fall through to the
HTTP path in `chain_bridge.py`.

### Antelope keys (chain anchoring)

Both keys are **hardcoded** in [chain_bridge.py](../../chain_bridge.py)
(lines ~160 and ~167). Do not put them in env vars. Confirm both exist:
```bash
grep -E "SIGNING_KEY|SRAUDIT" ../../chain_bridge.py | head -6
```

`sscore@active` signs sscore-targeted actions; `sraudit@active` signs
`logaudit`. The `--admin sscore` flag in the anchor script is a *data field*
on the audit row, not the chain authorizer.

---

## 3. Preparing a new corpus / topic

For each placeholder `<…>` below, decide a value before starting.

| Placeholder | Example (Python Tutorial run) | Notes |
|---|---|---|
| `<corpus-name>` | `python_tutorial` | Lowercase, no spaces, matches the H collection name |
| `<H-collection>` | `python_tutorial` | Hierarchy-aware (default ingest) |
| `<N-collection>` | `python_tut_N` | Naïve span (set `synopsis_naive:true`) |
| `<n-summ>` | 15 | Summarisation questions (one per chapter you want to cover) |
| `<n-control>` | 10 | Control / granular questions (deliberately *not* targeting full-chapter content) |
| `<chapter-range>` | 1–16 | Available chapters in the corpus |
| `<corpus-tag>` | `PaSSER-RAG/BdKCSE-ext/V.B` | Goes into every Merkle leaf and the on-chain row; bump on each re-run |
| `<milestone>` | `synopsis_span_v1` | On-chain row label; bump for re-runs (`_v2`, `_pyref`, `_rustbook`, …) |

**Questions guidance** (from labelling experience on the first run):

- Each summarisation question must have **exactly one** target chapter; phrase as *"Summarise Chapter N of …"* or *"What does Chapter N teach?"*.
- Include 2 non-sub-sectioned chapters as a within-experiment control (chapters whose naïve span will be larger than ~40 chars because there's no `\d+\.\d+` sub-section to truncate at). For Python Tutorial, that was Ch. 1 and Ch. 13.
- Control questions deliberately leave `gold_synopsis_id` blank; only body-chunk gold labels apply.
- Author the `reference_answer` field for *every* row — the LLM judges score the model's answer against this; missing references make Faithfulness undefined.

---

## 4. Step-by-step procedure

Working directory for every command:
```bash
cd /Users/boniradev/Downloads/testReact/passer/newRAG/experiments/synopsis_span_ablation
```

All scripts can run from this directory; they import from `../../chain_bridge.py` via relative path.

### Step 0 — Inventory check (~5 min)

```bash
# Models reachable + named correctly
curl -s http://195.230.127.226:11850/api/tags | python3 -m json.tool | grep -E '"name":' | head

# Chroma reachable + collections you expect
curl -s "http://92.247.133.89:63140/api/v2/tenants/default_tenant/databases/default_database/collections" \
    | python3 -m json.tool | grep -E '"name":' | sort

# Chain reachable + sraudit account known
curl -s http://blockchain2.uni-plovdiv.net:8033/v1/chain/get_info | python3 -m json.tool | head -5
curl -s -d '{"account_name":"sraudit"}' http://blockchain2.uni-plovdiv.net:8033/v1/chain/get_account | head -3
```

**Expected**: all four return non-error JSON. If any one fails, fix it before going further — no point ingesting a corpus you can't query.

### Step 1 — Ingest both collections (~30 min GPU)

Done via the existing `/start` endpoint of the ingestion Flask service (server-side; see [[gpuvm-setup-log]] for the standard ingestion procedure). For the experiment, you need TWO ingestions of the same PDF with one option flipped:

| Collection | Ingestion payload (key bits) |
|---|---|
| `<H-collection>` | normal options; `synopsis_naive` omitted |
| `<N-collection>` | add `"synopsis_naive": true` to the start payload |

Verify the two collections were built with the expected chunk-ID convention:
```bash
curl -s "http://92.247.133.89:63140/api/v2/tenants/default_tenant/databases/default_database/collections" \
    | python3 -m json.tool | grep -E '"<H-collection>"|"<N-collection>"'
```

**Sanity test** — confirm the smoking-gun ratio early, before sinking labelling time:
```bash
python3 -c "
import sys; sys.path.insert(0, '../../')
from chain_bridge import sha256_hex
import requests, statistics
def chars(coll):
    r = requests.get(f'http://92.247.133.89:63140/api/v2/tenants/default_tenant/databases/default_database/collections')
    cid = [c for c in r.json() if c['name']==coll][0]['id']
    r = requests.post(f'http://92.247.133.89:63140/api/v2/tenants/default_tenant/databases/default_database/collections/{cid}/get',
                      json={'limit':1000,'where':{'section':'synopsis'},'include':['metadatas']})
    vals=[m.get('synopsis_input_chars',0) for m in r.json()['metadatas']]
    return statistics.mean(vals), statistics.median(vals), len(vals)
for c in ['<H-collection>','<N-collection>']:
    print(c, chars(c))
"
```

**Expected**: H mean ≈ 5–15 k chars, N mean < 1 k chars on a sub-sectioned manual. If H ≈ N, the ingestions were not actually differentiated — check the start payload.

### Step 2 — Build questions.csv (1 h, manual)

Schema (10 columns):

| Column | Required | Example |
|---|---|---|
| `q_id` | yes | `1` |
| `subset` | yes | `summarisation` or `control` |
| `target_chapter` | summarisation only | `2` |
| `q_text` | yes | `Summarise Chapter 2 of the Python Tutorial.` |
| `gold_chunk_ids` | filled in Step 3 | comma-separated chunk IDs from labelling |
| `gold_synopsis_id` | summarisation only, Step 3 | comma-separated synopsis chunk IDs (H and N differ) |
| `reference_answer` | yes | 1–3 sentence ideal answer, used by judges |
| (`section`) | optional metadata | for your own bookkeeping |

Reference answers should be 1–3 short sentences. Verbose references make the strict judges (gemma4) score everything 0.0 because the model's answer can't enumerate every detail.

### Step 3 — Gold labelling (~1.5 h, manual via web UI)

Dump top-100 retrievals for both collections so you have an inspectable corpus:
```bash
python3 00_dump_top100.py \
    --collection <H-collection> --chroma http://92.247.133.89:63140 \
    --ollama http://195.230.127.226:11850 --embed-model mxbai-embed-large \
    --questions questions.csv --out top100_H.json

python3 00_dump_top100.py \
    --collection <N-collection> --chroma http://92.247.133.89:63140 \
    --ollama http://195.230.127.226:11850 --embed-model mxbai-embed-large \
    --questions questions.csv --out top100_N.json
```

Launch the labelling UI:
```bash
python3 06_label_ui.py \
    --questions questions.csv \
    --top100-h top100_H.json --top100-n top100_N.json \
    --port 8090
```

Then open `http://localhost:8090` in a browser. Tick chunks RELEVANT or chunks that satisfy the gold synopsis criterion. Auto-saves to `questions.csv` after each toggle.

**Labelling rules** (from the first run's lessons):

- Each summarisation question needs **at minimum** one gold synopsis chunk in each collection. H and N use different chunk IDs for the same chapter's synopsis — tick BOTH columns (the `gold_synopsis_id` field accepts comma-separated IDs).
- For body-chunk gold: tick the same content-chunk in BOTH columns. The H and N collections have shifted body-chunk numbering, so the same paragraph may show as `section-79-…` under H and `section-60-…` under N. The metric script intersects gold IDs with retrieved IDs, so missing the N twin will look like a recall loss when it isn't.
- Control questions deliberately have NO `gold_synopsis_id`. They evaluate body-chunk retrieval only.
- Some top-10 chunks will be irrelevant decoys (semantic neighbours, not actual answers). Top-10 is the *baseline retrieval*, not a list of correct answers — your job is to filter.

### Step 4 — Retrieval runs (~15 min)

```bash
python3 01_run_retrieval.py \
    --questions questions.csv \
    --collections <H-collection> <N-collection> \
    --chroma http://92.247.133.89:63140 \
    --ollama http://195.230.127.226:11850 \
    --embed-model mxbai-embed-large \
    --out retrieval.csv
```

**Output**: 50 rows in `retrieval.csv` (25 Q × 2 configs). Columns include `recall_at_10` and `synopsis_recall_at_10`. Verify:

```bash
wc -l retrieval.csv      # expect 51 (header + 50)
awk -F, 'NR>1 {print $3}' retrieval.csv | sort | uniq -c   # expect 25 H, 25 N
```

### Step 5 — Answer generation (~30 min)

```bash
python3 02_run_answers.py \
    --retrieval retrieval.csv --questions questions.csv \
    --chroma http://92.247.133.89:63140 \
    --ollama http://195.230.127.226:11850 \
    --llm mistral:latest --out answers.csv
```

**Reader is fixed at Mistral 7B** so the experiment isolates the synopsis variable, not the reader. Do not change without re-justifying §V.B's design.

Verify all 50 answers are non-empty:
```bash
awk -F, 'NR>1 && ($5=="" || $5~/^\[LLM ERROR/) {print "EMPTY/ERROR: "$1" "$2}' answers.csv
```

### Step 6 — Three-judge faithfulness scoring

Three independent passes, each writes its own metrics CSV. Total: ~1.5 h dominated by Llama.

#### 6a. Mistral 7B judge (~15 min)

```bash
time python3 03_compute_metrics.py \
    --questions questions.csv \
    --retrieval retrieval.csv \
    --answers answers.csv \
    --ollama http://195.230.127.226:11850 \
    --llm mistral:latest \
    --out metrics_mistral.csv
```

#### 6b. Llama 3.1 70B judge (~30–40 min)

```bash
time python3 03_compute_metrics.py \
    --questions questions.csv \
    --retrieval retrieval.csv \
    --answers answers.csv \
    --ollama http://195.230.127.226:11850 \
    --llm llama3.1:70b \
    --out metrics_llama.csv
```

**Expected behaviour**: each call takes ~30–60 s. Per-line progress shows `faith=0.XX`. **If you see** `! judge returned unparseable ''`, the `FINAL_SCORE:` prompt patch was lost — restore it from this checked-in version of `03_compute_metrics.py`.

#### 6c. Gemma 4 31B judge (~10 min)

```bash
time python3 03_compute_metrics.py \
    --questions questions.csv \
    --retrieval retrieval.csv \
    --answers answers.csv \
    --ollama http://195.230.127.226:11850 \
    --llm gemma4:31b \
    --out metrics_gemma.csv
```

**Expected behaviour**: each call takes ~6–12 s. **Gemma 4 is a reasoning model** — the script already includes `think: false` + `num_predict: 2048` to force visible output. **If you see** `done_reason=length, eval_count=200, response=''`, the reasoning-model fix has regressed. See §6 troubleshooting.

#### Quick sanity check on all three CSVs

```bash
for f in metrics_mistral.csv metrics_llama.csv metrics_gemma.csv; do
  echo "== $f"
  wc -l $f          # expect 51
  awk -F, 'NR>1 && $8=="" {print NR": blank faith"}' $f | head
done
```

### Step 7 — Three-judge comparison + statistics (~30 s)

```bash
python3 07_compare_judges.py \
    --judge "Mistral 7B"=metrics_mistral.csv \
    --judge "Llama 3.1 70B"=metrics_llama.csv \
    --judge "Gemma 4 31B"=metrics_gemma.csv \
    --questions questions.csv \
    --chroma http://92.247.133.89:63140 \
    --collections <H-collection> <N-collection> \
    --out analysis_triple.txt \
    --table3 table3_triple.md
```

**Outputs**:
- `analysis_triple.txt` — per-subset Faithfulness per judge with Wilson + bootstrap CIs, Wilcoxon p-values, full inter-judge Pearson r matrix.
- `table3_triple.md` — paste-ready replacement for Table III in the paper. Three Faithfulness rows + inter-judge agreement footer.

Sanity check (compare to Python Tutorial reference values):
| Metric | Reference (Python) | Acceptable range for a new run |
|---|---|---|
| `synopsis_input_chars` H/N ratio | 11.5× on means | ≥ 5× |
| Recall@10 summarisation H | 100 % | ≥ 90 % |
| Recall@10 summarisation N | 100 % | ≥ 90 % |
| Pearson r Mistral×Llama | 0.71 | ≥ 0.40 (any positive correlation supports the claim) |
| Pearson r Llama×Gemma | 0.90 | ≥ 0.50 |
| Wilcoxon p H vs N (any judge, summarisation) | 0.44–1.00 | > 0.05 unless your fix has a real effect at n=25 |

If Pearson r values are *negative*, judge prompts are mis-specified or answers are too short for a meaningful Faithfulness measurement. Stop and diagnose before anchoring.

### Step 8 — On-chain Merkle anchor

Dry run first (no chain write, manifest is computed and printed):
```bash
python3 08_anchor_results.py \
    --metrics "Mistral 7B"=metrics_mistral.csv \
    --metrics "Llama 3.1 70B"=metrics_llama.csv \
    --metrics "Gemma 4 31B"=metrics_gemma.csv \
    --project-id passer-rag \
    --milestone <milestone> \
    --experiment "<corpus-tag>" \
    --out audit_manifest.json
```

Note the printed `merkle_root` and `file_hash`. **They should be deterministic**: re-running the same dry-run with the same CSVs produces byte-identical values.

Submit to chain:
```bash
python3 08_anchor_results.py \
    --metrics "Mistral 7B"=metrics_mistral.csv \
    --metrics "Llama 3.1 70B"=metrics_llama.csv \
    --metrics "Gemma 4 31B"=metrics_gemma.csv \
    --project-id passer-rag \
    --milestone <milestone> \
    --experiment "<corpus-tag>" \
    --out audit_manifest.json \
    --submit --admin sscore
```

**Expected**: `✓ tx_id : …` within ~3 s. The script automatically uses `SRAUDIT_SIGNING_KEY` and signs as the `sraudit` actor (the action requires `require_auth(get_self())`; the `--admin` flag is a data field, not the chain signature).

### Step 9 — Verify (~5 s)

```bash
python3 09_verify_results.py \
    --manifest audit_manifest.json \
    --metrics "Mistral 7B"=metrics_mistral.csv \
    --metrics "Llama 3.1 70B"=metrics_llama.csv \
    --metrics "Gemma 4 31B"=metrics_gemma.csv
```

**Expected**: three `✓` lines and `VERIFICATION OK ✓`. The three checks are:
1. **CSV SHA-256 match** — every judge's CSV file hash matches what the manifest recorded.
2. **Merkle root re-derivation** — leaves rebuilt from CSVs hash to the same root the manifest recorded.
3. **On-chain row match** — the chain's `audits` table has a row with the same `(projectid, milestone)` whose `merkleroot` matches.

---

## 5. Acceptance criteria — when to call the run complete

All of the following must be true before treating the experiment as done:

- [ ] All 50 rows of each `metrics_<judge>.csv` have non-blank `faithfulness`.
- [ ] `wc -l` on each metrics CSV returns 51.
- [ ] `07_compare_judges.py` runs without errors and produces a `table3_*.md` with three Faithfulness rows.
- [ ] Inter-judge Pearson r ≥ 0.4 between every pair on the summarisation subset.
- [ ] Smoking-gun row shows H mean `synopsis_input_chars` at least 5× N's.
- [ ] `08_anchor_results.py --submit` returns a `tx_id`.
- [ ] `09_verify_results.py` prints `VERIFICATION OK ✓`.
- [ ] The on-chain `audits` table has a new row with the expected `(projectid, milestone)` — visible via `curl -s -d '{"json":true,"code":"sraudit","scope":"sraudit","table":"audits","limit":10}' http://blockchain2.uni-plovdiv.net:8033/v1/chain/get_table_rows | python3 -m json.tool`.

---

## 6. Troubleshooting reference

### 6.1 Gemma 4 returns empty `response`

**Symptom**: `q  1 H: ! judge returned unparseable '' (done_reason=length, eval_count=200, …)`.

**Cause**: gemma4 is a reasoning model. Its chain-of-thought consumes the `num_predict` budget; Ollama strips the hidden tokens from the `response` field, leaving an empty string.

**Fix already in `03_compute_metrics.py`**: `num_predict=2048` + top-level `think: false`. If you change the script, preserve both flags. The `_extract_response()` helper also reads `thinking` as a fallback in case `response` is empty.

### 6.2 Llama 3.1 70B "too slow"

**Symptom**: smoke test was 5 s, real judge pass crawls.

**Cause**: smoke test was 2 tokens output. Real judge calls produce 200+ tokens with 500–2 000 token inputs. V100 Q4 70B runs ~3–5 tok/s under this load → 30–60 s/call → ~30–40 min total.

**Mitigation**: this is expected. Don't kill the run. If you must use a faster judge, `qwen2.5:32b-instruct` is a defensible third-family alternative — but **do NOT use mixtral**: it's Mistral AI lineage, breaks the family-diversity claim.

### 6.3 Pyntelope import fails

**Symptom**: `submission failed: pyntelope not importable: constr() got an unexpected keyword argument 'regex'`.

**Cause**: pyntelope's Pydantic v1 syntax is broken on Pydantic v2.

**Fix**: nothing to do — `push_action()` in `chain_bridge.py` automatically falls through to the pure-HTTP signer path. If you see this error from `08_anchor_results.py`, you're on an *old* copy of `chain_bridge.py` that doesn't have the fall-through. Pull the current version.

### 6.4 `missing authority of sraudit`

**Symptom**: chain push returns `missing authority of sraudit`.

**Cause**: trying to sign sraudit-targeted actions with `sscore@active`'s key. The sraudit contract's `logaudit` action checks `require_auth(get_self())` — sraudit must sign itself.

**Fix**: `08_anchor_results.py` already passes `SRAUDIT_SIGNING_KEY` from `chain_bridge.py` (alongside the existing sscore `SIGNING_KEY`). If this error appears, confirm `SRAUDIT_SIGNING_KEY` is set in `chain_bridge.py` and the script imports it.

### 6.5 `chain query failed: 500 … get_table_rows`

**Symptom**: verification fails at Check 3 with a 500 on `/v1/chain/get_table_rows`.

**Cause**: wrong table name. Production sraudit names its audit table `audits`, not `auditlog` (the v1 contract).

**Fix**: `09_verify_results.py` already queries `"table": "audits"`. If this regresses, double-check.

### 6.6 Inter-judge Pearson r near zero or negative

**Symptom**: judges fundamentally disagree on which answers are good.

**Likely causes**:
- `reference_answer` field is too verbose, so judges score against unfair criteria.
- Reader produced near-identical "I don't know" answers across most questions, leaving little variance for judges to rank.
- `02_run_answers.py` failed silently — some `answer_text` cells are `[LLM ERROR…]`.

**Diagnose**:
```bash
awk -F, 'NR>1 && $5~/LLM ERROR/' answers.csv | wc -l   # should be 0
awk -F, 'NR>1 {print length($5)}' answers.csv | sort -n | head    # shortest answers
```

If sane, re-inspect 5 random reference answers and confirm they're 1–3 sentences. Long references inflate judge variance.

---

## 7. Time budget (measured, Python Tutorial first run, 2026-06-07)

| Step | Time | Bottleneck |
|---|---|---|
| 0. Inventory | 5 min | network |
| 1. Two ingestions | ~30 min | Ollama embed + synopsis LLM |
| 2. Question authoring | 1 h | manual |
| 3. Gold labelling | 1.5 h | manual UI |
| 4. Retrieval runs | ~15 min | Chroma + embeddings |
| 5. Answer generation | ~30 min | Mistral 7B |
| 6a. Mistral judge | ~15 min | mistral:latest |
| 6b. Llama judge | ~35 min | llama3.1:70b |
| 6c. Gemma judge | ~8 min | gemma4:31b |
| 7. Three-judge comparison | <1 min | local CPU |
| 8. On-chain submit | ~3 s | network |
| 9. Verify | ~2 s | network |
| **Total** | **~8 h focused work** | dominated by manual labelling + Llama judge |

For a re-run on a similar manual the only step that can shrink is (2) and (3) if you write a script to seed `reference_answer` from the chapter synopsis chunks themselves (then human-review). Everything else is roughly fixed per the corpus size.

---

## 8. What to bump for a new run

| Item | Why bump |
|---|---|
| `--milestone` | Each new run = a new on-chain row. Convention: `synopsis_span_v<N>_<corpus-shortname>`, e.g. `synopsis_span_v2_rustbook`. |
| `--experiment` | Updates the leaf canonical form so the new run's Merkle root is distinct from this one. |
| Collection names `<H-collection>` / `<N-collection>` | Avoid overwriting the Python Tutorial collections in Chroma. |
| `questions.csv` | Full re-author for the new corpus. |
| Reference answers | Author against the new corpus content. |
| `metrics_*.csv` filenames | Optional — keep the `mistral`/`llama`/`gemma` suffix convention so the comparison script's `--judge LABEL=PATH` mapping is unambiguous. |

Things that should NOT change without re-justifying the design:

- The reader stays Mistral 7B (isolates the synopsis variable from the reader).
- The judge panel stays Mistral 7B + Llama 3.1 70B + Gemma 4 31B (the size + family diversity is part of the paper claim — swapping introduces a new comparator-design discussion).
- The signing key for sraudit stays hardcoded in `chain_bridge.py`. Do not move to env vars; the project's standing decision is hardcoded keys.

---

## 9. Related artifacts (preserved alongside this runbook)

| Artifact | What it is |
|---|---|
| `questions.csv` | 25 Python-Tutorial questions, fully labelled. Use as a structural template. |
| `metrics_mistral.csv`, `metrics_llama.csv`, `metrics_gemma.csv` | Reference values from the first run. |
| `analysis_triple.txt`, `table3_triple.md` | Reference outputs from `07_compare_judges.py`. |
| `audit_manifest.json` | The anchored manifest. Root `c54ce09d…930c`, tx `7f0d368b…b2e950`, row id 1 on `sraudit`. |
| `analysis_mistral.txt`, `table3_mistral.md` | Single-judge baseline (preserved for backward-compatibility with the original `04_stats_table.py` flow). |
