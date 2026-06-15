# Development Log — Paper-Scoring RAG (newRAG/)

**Single-session record of everything built, every iteration, and every
decision made while bolting paper-scoring RAG onto the existing PaSSER
codebase. Chronological. References real files at their final state.**

Session date: **2026-05-21**.
Working tree: `/Users/boniradev/Downloads/testReact/passer`.
Touched files at the end of the session listed in §99 below.

---

## 0. Original goal

> The corpus of papers are in `./newRAG/corpus`. I have to score them
> against criteria using LLM + RAG, as a follow-up to the existing
> LLM-screening step. All new functionality goes in *new* components;
> the rest of the app must keep working.

So the constraints were:

- Don't touch any existing component file.
- Build new components that pick up the app's Ollama / ChromaDB /
  LLM settings automatically.
- Produce something that's the basis of a follow-up scientific paper.

---

## 1. Discovery — corpus & existing RAG components

### 1.1 Corpus inspection

Sampled 15 PDFs deeply + 50 stat-sampled out of 565 (806 MB):

| metric  | min | median | mean | max | p95 |
|---------|----:|------:|-----:|----:|----:|
| size MB | 0.1 | 0.7   | 1.1  | 5.7 | 3.9 |
| pages   | 2   | 9.5   | 15.8 | 112 | 49  |
| chars   | 400 | 33 K  | 38 K | 112 K | 90 K |

Observed properties:

- Filenames follow `NNN - <free-form>.pdf` for **562/563** → free,
  stable `paper_id`.
- ~95 % have a detectable **Abstract** in the first two pages.
- References sections occupy the last 8–20 % of each document — heavy
  noise source for similarity search in a topically homogeneous corpus.
- All papers cluster topically (blockchain e-voting) → flat corpus-wide
  similarity search routinely confuses papers.
- ~3–5 % use CID-encoded fonts (`pypdf` fails; needs `pdfplumber`
  fallback).

### 1.2 Existing components audit

| File | Behaviour | Implication |
|---|---|---|
| `src/component/dbFromPDF.js` | Browser-side `pdfjs` extract, `RecursiveCharacterTextSplitter(1024/50)`, `Chroma.fromTexts`, metadata = `{ source: fileName }`. | No per-paper isolation, no section tags — unfit for per-paper scoring. |
| `src/component/testRAGbat.js` | Memoized `OllamaEmbeddings({model: selectedEmbeddingModel, …})`, fallback list `[mxbai-embed-large, nomic-embed-text, …]`, two retriever modes. | Pattern to reuse for the new components' embedding wiring. |
| `src/component/chatFromDB.js` | Same memoization + retriever style as above. | Same. |
| `src/component/SelectModel.js` | Writes `selectedOllama`, `selectedChromaDB`, `selectedLLMModel`, `chatTempreture`, retriever settings to `localStorage`. | All new components must read from these keys. |
| `src/component/BCEndpoints.js`, `TestWharf.js`, `backEnd.py` (lines 446-488) | Existing Antelope integration pattern. | Footprint for the future blockchain audit-trail design (§7). |

**Conclusion:** the old `dbFromPDF.js` flow is unfit for paper-scoring
because (a) `Chroma.fromTexts` mixes chunks from all papers, (b)
metadata is only `{ source }`, (c) page text is mashed, (d)
browser-side parsing won't survive 800 MB / 565 PDFs, (e) no content
hash → re-ingestion duplicates.

---

## 2. Design proposal & user follow-ups

Proposed:

- One Chroma collection, **per-paper isolation via metadata filter** at
  query time.
- Per-chunk metadata: `paper_id`, `filename`, `title`, `doi`,
  `arxiv_id`, `section`, `page_from`, `page_to`, `chunk_index`,
  `total_chunks`, `pages_total`, `content_hash`, `ingest_run`.
- **Two-tier chunking:** one *card* chunk per paper (`Title + Abstract
  + Keywords`) plus *body* chunks split per-section (800/120),
  references retained but tagged.
- **Python worker** (Flask + CLI) for ingestion; React components are
  thin UI on top.
- React: `dbFromCorpusPapers.js` for ingestion, `scorePapersBat.js`
  for scoring.

User answered four follow-up questions:

| question | decision |
|---|---|
| Where ingestion runs | Python worker (Flask + CLI). |
| Collection layout | Single collection + per-paper metadata filter. |
| References handling | Keep and tag `section: "references"` (queryable but excludable). |
| Scope | Scaffold both components at once; preserve all existing functionality. |

