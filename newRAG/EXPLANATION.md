# Paper-Scoring RAG — Explanation

## 1. The original request (verbatim)

> look at this project carefully. in it i realized number of llm techniques. look
> at RAG realisations — `dbFromPDF.js` (creates vector store in ChromaDB),
> `testRAGbat.js` (where using RAG, i'm sending number of questions with referent
> answer to LLM using vector store, and score the answers), `chatFromDB` (where
> i'm asking single question to llm using RAG and vector store). I have new goal —
> to score number of papers using llm about their relevance to specific criterias.
> The corpus of papers are in `./newRAG/corpus` folder. they are in pdf format.
> at first i have to create vector store with them. inspect several of PDFs (10
> i.e. or more, if it is needed) and propose to me new way of creating appropriate
> vector store in chromadb. all new functionalities will be separated in new
> components of the existing app.

Follow-up answers given by the user (used as fixed design decisions):

- **Where ingestion runs:** Python worker.
- **Collection layout:** single Chroma collection, per-paper isolation via
  metadata filter.
- **References handling:** keep references chunks but tag them
  `section: "references"` so they can be excluded at query time.
- **Scope:** scaffold both new components at once; keep all existing app
  functionality working.
- **Ollama settings:** the worker for the vector store must use the app's
  configured Ollama path and Ollama model, as the old `dbFromPDF.js` did.

## 2. What I learned from the existing app

| Component | Role | Key behavior |
|---|---|---|
| `src/component/dbFromPDF.js` | Builds vector store from uploaded PDFs in the browser. | Reads `selectedOllama` + `selectedLLMModel` from `localStorage`, splits with `RecursiveCharacterTextSplitter (1024/50)`, calls `Chroma.fromTexts`, metadata only `{ source: fileName }`. |
| `src/component/testRAGbat.js` | Sends Q&A batches, scores answers via backend metrics endpoint. | Introduced a separate `selectedEmbeddingModel` with `mxbai-embed-large` fallback, two retrievers (Normal / ScoreThreshold). |
| `src/component/chatFromDB.js` | Single-question RAG chat over a collection. | Same memoized embeddings + LLM wiring as `testRAGbat.js`. |
| `src/component/SelectModel.js` | App-wide Ollama / Chroma / model picker. | Writes `selectedOllama`, `selectedChromaDB`, `selectedLLMModel`, `selectedMultiAgent`, retriever settings to `localStorage`. |

So the app has **two patterns**:

1. **Old (`dbFromPDF.js`):** uses `selectedLLMModel` directly as the embedding
   model (`new OllamaEmbeddings({ model: selectedLLMModel, baseUrl: selectedOllama })`).
2. **New (`testRAGbat.js`, `chatFromDB.js`):** uses a dedicated embedding
   model (`mxbai-embed-large`) with a fallback test, because some chat LLMs
   don't expose `/api/embeddings` reliably.

## 3. What I learned from the corpus

`./newRAG/corpus` contains **565 PDFs, 806 MB**. Stats from 15 deep-sampled +
50 stat-sampled files:

| metric | min | median | mean | max | p95 |
|---|---|---|---|---|---|
| size MB | 0.1 | 0.7 | 1.1 | 5.7 | 3.9 |
| pages | 2 | 9.5 | 15.8 | 112 | 49 |
| est chars | 400 | 33 K | 38 K | 112 K | 90 K |

Findings:

- Filenames follow `NNN - <title-fragment>.pdf` for **562 of 563** files →
  reusable as a stable `paper_id`.
- ~95 % have a detectable **Abstract** in the first two pages, many also have
  **Keywords**, DOI, and arXiv IDs.
- References sections start around **80–92 %** of the document — they
  introduce a lot of off-topic embeddings (author names, unrelated titles).
- All papers are about e-voting / blockchain voting, so the corpus is
  topically homogeneous → flat similarity search would confuse papers.
- A small slice has CID-glyph-encoded text (e.g. `490 - sustainability-...`),
  which `pypdf` can't decode well — needs a `pdfplumber` fallback.

## 4. Why the old `dbFromPDF.js` flow is unfit for paper-scoring

1. **No per-paper isolation.** `Chroma.fromTexts` puts every chunk in one bag.
   When scoring paper #123, retrieval pulls chunks from #50 and #200 because
   the corpus is topically homogeneous.
2. **Metadata is only `{ source: fileName }`** — no `paper_id`, no section,
   no page, no chunk index, no content hash.
3. **Page text is mashed** — `items.map(...).join(' ')` loses headers, footers,
   section boundaries; references pollute retrieval.
4. **Browser-side parsing.** Doing 800 MB / 565 PDFs in `pdfjs` + embeddings
   in the browser is slow, memory-fragile, and has no resume on crash.
5. **No content-hash de-dup.** Re-running re-inserts duplicates.
6. **No fallback for CID-encoded / image-only PDFs.**

## 5. Proposed design (built)

### 5.1 Vector-store layout

- **One Chroma collection** (default name `papers_corpus`), so cross-corpus
  search still works.
- **Per-chunk metadata:**

  | key | example |
  |---|---|
  | `paper_id` | `"001"` (from `NNN - …pdf`) |
  | `filename` | `"001 - 2509.22965v2.pdf"` |
  | `title` | `"Blockchain-Based Secure Online Voting Platform …"` |
  | `doi`, `arxiv_id` | when found |
  | `section` | `card` \| `abstract` \| `intro` \| `method` \| `results` \| `discussion` \| `conclusion` \| `references` \| `body` \| `appendix` |
  | `page_from`, `page_to` | page-range covered by the chunk |
  | `chunk_index`, `total_chunks` | for ordering / debugging |
  | `pages_total` | for context |
  | `content_hash` | sha1 of chunk text, for idempotent insert |
  | `ingest_run` | ISO timestamp of the ingestion job |

- **Two-tier chunking per paper:**
  - **Card chunk** = `Title + Abstract + Keywords` (tagged `section: "card"`).
    Used as a cheap relevance gate at scoring time.
  - **Body chunks** split per-section with `chunk_size=800`, `overlap=120`,
    never crossing section boundaries. References are kept but tagged so
    they can be filtered out (`{ section: { $nin: ['references','card'] } }`).

- **Idempotency.** Chunk IDs are `{paper_id}-{section}-{idx}-{sha1[:10]}`;
  re-ingesting skips chunks already present.

### 5.2 Pipeline (Python worker)

```
folder scan
  → pdfplumber extract  → pypdf fallback
  → strip repeating headers/footers
  → detect sections (regex)
  → build card chunk (Title + Abstract + Keywords)
  → split each section into body chunks
  → hash, dedupe-check, embed via Ollama
  → write to Chroma with metadata
  → progress + manifest_<collection>.json
```

### 5.3 Scoring (browser component)

For each (paper, criterion):

1. **Card-gate** (optional, default on): `similaritySearch(criterion, k=1, { paper_id, section: "card" })` →
   LLM scores against just the card. If score < `minCardScore`, stop early.
2. **Body retrieval**: `similaritySearch(criterion, topK, { paper_id, section: { $nin: ['references','card'] } })`.
3. LLM prompt requests **strict JSON** `{ score, justification, evidence }`
   over those chunks (uses `format: 'json'` in `@langchain/ollama`).
4. Per-criterion results are aggregated into a weighted-average ranking and
   exportable as CSV.

## 6. Ollama settings — explicit answer to your follow-up

You asked whether I kept in mind that the app already has Ollama path and
Ollama model settings, and that the worker that creates the vector store must
use them (as in `dbFromPDF.js`). **Confirmed and fixed.**

| Setting | localStorage key written by `SelectModel.js` | Old `dbFromPDF.js` | New `dbFromCorpusPapers.js` | New `scorePapersBat.js` |
|---|---|---|---|---|
| Ollama URL | `selectedOllama` | reads ✓ | reads ✓ (sent to Python worker as `ollama_url`) | reads ✓ |
| LLM model | `selectedLLMModel` | used as **embedding** model | used as **default embedding** model (overridable) | used as **LLM** for scoring + default embedding |
| ChromaDB URL | `selectedChromaDB` | reads ✓ | reads ✓ | reads ✓ |
| Temperature | `chatTempreture` | reads ✓ | reads ✓ (default of 0.2 also kept) | reads ✓ |
| Retriever / similarity / k | `retriever`, `symScore`, `k`, `kInc` | — | — | not used (we always filter by `paper_id`); top-K is configured in the form |

The fix I just applied:

- `embedModel` state in both new components now initializes from
  `localStorage.selectedLLMModel` (with `papersEmbedModel` as a per-page
  override) instead of being hardcoded to `mxbai-embed-large`.
- The Python worker's CLI and Flask `/start` now default `embed_model` to
  `mistral` (the same default the app uses for `selectedLLMModel`).

So by default: the embedding worker = **the same Ollama URL + same LLM model
you set in SelectModel** — exactly as `dbFromPDF.js` did. If you want a
dedicated embedding model (e.g. `mxbai-embed-large`, which is what
`testRAGbat.js` and `chatFromDB.js` switched to), you can override it in
either UI; the override is persisted in `papersEmbedModel`.

## 7. Files added / changed

### New files

- `newRAG/ingest_corpus.py` — Flask worker (default port `8010`), also
  runnable as CLI (`--cli ...`). Endpoints:
  - `GET /health`
  - `POST /start` — body: `{ corpus_dir, chroma_url, ollama_url, collection, embed_model, chunk_size, overlap, limit? }`
  - `GET /status` — live progress
  - `POST /stop`
  - `GET /papers?chroma=...&collection=...` — distinct papers, with title and chunk count, used by the scoring component
- `src/component/dbFromCorpusPapers.js` (+ `.css`) — UI to drive ingestion.
  Defaults all settings from `localStorage` so it picks up the app's Ollama
  path and selected LLM automatically.
- `src/component/scorePapersBat.js` (+ `.css`) — UI to score loaded papers
  against a criteria JSON, using per-paper metadata filters.
- `newRAG/EXPLANATION.md` — this document.

### Existing files edited (minimal, additive only)

- `src/App.js` — added two imports + two `<Route>`s for the new components.
- `src/component/Nav.js` — added "From Paper Corpus" under "Create
  Vectorstore", and a top-level "Paper Scoring" menu item.
- `src/component/configuration.json` — added
  `"IngestAPI": "http://127.0.0.1:8010"`.

No existing component files were modified.

## 8. How to run

### One-time install of the Python deps

```bash
pip install pdfplumber pypdf chromadb flask flask-cors requests
```

### Start the worker (default: port 8010)

```bash
python newRAG/ingest_corpus.py
```

Or run a one-shot ingestion without the server:

```bash
python newRAG/ingest_corpus.py --cli \
  --corpus newRAG/corpus \
  --chroma http://127.0.0.1:8000 \
  --ollama http://127.0.0.1:11434 \
  --collection papers_corpus \
  --embed-model mistral \
  --chunk-size 800 --overlap 120
```

### From the UI

1. Confirm `SelectModel` shows the Ollama URL, ChromaDB URL, and LLM model
   you want to use. Those are the values the new components inherit by
   default.
2. **Create Vectorstore → From Paper Corpus** — the form is pre-filled from
   `localStorage`. Click **Start Ingestion**. Progress polls
   `/status` every 1.5 s. Use **List indexed papers** to confirm.
3. **Paper Scoring** — click **Load papers**, edit or upload a criteria JSON
   (`[{ id, criterion, weight, scale }]`), click **Run scoring**. Results
   appear per-criterion and as a weighted-average ranking, exportable to CSV.

## 9. Scoring LLMs compatible with an `mxbai-embed-large` vector store

**Concept first — there is no compatibility constraint on the scoring LLM.**
The embedding model (`mxbai-embed-large`, 1024-dim cosine) decides how chunks
are *stored* and how the *query* is embedded for retrieval. The scoring LLM
just receives the retrieved text as plain context — it is fully decoupled
from the vector space. So any chat model served by Ollama can be the scoring
LLM over the same vector store. The only constraint is:

> At query time the *same* `mxbai-embed-large` must embed the question.
> The chat LLM that grades the retrieved chunks can be anything.

That is exactly how this app is wired: `embeddings` and `llm` are two
separate `Ollama*` instances; you can change the LLM model from the scoring
UI without re-ingesting.

### Models you mentioned, with notes

(All consumed via Ollama; tags are illustrative — `ollama list` will show
exactly what you have pulled. All listed models handle the prompt size used
here — ~5–7 K chars context per criterion — comfortably.)

| Model family | Common Ollama tags | Notes for this task |
|---|---|---|
| **Mistral** | `mistral:7b-instruct`, `mistral-nemo`, `mistral-small`, `mixtral:8x7b` | Already your default. Good baseline. `mistral-nemo` (12 B, 128 K ctx) and `mistral-small` (22 B) are stronger judges. Supports Ollama's `format:"json"`. |
| **Llama 3 / 3.1 / 3.2 / 3.3** | `llama3:8b`, `llama3.1:8b`, `llama3.1:70b`, `llama3.2:3b`, `llama3.3:70b` | Strong instruction-following; 3.1+ have 128 K context. 3.2:3b is fast for bulk scoring; 70 B variants are top-tier judges if you have the RAM. JSON mode reliable. |
| **Llama 4** | `llama4:scout`, `llama4:maverick` | Mixture-of-experts, very long context (10 M tokens advertised). Overkill for chunk-based scoring; useful only if you want to dump whole papers. |
| **Gemma 2 / 3** | `gemma2:9b`, `gemma2:27b`, `gemma3:4b`, `gemma3:12b`, `gemma3:27b` | Excellent at structured outputs. Gemma 3 has 128 K context and multimodal variants. `gemma3:12b` is a sweet spot for laptop/desktop scoring. |
| **Granite 3 / 4** | `granite3-dense:8b`, `granite3.1-dense:8b`, `granite3.2:8b`, `granite3.3:8b`, `granite4` | IBM models, tuned for enterprise-style structured tasks (good at JSON, classification, summarization). Solid for objective criteria like "does the paper provide an evaluation". |
| **Qwen 2.5 / 3** | `qwen2.5:7b`, `qwen2.5:14b`, `qwen2.5:32b`, `qwen3:8b`, `qwen3:14b`, `qwen3:32b`, `qwen3:235b-a22b` | Best-in-class multilingual + reasoning at small sizes. Strong on JSON. `qwen3:8b` or `qwen3:14b` are excellent default judges; `qwen3:32b` if you want top quality locally. |
| **Phi 3 / 4** | `phi3:mini`, `phi3.5`, `phi4`, `phi4-mini`, `phi4-reasoning` | Small, fast, surprisingly capable for short structured judgments. `phi4-reasoning` is good for criteria that need a chain-of-thought. |
| **DeepSeek-R1 / V3** | `deepseek-r1:7b`, `deepseek-r1:14b`, `deepseek-r1:32b`, `deepseek-r1:70b` | Reasoning-focused; great for criteria where you want a justification, but slower because they think before answering. |
| **Command R / R+** | `command-r:35b`, `command-r-plus:104b` | Cohere's RAG-tuned models — explicitly designed to ground answers in retrieved context. Worth trying for evidence-quote criteria. |

### What I would actually pick

- **Embedding model (one and only one — pin it):** `mxbai-embed-large`.
  Fast, 1024-dim, English MTEB top-tier, well-supported in Ollama.
- **Scoring LLM (try several, then A/B):**
  - Cheap-and-fast pass: `qwen3:8b` or `gemma3:12b` — bulk-rank the corpus.
  - Higher-quality re-rank on the top survivors: `qwen3:32b`,
    `gemma3:27b`, or `llama3.3:70b` (if you have the RAM/VRAM).
  - For criteria that need rigorous justification: `deepseek-r1:14b` or
    `phi4-reasoning`.

### Practical tips for swapping LLMs without re-ingesting

1. In `SelectModel`, change `selectedLLMModel` to the new LLM (e.g.
   `gemma3:12b`). The scoring UI picks it up on mount.
2. Leave the `Embedding model` field as the one stamped on the collection
   (the embed-guard banner shows it; mismatch is now refused).
3. If you want to compare LLMs on the same papers and criteria: run
   `Run scoring`, **Export CSV**, change the LLM, run again, diff the CSVs.
4. To compare two embedding models, build two collections (the worker
   refuses to mix vector spaces) — e.g. `papers_corpus__mxbai` and
   `papers_corpus__nomic` — then point the scoring UI at each in turn.

## 10. Criteria JSON — format, examples, and tips

The scoring component reads a JSON array of *criterion objects*. Each
criterion is evaluated **independently** for every selected paper, and the
per-paper weighted average of those scores is what produces the ranking.

### 10.1 Schema

Each item in the array is an object with these fields:

| field       | type    | required | purpose |
|-------------|---------|---------:|---------|
| `id`        | string  | yes      | Short slug used as the row key in the results table and the column in the CSV export. Use `snake_case`, ASCII only, no spaces. |
| `criterion` | string  | yes      | The full natural-language question the LLM will be asked. It is interpolated *verbatim* into the scoring prompt — write it as a plain question, ending with a question mark, answerable from the retrieved chunks. |
| `weight`    | number  | no (default `1`) | Multiplier in the weighted average per paper. Higher weight → more influence on the final ranking. Set `0` to score-but-not-rank. |
| `scale`     | string  | no (default `"0-5"`) | Score range as `"min-max"`. Interpolated into the prompt as "score (integer in {scale})". Common: `"0-5"`, `"1-5"`, `"0-3"`, `"1-10"`, `"0-1"` (boolean). |

### 10.2 What the LLM actually sees (one call per (paper, criterion))

```
You are scoring an academic paper against a single criterion.
Return ONLY a strict JSON object with keys:
  score (integer in {scale}),
  justification (1-2 sentences),
  evidence (a direct short quote from the context that supports the score,
           or "").

Criterion: {your `criterion` text verbatim}

Context excerpts from the paper (treat as the only evidence available):
---
[chunk 1 | section=method | p.4]
<retrieved text>

[chunk 2 | section=results | p.7]
<retrieved text>
...
---
JSON:
```

The LLM returns one JSON object, parsed into the `score`, `justification`,
and `evidence` columns of the results table. A short `source` column is
also tracked: `card` (rejected by the card-gate), `body` (full retrieval),
or `none` / `error`.

### 10.3 The default example shipped with the component

This is what populates the textarea when you first open *Paper Scoring* —
edit in place or replace via the file uploader.

```json
[
  { "id": "topical_fit",  "criterion": "How directly does the paper address blockchain-based e-voting?",      "weight": 1, "scale": "0-5" },
  { "id": "method_rigor", "criterion": "Are the proposed methods technically rigorous and well-justified?",   "weight": 1, "scale": "0-5" },
  { "id": "evaluation",   "criterion": "Does the paper provide concrete experiments or evaluation results?", "weight": 1, "scale": "0-5" },
  { "id": "novelty",      "criterion": "How novel are the contributions versus prior work?",                 "weight": 1, "scale": "0-5" }
]
```

Four criteria, equal weights, 0–5 scale. Per-paper max average = 5. Saved
as e.g. `newRAG/criteria/default_relevance.json` so it can be re-loaded
later via *Load criteria JSON*.

### 10.4 How `weight` affects the ranking

```
avg_paper = Σ (score_i · weight_i)   /   Σ weight_i
            (only criteria whose score is a number)
```

So with weights `[2, 1, 1, 1]`, criterion #1 counts as much as the other
three combined. `weight: 0` runs the criterion (you still get the score in
the table and CSV) but excludes it from the average — useful for scoring
diagnostic checks like "Does the paper link a code repo?" without letting
them dominate.

### 10.5 Writing good criteria — checklist

- **One claim per criterion.** Bad: *"Is the paper rigorous **and** novel?"* —
  the LLM has to compress two judgments into one integer. Split into two.
- **Answerable from the chunks.** Citations, h-index, journal IF are NOT in
  the PDF body, so don't ask. "Does the paper compare against ≥2 prior
  methods?" — yes, that's visible in Related Work or Evaluation.
- **Anchor the scale.** *"How novel is it?"* is ambiguous. Better:
  *"Rate novelty 0 (no new idea) to 5 (introduces a fundamentally new
  mechanism)."* — gives the LLM landmarks and your scores stay comparable
  across runs.
