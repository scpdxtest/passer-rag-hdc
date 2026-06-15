# §V.B Experiment — Hierarchy-Aware vs Naïve Synopsis Span (Python Tutorial)

The numerical study planned in `paper_PaSSER_RAG_BdKCSE_ext.md` §V.B. Tests whether the
hierarchy-aware re-spanning of chapter content in `build_synopsis_records` produces
measurable retrieval and answer-quality gains over the naïve span computation that
the original `detect_sections` would have supplied — on a 25-question Gold Standard
constructed from the Python Tutorial 3.7.0.

## Two conditions

| Config | Span computation | Expected `synopsis_input_chars` |
|---|---|---|
| **H** (hierarchy-aware) | `build_synopsis_records` re-spans each chapter through its sub-sections (the §III fix). | 3 000–14 000 chars on sub-sectioned chapters |
| **N** (naïve) | `opts["synopsis_naive"]=true` → use the original `spans[idx]` end (whatever `detect_sections` produced). | ~40 chars on chapters with `\d+\.\d+` sub-sections; **higher** (up to thousands) on chapters that have NO sub-sections, because the naïve span then continues to the next chapter. |

The N variance was observed empirically in the pilot ingestion: 3 sample synopsis
chunks ran at 4 424, 40, and 1 121 input chars under N. This is not a bug — it's
the actual `detect_sections` behaviour. It strengthens the experiment design rather
than weakening it: 2 of the 15 summarisation questions deliberately target
non-sub-sectioned chapters (Chapter 1, Chapter 13) as within-experiment controls,
where N≈H is the *expected* finding.

## Two collections (already ingested)

| Name | Mode | Notes |
|---|---|---|
| `python_tut_H` | hierarchy-aware | Default profile behaviour |
| `python_tut_N` | naïve | Ingested with `synopsis_naive:true` in the start payload |

Chunk IDs in `python_tut_N` carry a `-naive` suffix on synopsis chunks, so the two
collections cannot collide even if you accidentally ingest both into the same name.

## Files in this directory

| File | Stage | Type |
|---|---|---|
| `README.md` | — | playbook (this file) |
| `questions.csv` | Step 2 — manual review/edit | pre-drafted 25 Q's, gold columns blank |
| `00_dump_top100.py` | Step 3 — gold labelling | dumps top-100 retrievals per Q from a collection so you can label by reading |
| `01_run_retrieval.py` | Step 4 — top-10 retrieval | produces `retrieval.csv` |
| `02_run_answers.py` | Step 5 — RAG answers | produces `answers.csv` |
| `03_compute_metrics.py` | Step 6 — metrics + judge | produces `metrics.csv` |
| `04_stats_table.py` | Step 7 — stats + Table III | produces `analysis.txt` + `table3.md` |

## Inputs (you provide)

`questions.csv` columns:
- `q_id` — 1–25
- `subset` — `summarisation` (15) or `control` (10)
- `target_chapter` — for summarisation: chapter number being asked about (1–16). Blank for control.
- `q_text` — the question
- `gold_chunk_ids` — comma-separated chunk IDs (fill in Step 3)
- `gold_synopsis_id` — the specific `§synopsis` chunk for the asked chapter (summarisation only, fill in Step 3)
- `reference_answer` — author-drafted short answer for LLM-judge grounding (fill in Step 3)

## Run order