Plus a clarification asked separately: **the worker must use the
existing app's Ollama path and Ollama model setting** as the embedding
worker (matching `dbFromPDF.js`'s old behaviour).

---

## 3. First implementation pass

Three new files + three minimal additive edits.

### 3.1 Worker

**[`newRAG/ingest_corpus.py`](./ingest_corpus.py)** — Flask server on
default port `8010`, also runnable as `--cli`.

Pipeline stages implemented:

1. PDF text extraction (`pdfplumber` → `pypdf` fallback).
2. Header/footer stripping via the "≥ 40 % of pages contain this short
   line" rule.
3. De-hyphenation and whitespace normalisation.
4. Section detection — nine regexes (`abstract`, `keywords`,
   `intro`/`background`, `related`, `method`/`implementation`/`design`,
   `results`/`evaluation`/`experiments`, `discussion`, `conclusion`,
   `references`, `appendix`).
5. Card chunk: `Title + DOI + arXiv + abstract[:3000] + keywords[:600]`.
6. Body chunks: per-section split, 800-char window, 120-char overlap,
   never crossing section boundaries.
7. Embedding via Ollama `/api/embeddings`.
8. Idempotent upsert into Chroma (chunk IDs include the content hash).
9. Manifest JSON written after every 5 files.

Endpoints: `/health`, `/start`, `/status`, `/stop`, `/papers`.

### 3.2 React components

**[`src/component/dbFromCorpusPapers.js`](../src/component/dbFromCorpusPapers.js)**
+ `.css` — ingestion UI. Reads `selectedOllama`, `selectedChromaDB`,
`selectedLLMModel` from `localStorage`. Form for collection name,
chunk size, overlap, optional limit. Polls `/status` every 1.5 s while
running.

**[`src/component/scorePapersBat.js`](../src/component/scorePapersBat.js)**
+ `.css` — scoring UI. Lists papers in a collection via `/papers`,
editable criteria JSON (textarea + file upload), per-paper-filtered
similarity search (`filter: { paper_id, section: { $nin: ["references","card"] } }`),
LLM scoring with `format: "json"`. Card-gate optional.

### 3.3 Wiring

- **[`src/App.js`](../src/App.js)** — two added imports + two `<Route>`s
  (`/dbfromcorpuspapers`, `/scorepapersbat`).
- **[`src/component/Nav.js`](../src/component/Nav.js)** — "From Paper
  Corpus" added under *Create Vectorstore*, "Paper Scoring" added as a
  top-level menu item.
- **[`src/component/configuration.json`](../src/component/configuration.json)**
  — added `"IngestAPI": "http://127.0.0.1:8010"`.

**No existing component file was modified.**

### 3.4 First-pass settings flow

Initially I hardcoded `mxbai-embed-large` as the default embedding model
in both new components. The user pointed out the old `dbFromPDF.js`
used `selectedLLMModel` *as* the embedding model (so the Ollama URL +
LLM model dropdown in `SelectModel` drive the embedder). **Fixed:**
`embedModel` state in both new components now initialises from
`localStorage.selectedLLMModel`, with `localStorage.papersEmbedModel`
as an optional per-page override; the worker's CLI/Flask default
`embed_model` changed from `mxbai-embed-large` to `mistral`.

---

## 4. Documentation pass #1 — `EXPLANATION.md`

**[`newRAG/EXPLANATION.md`](./EXPLANATION.md)** — user-facing how-to,
12 sections (originally 9, grew to 12 over the session). Initial
contents:

- §1 original request, §2 existing-app audit, §3 corpus findings,
  §4 why old flow is unfit, §5 proposed design with metadata schema,
  §6 explicit Ollama-settings table, §7 file list, §8 run instructions,
  §9 trade-offs.

---

## 5. Embedding-model recommendation discussion

User asked: *"Should I use `mxbai-embed-large` instead of a large
model like `gemma:12b`? Later I'll score with different LLMs — do I
need separate vector stores?"*

Recommendation given:

- Yes, use a dedicated embedder (`mxbai-embed-large`). Chat LLMs as
  embedders are slower (forward pass through a 12 B model per chunk)
  and lower-quality (no contrastive training for similarity).
- The scoring LLM is **decoupled** from the embedding model. One
  Chroma collection serves any number of judge LLMs without
  re-ingestion.
- Separate vector stores are needed **only** when the *embedding*
  model changes.

Bake the embed-model name into the collection name
(`papers_corpus__mxbai`) so mismatch at query time is obvious.

Then I added a guard at the worker + UI level:

### 5.1 Collection-metadata embed-model tag

- Worker `get_collection_for_embed` (and later `ChromaHTTP.create_collection`)
  stores `{"hnsw:space": "cosine", "embed_model": <model>}` on the
  collection at creation.
- New endpoint `/collection_info` reports the tag.
- Both UIs probe `/collection_info` on collection/URL change and
  render a green "match" banner or a red "mismatch" banner with a
  concrete suggestion (`papers_corpus__embedname`).
- `Start Ingestion` and `Run scoring` show a confirm dialog if the
  user clicks through anyway; the worker refuses mismatched ingestion
  server-side regardless.

User confirmed the guard with: *"yes, do that"*.

Then added — in `EXPLANATION.md` — a section *"Scoring LLMs
compatible with `mxbai-embed-large` vector store"* with a table of
Mistral, Llama 3 / 4, Gemma 2 / 3, Granite 3 / 4, Qwen 2.5 / 3, Phi 3 /
4, DeepSeek-R1, Command-R, and the conceptual point that the scoring
LLM is decoupled.

---

## 6. First-run failures and the fixes that followed

User ran the worker and immediately hit problems. Each was diagnosed
and fixed in-session.

### 6.1 ChromaDB `KeyError('_type')` — server-side schema mismatch

User pasted a traceback from the Chroma server:

```
chromadb/server/fastapi/__init__.py:798 in create_collection
chromadb/server/fastapi/__init__.py:772 in process_create_collection
    else CollectionConfigurationInternal.from_json(create.configuration)
KeyError: '_type'
```

The chromadb Python client (any version) sends a `configuration` JSON
whose `_type` key the server can't decode when the JS chromadb (1.10.4,
used elsewhere in the app) and Python chromadb versions disagree. The
same trap fires on **GET-by-name** for collections previously created
in the broken state, because the server tries to deserialise the
stored configuration.

**Fix:** removed the chromadb Python client entirely. Wrote a tiny
`ChromaHTTP` class that talks to the v2 HTTP API directly and **never
sends a `configuration` field**, so the server falls back to its
built-in default (the `CollectionConfigurationInternal()` branch of
`process_create_collection`).

Endpoints added:

- `GET /collection_info` — reads metadata via `list_collections`
  (which doesn't deserialise the config and therefore stays clear of
  the bug).
- `POST /delete_collection` — recovery for already-broken collections.
- UI **"Drop collection"** button in `dbFromCorpusPapers.js` (with
  confirm).

The user dropped the broken collection, re-ran, and ingestion worked.

### 6.2 ChromaDB / Ollama URL fallback from `configuration.json`

User asked: *"did you get chromadb path from configuration file?"*

It wasn't being read. Fixed: both components now resolve URLs in this
order:

```
localStorage.selectedChromaDB              (set by SelectModel)
  → configuration.passer.Chroma[0].url     (configuration.json)
  → 'http://127.0.0.1:8000'                (literal fallback)
```

Same chain for Ollama. When more than one entry exists in
`configuration.json`, the input becomes a `Dropdown` like the one in
`SelectModel.js`.

### 6.3 Directory browser

User asked for a way to pick the corpus directory, not type the path.

Added:

- `GET /list_dir?path=...` on the worker — returns `{ path, parent,
  subdirs, pdf_count, cwd }`. Server-side directory listing.
- **"Browse…"** button next to the corpus-dir input in
  `dbFromCorpusPapers.js`. Opens a PrimeReact `Dialog` showing
  current path, PDF-count badge, **Parent** / **Worker cwd** /
  **Reload** buttons, clickable subdir list. "Use this directory"
  enabled only when PDFs are present.

### 6.4 Per-chunk embedding failures aborting the whole paper

User reported one paper (`010 - 2025109210.pdf`) marked
`embed_error` with `chunks: 0`. The PDF extracts fine — both pypdf
and pdfplumber recover ~32 K chars cleanly. The failure was a single
chunk choking Ollama's embeddings endpoint, and the old code aborted
the whole paper on the first failing chunk.

**Fixes (one pass):**

- New `_sanitize_for_embedding` strips null bytes, lone surrogates,
  and other non-newline non-tab control characters.
- `embed_text` now retries up to 3 times with exponential backoff
  on timeouts, connection resets, and non-context 5xx responses.
- `process_paper` no longer bails on first failure. Each failing chunk
  is counted in `failed_chunks` and its message recorded (up to 3
  errors per paper). New status semantics:
  - `ok` — every chunk embedded
  - `partial` — some chunks embedded, some failed (was previously
    masked)
  - `embed_error` — *zero* chunks succeeded
- UI Recent-files table grew a **Detail** column showing the first
  100 chars of the error (full string on hover), and the **Chunks**
  column shows `N (✗K)` in red when `failed_chunks > 0`.

### 6.5 Card chunk too long for the embedding model

After 6.4, three papers were reported `partial` with:

> `chunk 0 (card): ollama 500: {"error":"the input length exceeds the context length"}` — `i ran ollama with 10k context window`

This is the embedding model's **max-sequence-length**, not Ollama's
`num_ctx`. `mxbai-embed-large`, `bge-large`, `snowflake-arctic-embed`,
`all-minilm` all cap at 512 tokens *by architecture*, independent of
the `num_ctx` knob (which only affects chat LLMs). The card I built
was `Title + DOI + arXiv + 3000 chars abstract + 600 chars keywords`
≈ 3700 chars ≈ 900 tokens → over the limit.

**Two fixes:**