- **No leading language.** *"Doesn't this paper have a great evaluation?"* →
  *"Does the evaluation include both quantitative metrics and a real-world
  deployment?"*
- **Keep it short.** ~1–2 sentences. The criterion plus retrieved chunks
  share the LLM's context budget.

### 10.6 Variations / templates

**Custom anchored scale (recommended for systematic reviews):**

```json
[
  {
    "id": "evidence_strength",
    "criterion": "Rate the strength of empirical evidence: 0 = none or only qualitative, 1 = small-scale experiment, 2 = multiple experiments with comparisons, 3 = field deployment or large-scale benchmark.",
    "weight": 2,
    "scale": "0-3"
  }
]
```

**Boolean (0/1) gating criteria — fast yes/no filters:**

```json
[
  { "id": "has_threat_model", "criterion": "Does the paper explicitly state a threat model? 1 = yes, 0 = no.",                       "weight": 1, "scale": "0-1" },
  { "id": "has_code",         "criterion": "Does the paper link a public code repository (GitHub/GitLab/Zenodo)? 1 = yes, 0 = no.",  "weight": 1, "scale": "0-1" }
]
```

**Domain-tilted criteria (e.g. blockchain-voting systematic review):**

```json
[
  { "id": "consensus_specified", "criterion": "Does the paper specify which blockchain consensus mechanism it uses (PoW, PoS, PBFT, …)?",                       "weight": 1, "scale": "0-5" },
  { "id": "ballot_secrecy",      "criterion": "Does the paper address ballot secrecy or voter anonymity, and how (homomorphic encryption, mixnets, zk-proofs)?", "weight": 2, "scale": "0-5" },
  { "id": "verifiability",       "criterion": "Does the paper provide cast-as-intended, recorded-as-cast, or tallied-as-recorded verifiability?",                "weight": 2, "scale": "0-5" },
  { "id": "scalability_eval",    "criterion": "Is scalability evaluated with concrete numbers (TPS, voter counts, gas costs)?",                                  "weight": 1, "scale": "0-5" }
]
```