```bash
# Step 3: gold labelling helper. Run twice (once per collection) to inspect what's there.
python3 00_dump_top100.py \
    --collection python_tut_H --chroma http://92.247.133.89:63140 \
    --ollama http://195.230.127.226:11850 --embed-model mxbai-embed-large \
    --questions questions.csv --out top100_H.json
python3 00_dump_top100.py \
    --collection python_tut_N --chroma http://92.247.133.89:63140 \
    --ollama http://195.230.127.226:11850 --embed-model mxbai-embed-large \
    --questions questions.csv --out top100_N.json

# After manual labelling of questions.csv (gold_chunk_ids / gold_synopsis_id / reference_answer):

# Step 4: retrieval
python3 01_run_retrieval.py --questions questions.csv \
    --collections python_tut_H python_tut_N \
    --chroma http://92.247.133.89:63140 \
    --ollama http://195.230.127.226:11850 \
    --embed-model mxbai-embed-large \
    --out retrieval.csv

# Step 5: answers
python3 02_run_answers.py --retrieval retrieval.csv --questions questions.csv \
    --chroma http://92.247.133.89:63140 \
    --ollama http://195.230.127.226:11850 \
    --llm mistral:latest --out answers.csv

# Step 6: metrics (incl. LLM-judge faithfulness)
python3 03_compute_metrics.py --questions questions.csv \
    --retrieval retrieval.csv --answers answers.csv \
    --ollama http://195.230.127.226:11850 \
    --llm mistral:latest --out metrics.csv

# Step 7: stats + Table III
python3 04_stats_table.py --metrics metrics.csv --questions questions.csv \
    --chroma http://92.247.133.89:63140 \
    --collections python_tut_H python_tut_N \
    --out analysis.txt --table3 table3.md
```

## Falsifiable predictions

On the **summarisation** subset (excluding the 2 non-sub-sectioned controls):
- H produces a Synopsis-Recall@10 gain over N (the synopsis chunk is well-grounded and ranks high).
- H produces a Hedging Rate drop over N (the reader has substantive content to summarise from).

On the **granular control** subset:
- N and H are statistically indistinguishable (the body chunks dominate retrieval either way).

On the **non-sub-sectioned chapters** sub-subset (Q14 and Q15):
- H ≈ N is the *expected* finding (a within-experiment control demonstrating the §III fix is no-op on chapters that don't need it).

A null result on the first prediction refutes the §III claim. A *negative* result on the
second prediction (significant H > N on control queries) means the experiment isolates
something other than the synopsis pathway.

## Output schema

`metrics.csv`:

| field | description |
|---|---|
| `q_id` | 1–25 |
| `subset` | summarisation / control |
| `target_chapter` | chapter number (summarisation only) |
| `config` | H / N |
| `recall_at_10` | 0/1 indicator that any gold ID appears in top-10 |
| `synopsis_recall_at_10` | 0/1 indicator that the gold synopsis chunk appears in top-10 (NaN for control) |
| `hedged` | 0/1 from regex match on hedge phrases in the generated answer |
| `faithfulness` | Mistral-judge score in [0, 1] |

`analysis.txt` reports:
- Per-subset Recall@10, Synopsis-Recall@10, Hedging Rate, Faithfulness — both configs
- McNemar's exact test p-values (paired binary metrics) — Bonferroni-corrected
- Wilcoxon signed-rank p-values (Faithfulness)
- Wilson 95 % CIs on per-subset proportions
- Mean ± bootstrap-95%-CI on Faithfulness
- Smoking-gun row: mean `synopsis_input_chars` per config (computed from chunk metadata)
- Per-subset breakdown for the sub-sectioned vs non-sub-sectioned split within summarisation

`table3.md` is a paste-ready replacement for the placeholder Table III in
`paper_PaSSER_RAG_BdKCSE_ext.md`.

## Caveats baked into the design

- Single LLM-judge (Mistral 7B). The conference paper acknowledges this as a limitation;
  the journal-paper experiment will repeat with a 70B comparator judge.
- Questions are author-drafted; no second annotator for question quality. Acceptable for
  a 25-question pilot but worth noting.
- The pilot N ingestion showed synopsis_input_chars variance across chapters; Q14 and Q15
  are deliberate controls for this. The paper text will be updated to use precise
  language: *"the naïve span ends at the next any-name section match, which collapses to
  the bare heading on chapters with `\d+\.\d+` sub-sections"* rather than the original
  *"the naïve span is ~40 chars"*.