1. Card budget tightened to **~1500 chars total** in `process_paper`,
   with title/DOI/arXiv first and the remainder split 75 % abstract
   / 25 % keywords. ~1500 chars of English ≈ 380 tokens, safe for
   every common embedder.
2. `embed_text` now auto-truncates on context overflow. New helper
   `_looks_like_context_overflow` matches `"context length"`,
   `"input length"`, `"exceeds"`, `"too long"`, `"max_seq_length"`,
   `"maximum sequence"`. On detection: halve the input, retry. Up to
   three halvings (down to ~250 chars). Doesn't consume regular retry
   attempts. Transient retry behaviour preserved.

User re-ran and all 10 papers completed.

### 6.6 No visible "done" state at end of a run

User reported: *"all 10 done. but at the end progress bar didn't stop,
and there is no indication that process is finished"*. Polling was
working correctly; the issue was *visual*. Progress bar at 100 % looks
identical to progress bar in progress.

**Added (in `dbFromCorpusPapers.js`):**

- `prevRunningRef` to detect the `running: true → false` transition
  across polls.
- `finishSummary` state, populated on the transition with
  `{ done, total, errors, duration, error, finishedAt }`. Cleared at
  the next start.
- Green completion card "✅ Ingestion finished at HH:MM:SS" with a
  3-column grid (Papers processed / Errors / Duration) and **Dismiss**
  + **List indexed papers** buttons.
- Progress card itself flips green at `!running && pct === 100`
  (`progressDone` class, green left border, `✓ Done` `Tag` next to the
  heading, bar fill colour `#22863a`).
- **Voice cue** via `SpeechSynthesisUtterance` — same `speak()`
  pattern used by `dbFromPDF.js`.

---

## 7. Documentation pass #2 — criteria JSON how-to

User said: *"add detailed explanation and instructions how criteria
json is composed with current example"*.

Added **`EXPLANATION.md` §10 "Criteria JSON — format, examples, and
tips"** with nine subsections:

- 10.1 Schema (table of `id`, `criterion`, `weight`, `scale`).
- 10.2 The literal LLM prompt template (so the user sees what their
  criterion text turns into).
- 10.3 The default 4-criterion example as shipped.
- 10.4 Aggregation formula
  $\bar s_P = \frac{\sum_c w_c s_{P,c}}{\sum_c w_c}$ (skip nulls)
  with notes on `weight: 0`.
- 10.5 Five-rule checklist for writing good criteria.
- 10.6 Four ready-to-use templates: custom anchored scale,
  boolean 0/1 gating, domain-tilted blockchain-voting systematic
  review, diagnostic-only.
- 10.7 Loading & editing (inline textarea vs file picker),
  suggested folder layout.
- 10.8 CSV export columns (later updated in §8).
- 10.9 Card-gate interaction.

---

## 8. LLM identity in CSV exports

User said: *"add llm name when export scores (somewhere in name or
inside csv)"*.

Implemented:

- `runScoring` captures `runLlm`, `runEmbed`, `runStartedAt` once at
  the start so every row in a batch carries consistent labels even
  if the user edits the inputs mid-run.
- Each row in `scorePapersBat.js` state now carries `llm_model`,
  `embed_model`, `collection`, `run_started_at`.
- `exportCSV` adds these four as columns and rewrites the filename:
  ```
  scores_<collection>_<llm_model>_<YYYY-MM-DD-HHMM>.csv
  ```
  with non-alphanumerics replaced by `_`. LLM name in the filename is
  taken from the *last* row (matches the data, not the inputs).

`EXPLANATION.md` §10.8 updated to document the new schema, the
filename format, and a pandas snippet for pivoting concatenated CSVs by
`llm_model`.

---

## 9. Documentation pass #3 — `PAPER_METHODOLOGY.md`

User said: *"as this functionality will be part of scientific paper
(the next after llm scoring), create document with detailed
explanations of algorithm, realisation and how to collect and
interpret the results. the experiments will include several different
llms over previously selected papers."*

Wrote **[`newRAG/PAPER_METHODOLOGY.md`](./PAPER_METHODOLOGY.md)** —
666 lines, Markdown with KaTeX-style math, fenced pseudocode.
13 top-level sections:

1. Abstract.
2. Motivation (3 distinguishing properties of this task).
3. Related work (RAG, LLM-as-judge, automated SR tooling).
4. Algorithm:
   - 3.1 Pipeline diagram.
   - 3.2 Corpus ingestion: text extraction + header rule (formal
     definition with $c(\ell) \geq \max(2, \lfloor 0.4 n \rfloor)$),
     structural parsing, two-tier chunking with $B_{\text{card}}$,
     chunk-ID formula, metadata Table 1, embed-model tag guard.
   - 3.3 Per-paper scoring: Algorithm 1 pseudocode (card-gate →
     filtered retrieval → LLM-as-judge → aggregation formula).
5. Implementation:
   - 4.1 Architecture diagram.
   - 4.2 Direct-HTTP ChromaDB rationale.
   - 4.3 Embedding robustness (§6.4–6.5 in this log).
   - 4.4 Idempotency for cheap re-runs.
6. Experimental protocol:
   - Held-fixed vs. varied variables.
   - Pre-registration.
   - $\tau = 0.1$ + JSON format.
   - Minimum-runs formula $J \cdot 2 \cdot N \cdot M$.
7. Results collection (CSV schema, manifest, sanity checks).
8. Interpretation methodology:
   - Descriptive stats.
   - Inter-judge agreement: Spearman $\rho$, weighted Cohen's $\kappa$,
     Krippendorff's $\alpha$, ICC(2,1).
   - Consensus rankings: Borda count + mean-of-means.
   - Calibration via leave-one-out consensus correlation.
   - Faithfulness audit via LCS evidence-vs-chunks.
   - Statistical tests with Bonferroni / LOJO Kendall $\tau_b$.
9. Six threats to validity specific to this setup.
10. Reproducibility checklist (7 points).
11. Implementation references (file:line pointers).
12. Notation summary.
13. References (placeholders).

---

## 10. Documentation pass #4 — `BLOCKCHAIN_INTEGRATION_PLAN.md`

User said: *"as we have our antelope (now Spring) blockchain, and want
to use it for traceability and transparency of the test results, can
you suggest how to use it for current tasks? see how we are using it
in other parts of the app. see also Crypto-Primitives-Upgrade-Plan.md
for any idea how to implement newly installed and activated crypto bls
primitives."*

Read existing chain integration:

- `src/component/BCEndpoints.js` (chain-URL fallback chain).
- `src/component/TestWharf.js` (Anchor login → `wharf_user_name`).
- `backEnd.py` lines 446-488 (`pyntelope` push template, `llmtest`
  contract, hot-key signing).
- `Screening.js`, `LLMScreening.js`, `AdminDashboard.js`,
  `UserActionLog.js` — `MementoAPI` decoded-transaction viewer.
- `Crypto-Primitives-Upgrade-Plan.md` — Tracks 1-5 (k1_recover, sha3
  Merkle, Pedersen commit-reveal, VRF, BLS aggregate).

Produced
**[`newRAG/BLOCKCHAIN_INTEGRATION_PLAN.md`](./BLOCKCHAIN_INTEGRATION_PLAN.md)**:

- §1 Five things we want from the chain.
- §2 Recap of existing chain plumbing to reuse.
- §3 Proposed `sscore` contract: three tables (`run_row`, `cell_row`,
  `judge_row`), eight actions, canonical payload schema.
- §4 Client-side flow: where to hook `scorePapersBat.js` /
  `dbFromCorpusPapers.js`, Python worker side (new `chain_bridge.py`
  blueprint mounted into the Flask worker), CSV-export extension with
  4 new columns.
- §5 Per-track mapping of Crypto-Primitives-Upgrade-Plan.md primitives
  to scoring use cases (Tracks 1, 2, 3, 4, 5 each get their own
  subsection with mechanism and paper-section impact).
- §6 Adoption sequence + effort estimate per track.
- §7 Paper-claim table (without/with v0.1/with Phase-D).
- §8 Open questions (later replaced with settled decisions, see §11).
- §9 Cross-references.

---

## 11. Settled blockchain decisions (this conversation)

User's answers to the five open questions in
`BLOCKCHAIN_INTEGRATION_PLAN.md` §8 are now reflected in the document
itself; recorded here for traceability:

| # | question | decision |
|---|---|---|
| 1 | Contract name | **`sscore`** — separate from `sraudit`. |
| 2 | IPFS pinning | **No IPFS at all.** Full payload (justification + evidence) goes into **MongoDB**, matching the `backEnd.py` pattern. `cell_row` carries `mongo_oid` instead of `ipfs_cid`. |
| 3 | `criterion_id` namespace | Keep as-is. Default ids fit Antelope's 12-char `[a-z1-5.]` `name` constraint; load-time lint in `scorePapersBat.js` flags any future id that doesn't. |
| 4 | Streaming vs. batching | **Stream** `logcell` per row. Real-time tx-ids in the UI table, matches `LLMScreening.js` UX. |
| 5 | Key custody | **Hybrid.** Anchor wallet signs `startrun` and `sealrun`; worker hot key signs each `logcell` in between. Contract enforces `require_auth(analyst)` on boundaries and `require_auth(worker)` on cells, plus a `run_id` foreign-key check. |

§3.1 (`cell_row` table), §3.2 (`logcell` action signature), §3.3
(canonical-payload note), §4.2 (`logcell` Python handler), §4.3 (CSV
schema) of `BLOCKCHAIN_INTEGRATION_PLAN.md` were updated to remove
`ipfs_cid` and add `mongo_oid`. §8 was rewritten from "Open questions"
to "Settled design decisions".

---

## 12. Final state

### 12.1 Files created in `newRAG/`

| file | purpose | lines |
|---|---|---|
| [`ingest_corpus.py`](./ingest_corpus.py) | Flask + CLI worker: PDF → Chroma | ~700 |
| [`EXPLANATION.md`](./EXPLANATION.md) | User-facing how-to + criteria JSON guide | ~570 |
| [`PAPER_METHODOLOGY.md`](./PAPER_METHODOLOGY.md) | Publication-grade methodology | 666 |
| [`BLOCKCHAIN_INTEGRATION_PLAN.md`](./BLOCKCHAIN_INTEGRATION_PLAN.md) | `sscore` contract design | ~610 |
| [`CHANGELOG.md`](./CHANGELOG.md) | This file | — |
| `manifest_<collection>.json` | Auto-generated per ingestion run | varies |

### 12.2 Files created in `src/component/`

| file | purpose |
|---|---|
| [`dbFromCorpusPapers.js`](../src/component/dbFromCorpusPapers.js) | Ingestion UI |
| `dbFromCorpusPapers.css` | Ingestion UI styling |
| [`scorePapersBat.js`](../src/component/scorePapersBat.js) | Scoring UI |
| `scorePapersBat.css` | Scoring UI styling |

### 12.3 Existing files edited (minimal, additive)

| file | change |
|---|---|
| [`src/App.js`](../src/App.js) | 2 imports + 2 `<Route>`s |
| [`src/component/Nav.js`](../src/component/Nav.js) | 2 menu entries |
| [`src/component/configuration.json`](../src/component/configuration.json) | `IngestAPI: "http://127.0.0.1:8010"` |

### 12.4 Files NOT touched

**Every other component file in the app**, including `dbFromPDF.js`,
`testRAGbat.js`, `chatFromDB.js`, `Screening.js`, `LLMScreening.js`,
`backEnd.py`, `BCEndpoints.js`, `TestWharf.js`, … — preserved as the
brief required.

### 12.5 Routes added

- `/dbfromcorpuspapers` → `DBFromCorpusPapers`
- `/scorepapersbat`     → `ScorePapersBat`

### 12.6 Worker endpoints

- `GET  /health`
- `POST /start`           — body: `{ corpus_dir, chroma_url, ollama_url, collection, embed_model, chunk_size, overlap, limit? }`
- `GET  /status`
- `POST /stop`
- `GET  /papers?chroma=…&collection=…`
- `GET  /collection_info?chroma=…&collection=…`
- `POST /delete_collection`
- `GET  /list_dir?path=…`

### 12.7 LocalStorage keys read (existing keys, not new)

`selectedOllama`, `selectedChromaDB`, `selectedLLMModel`,
`chatTempreture`, `wharf_user_name`, `ingestAPI` (new),
`papersEmbedModel` (new), `papersCollection` (new), `corpusDir` (new).

---

## 13. Blockchain v0.1 implementation (this session, after the plan)

User said: *"fine. start implementing your plan."* — followed by the
five decisions recorded in §11. Implemented the Phase-D-independent
track from `BLOCKCHAIN_INTEGRATION_PLAN.md` §6.

### 13.1 New: smart contract `sscore` v0.1

**[`blockchain/sscore.cpp`](../blockchain/sscore.cpp)** — three tables
(`run_row`, `cell_row`, `import_row`) and six actions
(`startrun`, `logcell`, `sealrun`, `verifycell`, `logimport`,
`clearrun`). Notes:

- All write auth in v0.1 is `require_auth(get_self())` — the worker hot
  key under `sscore@active`. Track 1 (k1_recover analyst signatures) is
  scoped for v0.2 and the contract comment marks the exact line that
  changes.