**Diagnostic-only criterion (score, never ranks):**

```json
[
  { "id": "is_survey", "criterion": "Is this paper a survey or systematic review (rather than an original contribution)? 1 = yes, 0 = no.", "weight": 0, "scale": "0-1" }
]
```

### 10.7 Loading & editing — two ways

1. **Inline editing** — the textarea on the *Criteria* card is the live
   JSON. As you type, it is re-parsed; the "Parsed: N criteria" counter
   updates as soon as the JSON becomes valid. Invalid JSON keeps the old
   parsed list (no crash).
2. **File upload** — the `Select criteria JSON` button reads a `.json`
   file from disk and replaces both the textarea and the parsed list.

A criteria file is just plain JSON — version it in git, share it with
collaborators. Suggested layout next to your corpus:

```
newRAG/
  corpus/                          # 565 PDFs
  criteria/
    default_relevance.json         # the shipped 4-criterion default
    voting_systematic_review.json  # the domain-tilted set above
    binary_filters.json            # the 0/1 gating set
```

### 10.8 CSV export — one row per (paper, criterion)

**Filename** is `scores_{collection}_{llm_model}_{YYYY-MM-DD-HHMM}.csv`,
with non-alphanumerics replaced by `_`. Example:

```
scores_papers_corpus_mxbai_qwen3_8b_2026-05-21-1042.csv
```

The LLM name is taken from the most recent row in the table — so even if
you swap LLMs and run again, the exported file matches what produced the
data in it, not whatever is currently typed in the input.

**Columns:**

| column            | meaning |
|-------------------|---------|
| `paper_id`        | from the PDF filename's `NNN - ` prefix |
| `title`           | title extracted from the PDF |
| `criterion_id`    | your `id` field |
| `score`           | integer in the criterion's `scale` (blank if the LLM's JSON failed to parse) |
| `justification`   | 1–2 sentences from the LLM |
| `evidence`        | short quote from a retrieved chunk (or empty) |
| `source`          | `card` / `body` / `none` / `error` — see §10.2 |
| `llm_model`       | the scoring LLM at the time the row was produced (e.g. `qwen3:8b`) |
| `embed_model`     | the embedding model used for retrieval (matches the collection tag) |
| `collection`      | the Chroma collection the row was scored against |
| `run_started_at`  | ISO timestamp of when *that* `Run scoring` click started |

Because `llm_model` / `embed_model` / `run_started_at` are per-row, you
can concatenate multiple CSVs in pandas and still distinguish runs:

```python
import pandas as pd, glob
df = pd.concat([pd.read_csv(p) for p in glob.glob("scores_*.csv")])
df.groupby(["paper_id", "llm_model", "criterion_id"])["score"].mean().unstack("llm_model")
```