- `verifycell` does the on-chain Merkle inclusion proof using
  `sha256` (Antelope's native checksum256 hash function). Path bits
  follow the convention `0 = current on the left`, `1 = current on
  the right`.
- `logimport` accepts a `prev_hash` from the client (the worker
  computes it from the previous import for the same collection,
  fetched from MongoDB) — gives a tamper-evident sequence per
  collection without an on-chain scan.
- `clearrun` is a dev-only admin escape hatch.

**[`blockchain/README.md`](../blockchain/README.md)** — compile / deploy /
smoke-test instructions (`cdt-cpp -abigen -o sscore.wasm sscore.cpp`;
`cleos create account`; `cleos set contract`; a 4-step cleos
end-to-end exercise of `startrun → logcell → sealrun → get table`).
Also documents the `name_safe()` digit-folding (§13.4 below).

### 13.2 New: Python chain bridge

**[`newRAG/chain_bridge.py`](./chain_bridge.py)** — Flask blueprint
mounted into the ingestion worker. Endpoints:

| route | purpose |
|---|---|
| `GET /chain/status` | Reports pyntelope/pymongo availability and `chain_enabled` flag. |
| `POST /chain/startrun` | Hashes the run setup (corpus_hash, criteria_hash, params_hash), derives a deterministic `run_id`, pushes `sscore.startrun`, mirrors to `sscore_runs` Mongo collection. |
| `POST /chain/logcell` | Builds canonical-JSON of the row, hashes it (sha256), persists the full payload to `sscore_cells` in MongoDB, pushes `sscore.logcell`. Returns `{trx_id, payload_hash, mongo_oid}`. |
| `POST /chain/sealrun` | Computes the binary-Merkle root over the run's payload_hashes (SHA-256), pushes `sscore.sealrun`, stores the leaves on the Mongo run doc. |
| `POST /chain/verifycell` | Builds the Merkle path for a given leaf and pushes `sscore.verifycell`. |
| `POST /chain/logimport` | Computes corpus_hash + prev_hash for the collection, pushes `sscore.logimport`, mirrors to `sscore_imports`. |

Key behaviours:

- **Tolerant of missing infrastructure.** If `pyntelope` is absent or
  `SSCORE_SIGNING_KEY` is unset (i.e. contract not yet deployed),
  every chain push returns `{trx_id: null, error: "chain disabled: …"}`
  and the off-chain persistence still happens. The UI keeps working;
  CSV rows just have empty `chain_trx_id` cells.
- **Canonical JSON for hash reproducibility.** `sort_keys=True`,
  `separators=(",",":")`, `ensure_ascii=False`. A third-party reviewer
  recomputes the hash with the same call.
- **Deterministic `run_id`**: first 8 bytes of
  `sha256(analyst | started_at | collection | criteria_hash)` —
  collision is astronomically unlikely and there's no chain-side
  auto-id round-trip required.
- **MongoDB collections used** (same instance as `backEnd.py`'s
  `myDB`): `sscore_runs`, `sscore_cells`, `sscore_imports`. No new
  database, no IPFS.
- **Configuration** via env vars `SSCORE_BC_URL`, `SSCORE_SIGNING_KEY`,
  `SSCORE_CONTRACT`, `SSCORE_MONGO_URL`, `SSCORE_MONGO_DB`. Falls back
  to the existing `configuration.json` Chroma/Ollama-style first-URL
  pick for the chain endpoint.

### 13.3 Blueprint registration

**[`newRAG/ingest_corpus.py`](./ingest_corpus.py)** — added a guarded
`app.register_blueprint(chain_bp)` after the existing `app = Flask(...)`.
If `chain_bridge` imports fail, the rest of the worker still runs and
the failure reason is printed on startup.

### 13.4 LLM-tag → Antelope-name folding

Antelope `name` allows only `[.a-z1-5]`; digits `0,6,7,8,9` are not
valid. Naïve replacement would drop them and collide common LLM tags
(`qwen3:8b` ≡ `qwen3:7b` ≡ `qwen3.b`). Fixed by digit-folding before
the regex strip: `0→o`, `6→g`, `7→s`, `8→t`, `9→n`. Verified outputs:

| raw | on chain |
|---|---|
| `mxbai-embed-large` | `mxbai.embed` |
| `mistral`           | `mistral` |
| `mistral-nemo`      | `mistral.nemo` |
| `qwen3:8b`          | `qwen3.tb` |
| `qwen3:7b`          | `qwen3.sb` |
| `gemma3:12b`        | `gemma3.12b` |
| `gemma3:4b`         | `gemma3.4b` |
| `llama3.1:8b`       | `llama3.1.tb` |
| `llama3.3:70b`      | `llama3.3.sob` |
| `deepseek-r1:14b`   | `deepseek.r1` |
| `phi4-reasoning`    | `phi4.reasoni` |

Raw and folded are both persisted per-row in MongoDB
(`{llm_model_raw, llm_model_chain}`) so the chain name is always
decodable. Documented in [`blockchain/README.md`](../blockchain/README.md).

### 13.5 Scoring UI wired

**[`src/component/scorePapersBat.js`](../src/component/scorePapersBat.js)**:

- At the start of `runScoring`, POSTs `/chain/startrun` with `analyst`
  (`localStorage.wharf_user_name`), `llm_model`, `embed_model`,
  `collection`, `criteria`, `params`, `n_papers`, `n_criteria`,
  `corpus_ref`. Captures `chainRunId` from the response.
- Inside the per-cell loop: POSTs `/chain/logcell` with the full row;
  receives `trx_id`, `payload_hash`, `mongo_oid` and merges them into
  the row before pushing to `setRows`. Collects all `payload_hash`es
  into `sessionPayloadHashes`.
- After the loop: POSTs `/chain/sealrun` with the run id and all
  payload_hashes. Worker computes Merkle root and pushes `sscore.sealrun`.
- Each chain call is wrapped in a try/catch with a `console.warn` and
  proceeds on failure — scoring is never blocked by chain
  unavailability.

UI changes:

- New **Chain** column in the per-criterion results table. Shows
  a blue `Tag` with the first 8 chars of the transaction ID when
  available, or an orange `Tag hash xxxxxx` when the cell was only
  hashed off-chain (signing key not configured).

CSV export grew four columns:

- `chain_run_id`, `chain_trx_id`, `payload_hash`, `mongo_oid` — all
  per-row, all populated when the chain push succeeds.

### 13.6 Ingestion UI wired

**[`src/component/dbFromCorpusPapers.js`](../src/component/dbFromCorpusPapers.js)**:

- Inside the existing `pollStatus` finish-event handler, fire-and-forget
  POST to `/chain/logimport` with `{analyst, collection, embed_model,
  n_papers, manifest_ref, corpus_ref}`. Logged to console; never
  blocks the finish banner. Skipped if the run finished with errors.

### 13.7 What's NOT yet built (deferred to follow-on sessions)

The four tracks listed in `BLOCKCHAIN_INTEGRATION_PLAN.md` §6 as
Phase-D-dependent (or v0.2):

- **Track 1 (k1_recover)** — Anchor wallet signs `startrun` /
  `sealrun`; contract verifies via `k1_recover`. Requires a contract
  v0.2 with the `screeners`-style table plus a signing helper in
  `scorePapersBat.js`. v0.1 uses worker hot key throughout.
- **Track 5 (BLS aggregate study seal)** — `aggsign` action and
  multi-judge co-signature collection.
- **Track 4 (VRF paper-subset sampling)** — `vrfsample` action +
  client-side proof builder.
- **Track 3 (Pedersen commit-reveal between judges)** — `commitvote` /
  `revealvote` actions and the per-judge commitment UI.

### 13.8 Deployment checklist

Before v0.1 produces real on-chain receipts, three actions are needed
by whoever has admin rights on the Spring chain:

1. `cd blockchain && cdt-cpp -abigen -o sscore.wasm sscore.cpp`
2. `cleos -u <BC_URL> create account eosio sscore <PUB> <PUB>`
3. `cleos -u <BC_URL> set contract sscore ./ sscore.wasm sscore.abi -p sscore@active`
4. `export SSCORE_SIGNING_KEY=<WIF>` in the worker's shell, restart
   `python newRAG/ingest_corpus.py`
5. The UI's `/chain/status` button (forthcoming) confirms
   `chain_enabled: true`.

Until step 4 is done, the UI/CSV continue working but `chain_trx_id`
remains `null` for every row; only `payload_hash` and `mongo_oid` are
populated.

### 13.9 Files touched in this session (Section 13)

| file | type | purpose |
|---|---|---|
| `blockchain/sscore.cpp` | NEW | v0.1 contract source |
| `blockchain/README.md`  | NEW | build/deploy/smoke-test, name-fold table |
| `newRAG/chain_bridge.py` | NEW | Flask blueprint, MongoDB mirror, name-safe |
| `newRAG/ingest_corpus.py` | EDIT | register blueprint (guarded) |
| `src/component/scorePapersBat.js` | EDIT | call `/chain/{startrun,logcell,sealrun}`, new Chain column, 4 new CSV columns |
| `src/component/dbFromCorpusPapers.js` | EDIT | call `/chain/logimport` on finish |
| `newRAG/CHANGELOG.md` | EDIT | this section |

All Python and JSX parse-checked clean. Smoke test verified
`chain_bridge.py` helpers (canonical-JSON hash, deterministic `run_id`,
Merkle root + path/bits, `name_safe` on 11 representative LLM tags).

---

## 14. Track 1 — analyst signatures (contract v0.2)

User said: *"lets go with track 1. when doing batch tests i want to use
hardcoded key — not anchor."* — so the design supports both paths but
only the hardcoded (server-side signing) one is implemented in this
iteration; Anchor-wallet client-side signing was scoped as a follow-on
v0.2.1 but is deliberately not pursued for the current paper — see §15.7
and §16.

### 14.1 Contract changes — [`blockchain/sscore.cpp`](../blockchain/sscore.cpp)

- New table **`analysts`** keyed by `account` → `(public_key pubkey, bool active)`.
- New action **`setanalyst(name, public_key, bool active)`** — admin
  (`sscore@active`) registers / updates / disables an analyst.
- Helper **`_verify_analyst_sig(analyst, canonical, sig)`** — looks up
  the analyst, calls `sha256(canonical)` → `recover_key(digest, sig)` →
  refuses unless the recovered pubkey equals the registered one.
- **`startrun`** and **`sealrun`** and **`logimport`** each gain a
  trailing `signature analyst_sig` parameter. Before they mutate state
  they `pack(make_tuple(...other params...))` and verify.
- **`logcell`**, **`verifycell`**, **`clearrun`** unchanged — the
  analyst sigs on the boundary actions (`startrun`/`sealrun`) attest to
  the entire run, so per-cell sigs would be redundant.

### 14.2 Worker bridge — [`newRAG/chain_bridge.py`](./chain_bridge.py)

- **`ANALYST_KEYS = { "boniradev": "5K…" }`** — hardcoded
  `analyst_name → WIF` map. For batch (unattended) runs the worker
  signs the canonical payload itself using this dict. Documented in
  `blockchain/README.md` §7.
- New ABI types in `_serialize_action_data`:
  - `signature` → `_serialize_signature(sig_k1_str)` →
    `0x00 || sig[:65]` (1 type byte + 65 raw sig bytes).
  - `public_key` → `_serialize_public_key(pk_str)` →
    `0x00 || pk[:33]` (1 type byte + 33 compressed pubkey bytes).
- New helper **`_analyst_sign(analyst, params_for_sig)`** — builds the
  canonical bytes via `_serialize_action_data(params_for_sig)`, signs
  with the analyst's WIF (via the pyntelope.utils signer we already
  use for transaction-level signatures), returns a `SIG_K1_<base58>`.