The aggregated ranking in the UI is computed in the browser from these
rows — pivot the CSV in pandas / Excel if you want a
`paper_id × criterion_id` matrix.

### 10.9 Interaction with the card-gate

When *Use card-gate* is on (default), each criterion is first scored
against the paper's **card chunk** (`Title + Abstract + Keywords`). If
that gate score is *strictly less than* `Card-gate min score`, the paper
is **rejected for that criterion** with `source: "card"` and the full
body retrieval is skipped — saves a lot of LLM calls when most of the
corpus is off-topic. Set `useCardGate=false` to score every paper fully.
The gate uses the same prompt and scale as the body pass — only the
context differs (card chunk vs. top-K body chunks).

## 11. Trade-offs and known caveats

- Embeddings via a chat LLM (e.g. `mistral`) are slower and lower-quality
  than a dedicated embedding model. The UI lets you switch to
  `mxbai-embed-large` — but the embedding model used at ingestion **must
  match** the one used at scoring (the metadata filter doesn't help if the
  vectors are in a different space).
- A handful of PDFs are CID-glyph-encoded and may still extract poorly even
  with `pdfplumber`; those papers will show up with `status: "skipped_no_text"`
  in the recent-files panel and the manifest.
- The default chunk size of 800 was picked for the corpus's per-page char
  density (~3–5 K chars/page) and `mxbai-embed-large`'s 512-token sweet
  spot. For very short or very long papers, you can tune from the UI.
- Card-gate uses the same LLM-as-judge prompt as the body pass, just over
  fewer tokens. Set `useCardGate=false` if you want to score every paper
  fully (more accurate, more expensive).