- `/chain/startrun`, `/chain/sealrun`, `/chain/logimport` now:
  1. Build `params_for_sig` (everything *except* the trailing
     `analyst_sig` field) in the exact order the contract re-packs;
  2. Prefer a client-supplied `analyst_sig` from the request body
     (future Anchor path); otherwise call `_analyst_sign`;
  3. Append the signature as a `signature`-typed param and push.
- `/chain/sealrun` looks up the analyst from the MongoDB run mirror so
  the same analyst seals the run that opened it.
- New endpoint **`POST /chain/setanalyst`** — admin convenience for
  registering an analyst from the worker host (uses `sscore@active`).

### 14.3 ABI / wire-format verification

A smoke test in the venv (where pyntelope is broken-at-import but the
direct-HTTP path is fully functional) confirmed:

- `_serialize_signature(SIG_K1_…)` → 66 bytes, leading byte `0x00`.
- `_serialize_public_key(EOS…)` → 34 bytes, leading byte `0x00`,
  followed by the 33-byte compressed point with `0x02` or `0x03` prefix.
- `_analyst_sign("boniradev", [{run_id…},{rows_root…}])` → 101-char
  `SIG_K1_<base58>` string. Round-trips through `_serialize_signature`
  to 66 wire bytes.

### 14.4 Deployment

To activate v0.2 on the running chain:

```bash
# 1. Recompile
cd blockchain
cdt-cpp -abigen -o sscore.wasm sscore.cpp

# 2. Re-set the contract (the existing runs/cells/imports tables stay)
cleos -u http://blockchain2.uni-plovdiv.net:8033 set contract sscore ./ \
    sscore.wasm sscore.abi -p sscore@active

# 3. Register the analyst pubkey
cleos -u http://blockchain2.uni-plovdiv.net:8033 push action sscore setanalyst \
    '["boniradev","EOS8j4Egh7co1dagMDLEVEf1GVJB1Xi7GGYJEbiS6rGfMeMZMsj7m",true]' \
    -p sscore@active

# 4. Restart the worker (it now signs each boundary action as the analyst)
python newRAG/ingest_corpus.py
```

After step 4, the next scoring run produces:

- a `startrun` transaction whose embedded `analyst_sig` recovers to the
  analyst's registered pubkey — refused on chain otherwise;
- per-cell `logcell` transactions as before (worker-signed, no change);
- a `sealrun` transaction also analyst-signed.

The on-chain semantic of a sealed run becomes: *"the human analyst
identified by `EOS8j4Egh…` cryptographically signed both ends of this
run, and the set of cells in between hashes to this Merkle root."*

### 14.5 What's still deferred

- **v0.2.1 — Anchor-wallet client-side signing — designed, not pursued.**
  The bridge already accepts a client-supplied `analyst_sig` from the
  request body in preference to the `ANALYST_KEYS` fallback, so the
  full server-side surface is ready. The React signing flow (Anchor
  via `@wharfkit/session` — plumbing is in `TestWharf.js`) was scoped
  but **deliberately not implemented** for the current paper; the
  decision and its rationale are recorded in §16 below.
- Phase-D tracks 3, 4, 5 unchanged from §13.7.

---

## 15. Scoring jobs (server-side orchestration)

User said: *"I want 'Paper scoring' tasks to be grouped in jobs (similarly
to what we did in LLMScreening). I want to be able to pause and resume
jobs, and to see completed jobs, and to download (or just inspect) their
results."* The three follow-up decisions locked in:

- **Replace** the in-browser scoring loop (don't keep a Quick mode).
- **Jobs panel inside `scorePapersBat.js`**, above the existing config cards.
- **On worker restart**, jobs left in `running`/`paused` are marked
  `interrupted`; the operator must explicitly resume.

### 15.1 Architecture shift

The scoring step moves from **browser-side (LangChain.js in React)** to
**server-side (Python worker thread per job)**. The browser becomes a
thin client: it creates jobs, polls status, opens the job-detail view,
and downloads CSVs. The worker does:

- Chroma similarity search via direct HTTP (`/api/v2/.../query`)
- Ollama `/api/embeddings` for the criterion query
- Ollama `/api/generate` with `format: "json"` for the LLM-as-judge call
- Strict-JSON parsing
- Per-cell `payload_hash` + MongoDB persistence + `sscore.logcell`
- Sealing via `sscore.sealrun` on completion

Net effect: jobs continue running when the browser is closed. Pause and
resume actually pause and resume (event-based, no busy loops). On worker
restart, in-flight jobs are preserved in MongoDB and resumed manually
by the operator.

### 15.2 New backend module — [`newRAG/scoring_jobs.py`](./scoring_jobs.py)

Flask blueprint mounted at `/jobs`. ~700 lines. Endpoints:

| route | purpose |
|---|---|
| `POST /jobs`               | create + start a new scoring job |
| `GET  /jobs`               | list jobs (newest first; optional `?state=` filter) |
| `GET  /jobs/<id>`          | single job's full status (adds `thread_alive`, `pause_flag`) |
| `POST /jobs/<id>/pause`    | set the per-thread `pause_event` |
| `POST /jobs/<id>/resume`   | clear `pause_event` if alive, else spawn a fresh thread |
| `POST /jobs/<id>/cancel`   | set `cancel_event`; mark state `cancelled` |
| `DELETE /jobs/<id>`        | delete the job doc (cells optionally with `?with_cells=true`) |
| `GET  /jobs/<id>/cells`    | all scored cells for the job (from `sscore_cells`) |
| `GET  /jobs/<id>/csv`      | stream a CSV download (12 columns) |

Key implementation notes:

- **State machine.** Seven states: `queued` → `running` ↔ `paused`,
  with terminal states `completed`, `cancelled`, `error`, plus the
  recovery-only `interrupted` (worker restart).
- **`mark_orphans_interrupted()`** runs once on worker startup
  (called by `ingest_corpus.py` after blueprint registration).
  Sweeps MongoDB and moves any `running`/`paused` job to `interrupted`
  with a timestamp. Logged to stdout for visibility.
- **Per-job thread.** `_start_thread(job_id)` is idempotent — won't
  spawn a second thread for an already-live job. Threading objects
  (`pause_event`, `cancel_event`) live in module-level `RUNTIME` dict;
  durable state lives in MongoDB. Worker restarts therefore lose only
  the *threading objects* — durable state is intact.
- **Resume semantics.** A `resume` call either clears the live
  thread's `pause_event` (fast path), or spawns a fresh thread that
  reads the persisted state and **skips cells already in `sscore_cells`
  for this job** (idempotent re-entry). Resume works from `paused`,
  `interrupted`, or `error` — never from terminal `completed`/`cancelled`.
- **Chroma query.** Self-contained `_chroma_query()` helper calls
  `POST /api/v2/.../collections/{id}/query` directly. Inlined (rather
  than imported from `ingest_corpus.py`) to avoid the circular dep
  that would arise from blueprint registration.
- **Chain integration.** Reuses `chain_bridge.py`'s
  `_analyst_sign`, `push_action`, `derive_run_id`, `merkle_root_sha256`,
  `canonical_bytes`, `sha256_hex`, `name_safe`. Server-side signing
  uses the same `ANALYST_KEYS` dict as the existing `/chain/*`
  endpoints, so a job's `startrun` / `logcell` / `sealrun` push
  exactly the same transactions the v0.2 client-side flow did.
- **Per-cell error isolation.** A failing cell records `score: null`
  with the error string in `justification`; the job keeps going. Only
  hard chain or chroma failures mark the whole job `error`.
- **Cell schema in MongoDB** (`sscore_cells`) now carries `job_id`
  alongside the existing `payload_hash`/`run_id` fields, so a job's
  cells are easy to filter (`{job_id: <uuid>}`).

### 15.3 Wiring — [`newRAG/ingest_corpus.py`](./ingest_corpus.py)

After the existing `chain` blueprint registration, the worker now also:

```python
from scoring_jobs import bp as jobs_bp, mark_orphans_interrupted
app.register_blueprint(jobs_bp)
n = mark_orphans_interrupted()        # marks crashed jobs as 'interrupted'
```

Logged on startup so the operator sees how many jobs need manual resume.

### 15.4 React UI — full rewrite of [`scorePapersBat.js`](../src/component/scorePapersBat.js)

The in-browser scoring loop (`scorePaperCriterion`, `runScoring`,
`SCORING_PROMPT`, `tryParseJSON`, `stopRef`, `OllamaEmbeddings`,
`@langchain/community/vectorstores/chroma`) is **removed entirely**.
The component is now a thin client over the new `/jobs` endpoints.

Layout (top to bottom):

1. **Jobs panel** (new) — DataTable of all jobs with state-coloured
   tag, progress bar, LLM, created-at, action buttons (Pause /
   Resume / Cancel / View / Download CSV / Delete). Auto-polls every
   2.5 s while any job is non-terminal *or* the detail dialog is
   open; idle otherwise.
2. **Source card** — ChromaDB URL, Collection (filterable Dropdown
   from the existing v0.2 work), Embedding model, Ingestion API.
3. **LLM card** — Analyst, Ollama URL, LLM model, Top-K, Card-gate
   min score, Temperature, Card-gate toggle.
4. **Criteria card** — JSON textarea + file uploader (unchanged).
5. **Embed-guard banner** (unchanged).
6. **Papers card** — DataTable of papers with checkbox selection.
7. **Create-job card** — single button "🚀 Create scoring job" that
   POSTs to `/jobs` with the assembled config. After creation, the
   job-detail dialog auto-opens for the new job.

The **job-detail dialog** is the rich workspace:

- Status row (state tag + thread-alive tag + chain-run-id tag +
  sealed-with-rows-root tag);
- Error block (when `state === "error"`);
- Progress bar + currently-scoring paper/criterion + control buttons
  (Pause / Resume / Cancel / Download CSV);
- **Chain receipts panel** — `startrun_trx`, `sealrun_trx`,
  `rows_root`. Each trx id is a clickable link that opens the
  existing Memento-backed Transaction dialog.
- **Ranking panel** — weighted-average ranking computed in-browser
  from the cells (uses each criterion's `weight` from the criteria
  JSON).
- **Cells panel** — DataTable of every cell with score / source /
  justification / evidence / payload-hash. Shows `?` warning tag
  for cells whose JSON failed to parse.

State management is intentionally simple: one `jobs` array, one
`detailJob` doc, one `detailCells` array. The polling effect is the
single source of refresh — no per-row WebSocket / SSE.

### 15.5 What's preserved from v0.2

- The blockchain audit trail end-to-end works *exactly* as before.
  Same `sscore.startrun` / `logcell` / `sealrun` / `verifycell`
  actions; same analyst signatures via `_analyst_sign`; same on-chain
  Merkle proofs; same MongoDB cell mirror; same `payload_hash`
  canonicalisation in §4.7 of `PAPER_METHODOLOGY.md`. The
  *orchestration* moved from the browser to a Python thread, but
  *what lands on chain* is byte-for-byte identical.
- The chain-transaction viewer dialog (Memento-backed) is still
  available — now reached by clicking trx ids inside the job-detail
  dialog instead of per-row in a live table.
- The configuration UI (Source / LLM / Criteria / Papers) is
  unchanged in look and persistence keys, so existing user state in
  localStorage (`selectedOllama`, `selectedChromaDB`,
  `selectedLLMModel`, `papersEmbedModel`, `papersCollection`,
  `sscoreAnalyst`, `ingestAPI`) is reused.

### 15.6 Smoke test path

```bash
# 1) Restart the worker so blueprint + orphan sweep run
python newRAG/ingest_corpus.py
# look for: [jobs] /jobs/* endpoints registered

# 2) From the UI: configure a tiny job (2 papers × 2 criteria), click
#    'Create scoring job'. Job-detail dialog opens; progress bar
#    advances as the Python worker scores cells; chain trx-ids
#    appear in the receipts panel.

# 3) Click 'Pause'. The state badge flips to 'paused'. Click 'Resume'.
#    State flips back to 'running'.

# 4) Click 'Cancel' on a mid-flight job. State flips to 'cancelled'.

# 5) Run a job to completion. Download CSV. Verify the CSV has all
#    12 columns and one row per scored cell.

# 6) Restart the worker mid-job (simulate a crash). The stdout should
#    report 'marked N orphan job(s) as interrupted'. The UI shows
#    those jobs with severity=warning, label='interrupted'. Click
#    Resume — state flips to 'running' and scoring continues from
#    the last completed cell (via the existing-cell skip in
#    `_run_job`).

# 7) Verify on chain:
cleos -u <BC_URL> get table sscore sscore runs   -L 10
cleos -u <BC_URL> get table sscore sscore cells  -L 50
```

### 15.7 What's NOT in this iteration

- **VRF sampling (Track 4 from BLS_ROADMAP.md)** — not wired yet.
  Job creation still uses the user's manual paper selection.
- **Anchor-wallet client-side signing** — **designed, not pursued**
  for this paper. The hardcoded `ANALYST_KEYS` WIF path is the
  production signing route. See §16.
- **Per-cell trx-id viewing inside the job-detail dialog.** The cell
  table shows `payload_hash` but not the individual `logcell` trx id
  (it isn't yet stored on the cell doc — could be added in a few
  lines if reviewers want it).
- **Job re-ordering / priority queue.** Jobs run in their own
  threads, sharing Ollama capacity. Multiple parallel jobs serialise
  at Ollama; a job queue with `MAX_CONCURRENT` would be a small
  follow-up if needed.
- **Server-Sent Events / WebSocket** — polling is currently 2.5 s
  and adequate. Can be upgraded later.

### 15.8 Files touched in this iteration

| file | type | purpose |
|---|---|---|
| `newRAG/scoring_jobs.py` | **NEW** | Flask blueprint + per-job worker thread |
| `newRAG/ingest_corpus.py` | EDIT | register blueprint + orphan sweep |
| `src/component/scorePapersBat.js` | REWRITTEN | Jobs panel + job-detail dialog; in-browser scoring loop removed |
| `newRAG/CHANGELOG.md` | EDIT | this section |

All Python and JSX parse-checked clean.

---

## 16. Decision — Anchor-wallet client-side signing is out of scope

Decided 2026-05-27.

**Decision:** the Anchor-wallet (interactive) signing flow scoped as
v0.2.1 will *not* be implemented for the current paper. The design is
preserved in §14.5, §15.7, `blockchain/README.md` §7, and
`PAPER_METHODOLOGY.md` §4.9.2 mode C / §8.1 so it can be revived
later, but no contract change, no bridge change, and no React change
will be made under this manuscript.

**Rationale.**

1. The scoring methodology paper (the immediate publication) only
   needs to claim that *every scoring run is bracketed by signatures
   from the named human analyst, verified on-chain by
   `eosio::recover_key` against a pre-registered pubkey*. Mode B
   (per-analyst hot key in `ANALYST_KEYS`) already supports this
   claim — the analyst's key is distinct from `sscore@active`, the
   verification on chain is identical, and the audit trail is
   complete.
2. The marginal claim Anchor signing adds — *"no analyst private key
   ever touched the worker"* — is a stronger threat-model assertion
   than the current paper makes. Reviewers of a methodology paper
   typically question agreement metrics, faithfulness, and sample
   construction, not key custody. The custody concern is correctly
   noted as a Threat to Validity (`PAPER_METHODOLOGY.md` §8.1) and
   left as future work.
3. The implementation cost (~3 days) buys no methodological lift
   and would push the publication timeline without changing any
   reported number.

**What remains true regardless of the decision.**

- The bridge endpoint `/chain/startrun`, `/chain/sealrun`,
  `/chain/logimport` already accept a `analyst_sig` field in the
  request body and prefer it over the `ANALYST_KEYS` fallback. So if
  a future fork wants to add the React-side signing, the wire
  protocol does not need to change.
- The contract's `_verify_analyst_sig` is mode-agnostic: it accepts
  any signature that recovers to the analyst's registered pubkey,
  regardless of where the signing happened. The `analysts` table is
  also reusable.
- The "designed but not pursued" trace in the docs is intentional —
  it both records the option for paper reviewers and signposts the
  next obvious improvement for follow-on work.

---

## 99. One-screen recap for a future maintainer

- The corpus-scoring system has two components: ingestion
  (`dbFromCorpusPapers.js` + `ingest_corpus.py`) and scoring
  (`scorePapersBat.js`).
- Vector store: one Chroma collection per embedding model, tagged with
  `embed_model` in collection metadata. Ingesting with a different
  embedder is refused server-side. The UIs warn before they let you
  try.
- Talks to ChromaDB **directly via HTTP v2** — bypasses the
  `KeyError('_type')` schema-mismatch in the language SDKs. Never sends
  a `configuration` field on create.
- Embeddings via Ollama. Defaults to `selectedLLMModel` (matches the
  old `dbFromPDF.js` behaviour); override via the inline input.
- Two-tier chunking: 1 card chunk (≤ 1500 chars) + N body chunks
  (800/120). References tagged but kept. Per-paper isolation via
  metadata filter at retrieval time.
- Scoring: card-gate (optional, default on) then top-K body retrieval
  with `paper_id` filter, LLM in JSON mode returns
  `{score, justification, evidence}`. Per-criterion weighted average
  drives ranking.
- CSV export includes `llm_model`, `embed_model`, `collection`,
  `run_started_at` per row; filename includes the LLM. Concatenate
  multiple exports → pivot in pandas to compare judges.
- Documentation: user-facing in
  `EXPLANATION.md`, methodology in `PAPER_METHODOLOGY.md`, future
  blockchain audit-trail in `BLOCKCHAIN_INTEGRATION_PLAN.md`, this
  development log.
- Blockchain integration **not yet built** — design only. When ready,
  follow `BLOCKCHAIN_INTEGRATION_PLAN.md` §6 adoption sequence:
  v0.1 + Track 1 first, then Phase-D-dependent tracks 5 → 4 → 3.

---

*End of development log.*
