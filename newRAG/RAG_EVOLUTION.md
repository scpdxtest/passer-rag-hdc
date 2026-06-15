# From legacy RAG to **NewRAG** — a comparative technical analysis

A side-by-side examination of the legacy `db-from-*` / `chatFromDB`
components and the **NewRAG** stack (paper-corpus ingestion, scoring,
chat, and audit trail) that this work introduces. Written so it can
feed directly into the implementation section of the follow-up paper.

> **Scope of the comparison.** *Legacy* = the components shipping in
> `src/component/`: `dbFromText.js`, `dbFromWEB.js`, `dbFromPDF.js`,
> `chatFromDB.js`, `testRAGbat.js`. *NewRAG* = the components in
> `newRAG/` and the new React components built on top of them
> (`dbFromCorpusPapers.js`, `scorePapersBat.js`, `chatNewRAG.js`).

---

## 0. Executive summary

The legacy stack treats a document corpus as **one undifferentiated
bag of text chunks** ingested in the browser and queried with flat
top-$k$ similarity. NewRAG instead treats it as a *structured
research corpus*: every chunk carries paper-level provenance,
section-level structure, and a content hash; ingestion is
server-side and idempotent; retrieval is metadata-filtered to a
single paper or a section subset; downstream scoring runs as
durable server-side jobs with full audit trail on an Antelope
chain.

The shift is not one improvement but nine, each tracked in §3–§6.
The headline aggregate effect:

| dimension | legacy | NewRAG |
|---|---|---|
| Corpus model | *bag of chunks*, source = filename | *graph*: paper → sections → chunks, with hashes, pages, identifiers |
| Where the work runs | Browser (LangChain.js) | Python worker (Flask), browser is a thin client |
| Retrieval semantics | flat top-$k$ over all chunks | filtered ($paper\_id$, section) top-$k$ |
| Re-ingestion | duplicates everything | idempotent via SHA-1 chunk IDs |
| Failure mode | one bad chunk → whole document lost | per-chunk failure isolation, automatic retry |
| Reproducibility | nothing tracked | embedding model, criteria, params, corpus hash committed on chain |
| Multi-LLM scoring | not part of the legacy stack | first-class, with pause/resume jobs |
| Scale | tens of PDFs (browser ceiling) | thousands (server bound) |
| Corpus type assumption (after §6) | *any text was a "paper"* | one of {`academic_paper`, `novel`, `manual`}; profile registry; new profiles addable in ~80 LOC |

The rest of this document gives the technical justification for each
of those rows.

---

## 1. Architectural shift

### 1.1 Legacy

Every step happened **in the browser**, driven by LangChain JS:

```
browser ──pdfjs──▶ string  ──RecursiveCharacterTextSplitter──▶ chunks
       └──OllamaEmbeddings.embedQuery──▶ vectors
       └──Chroma.fromTexts (vectors + metadata)─────────────────▶ ChromaDB
```

Concrete consequences:

- The browser tab must stay open for ingestion to complete.
- Page lifecycle bugs (refresh / navigation away) abort large
  uploads silently.
- The browser bundle ships the full LangChain JS retrieval / chain
  layer, plus `chromadb-js`, plus `pdfjs-dist` — a non-trivial
  footprint.
- A 565-PDF / 806 MB corpus is operationally infeasible — Chrome
  runs out of memory at the chunk-array stage.

### 1.2 NewRAG

A small **Python worker** (Flask, default port `8010`) hosts the
ingestion and scoring pipelines; the React UI is a thin client that
configures jobs, polls progress, and downloads results.

```
React ──POST /start──▶ ingest_corpus.py ──pdfplumber / pypdf──▶ pages
                                       └─section regex─▶ canonical text
                                       └─sliding splitter──▶ chunks (card + body)
                                       └─/api/embeddings (Ollama)─▶ vectors
                                       └─/api/v2/collections/{id}/upsert──▶ ChromaDB
React ──POST /jobs───▶ scoring_jobs.py ─per-thread loop─▶ Ollama /api/generate
                                       └─/chain/logcell────────────▶ Antelope sscore
```

Net consequences:

- Browser close, network drop, and OS sleep are all survivable —
  jobs persist in MongoDB and resume from the last completed cell.
- The chunk-array stays in the worker process, where 4 GB is
  normal, not exceptional.
- The React bundle no longer needs the LangChain ingestion path; the
  *chat* component still uses LangChain JS (it is interactive,
  one message at a time), but ingestion and scoring lift to Python.

---

## 2. Stage-by-stage comparison

### 2.1 PDF text extraction

| | legacy | NewRAG |
|---|---|---|
| Library | `pdfjs-dist` (Mozilla, browser) | `pdfplumber` (primary) + `pypdf` (fallback) |
| Layout awareness | per-page `items.map(it => it.str).join(' ')` | layout-aware paragraphs (pdfplumber) with whitespace normalisation |
| Header/footer handling | none — running titles repeat in every chunk | the "≥40 % of pages contain this short line" rule strips boilerplate before chunking |
| Hyphenation across line breaks | preserved verbatim | regex de-hyphenation (`-\n[a-z]` → `\1`) |
| CID-encoded PDFs (rasterised text) | extracts empty string silently | pdfplumber tries first; if extracted length < 200, pypdf fallback; if still empty, paper marked `skipped_no_text` in the manifest with a clear reason |
| Title / DOI / arXiv ID extraction | not attempted | regex scan over first 5 K chars for `Title:` line, `10\.\d{4,9}/…`, `arXiv[:\s]*\d{4}\.\d{4,5}` |
| Failure mode | exception silently swallows the whole PDF | per-PDF status row in `manifest_<collection>.json` |

Joining text items with a single space loses paragraph and column
structure, makes section detection (§2.3) materially harder, and
splits e.g. multi-column author affiliations into nonsensical runs.
pdfplumber's `extract_text()` reconstructs paragraph order using
layout boxes, which is why downstream section detection becomes
viable.

### 2.2 Chunking strategy

**Legacy** uses a single `RecursiveCharacterTextSplitter` with
`chunkSize: 1024, chunkOverlap: 50` (the default; user-configurable
in the UI). Splitting happens *per page-string*, ignoring document
structure. Each PDF produces a flat list of homogeneous chunks.

**NewRAG** uses a **two-tier** scheme:

1. **The card chunk** — one per paper, ≤ 1500 chars, composed of
   `Title + DOI + arXiv + Abstract (75 % of the budget) + Keywords
   (25 %)`. Tagged `section: "card"`. Designed as a *cheap
   relevance gate*: a downstream judge can ask "is this paper
   relevant at all?" against the card before triggering full
   retrieval, saving ~70 % of LLM calls when the corpus is broad.
2. **Body chunks** — sliding window of 800 chars with 120 overlap,
   produced per detected section, never crossing section
   boundaries. Tagged with the section name they belong to
   (`abstract`, `intro`, `method`, `results`, `discussion`,
   `conclusion`, `references`, …).

Quantitative impact on a 565-PDF corpus (English, IMRaD venues):

| metric | legacy | NewRAG |
|---|---:|---:|
| chunks per paper (median) | 38 | 53 (1 card + 52 body) |
| chunks per paper (range) | 4 – 230 | 9 – 280 |
| % chunks tagged with a section | 0 | 88 |
| % chunks tagged `references` | 0 | 21 (filterable at retrieval) |

The card-tag alone lets a multi-judge scoring run drop ~70 % of
papers at the gate with one cheap LLM call instead of `n_criteria`
expensive ones.

### 2.3 Section detection

Legacy: not attempted. Sections are dissolved into raw character
runs.

NewRAG: nine regex patterns scan the de-noised text for the
canonical headings. The patterns target the first non-whitespace
occurrence of (case-insensitive) any of:

```
abstract                   keywords | index terms
introduction | background  related work | literature review
methodology | method[s] | approach | proposed (system|method|approach|model) |
              system (architecture|design) | design | implementation
results | evaluation | experiments | findings
discussion | analysis
conclusion[s] | future work
references | bibliography
appendix | acknowledg(e)?ments?
```

Detected start offsets are sorted, deduplicated, and turned into
non-overlapping spans. A leading `body` span (before the first
detected header) catches the title page / author block. References
are kept but tagged so they can be excluded from retrieval at query
time (§2.6).

The downstream consequence: a criterion like *"Does the paper
provide concrete experiments or evaluation results?"* can be steered
to retrieve from `section: "results"` first. Legacy retrieval has no
such handle and routinely pulls related-work paragraphs that
mention evaluation in a literature-review sense.

### 2.4 Metadata model

This is the single largest functional gap.

**Legacy metadata per chunk** (`dbFromPDF.js` line 215):

```javascript
Chroma.fromTexts(
    docArr.map(d => d.text),
    docArr.map(d => ({ source: d.source })),   // <-- ONLY "source"
    embeddings_open, { collectionName, url },
);
```

That's it: one string field, equal to the filename. Per-cell
provenance, page numbers, section, chunk ordering, content hash —
none of it exists.

**NewRAG metadata per chunk** (per `ingest_corpus.py:process_paper`):

| key | example | role |
|---|---|---|
| `paper_id` | `"001"` | from filename `NNN -` prefix, primary retrieval filter |
| `filename` | `"001 - 2509.22965v2.pdf"` | full traceability |
| `title` | `"Blockchain-Based Secure Online Voting Platform …"` | display + audit |
| `doi`, `arxiv_id` | when present | external linking |
| `section` | `card` / `abstract` / `intro` / `method` / `results` / `discussion` / `conclusion` / `references` / `body` / `appendix` | section-targeted retrieval |
| `page_from`, `page_to` | 4, 5 | provenance for citations |
| `chunk_index`, `total_chunks` | 7, 42 | ordering / debugging |
| `pages_total` | 12 | document scale context |
| `content_hash` | sha1[:40] | idempotent re-ingestion |
| `ingest_run` | ISO timestamp | run lineage |

The `paper_id` field is the keystone — without it, the metadata
filter that gives NewRAG its per-paper isolation (§2.6) cannot be
expressed.

### 2.5 Embedding strategy

**Legacy** (`dbFromPDF.js`, `dbFromText.js`):

```js
const embeddings_open = new OllamaEmbeddings({
    model: selectedModel,         // <-- the LLM the user picked
    baseUrl: selectedOllama,
});
```

The *LLM model* (e.g. `mistral`) is used as the embedding model.
Most chat LLMs do expose `/api/embeddings`, but their embedding
quality is mediocre because they were not trained with a
contrastive objective. Worse: the user-perceived embedding model is
*hidden inside* the LLM choice, so switching the LLM silently
changes the vector space without any retrieval warning.

**NewRAG** separates the two:

- The embedding model is a first-class field (`embedModel`), with
  the sensible default `mxbai-embed-large` (English MTEB top-tier,
  1024-dim).
- The chosen embedding model is **stored in the Chroma collection
  metadata** at creation time (`collection.metadata.embed_model`).
- Any subsequent ingestion that requests a *different* embedding
  model against an existing collection is **refused at the worker
  level** with a clear error and a suggested per-model collection
  name (e.g. `papers_corpus__mxbai`).
- Both the ingestion UI and the scoring UI probe
  `/collection_info` and render a green/red banner indicating
  whether the configured embed model matches the collection's
  recorded tag — preventing the silent-garbage failure mode where
  retrieval returns 100 similar-looking but actually-random
  results.

This single guard eliminates a class of bugs that are nearly
impossible to diagnose downstream: vectors live in *different
embedding spaces*, similarity scores look numerically reasonable
(0.6–0.8 cosine), and the returned chunks bear no semantic
relationship to the query.

### 2.6 Retrieval semantics

**Legacy retrieval pattern** (`chatFromDB.js`):

```js
const vectorStore = await Chroma.fromExistingCollection(embeddings, {
    collectionName, url,
});
const retriever = vectorStore.asRetriever({ k: 100 });
const chain = ConversationalRetrievalQAChain.fromLLM(mdl, retriever, { memory });
const res = await chain.invoke({ question });
```

Retrieval is *unfiltered* top-$k$ over the whole collection. Two
problems for any topically-coherent corpus:

1. **Cross-paper contamination.** A query about paper $P$ routinely
   pulls chunks from papers $P', P'', \dots$ that happen to use the
   same vocabulary. The LLM then composes an answer from a
   chimera document the corpus authors never wrote.
2. **Reference pollution.** References-section text matches well on
   surface vocabulary (titles, author names, common method
   terms) and dominates the top-$k$ for technical queries.

**NewRAG retrieval** uses Chroma's `where` clause with the metadata
populated in §2.4:

- *Per-paper isolation.* When scoring or asking about paper $P$:
  ```js
  { paper_id: P,
    section: { $nin: ["references", "card"] } }
  ```
- *Card-gate.* Cheap relevance pre-check:
  ```js
  { $and: [{ paper_id: P }, { section: "card" }] }
  ```
- *Section-targeted retrieval.* For *"Does the method include …?"*:
  ```js
  { $and: [{ paper_id: P }, { section: "method" }] }
  ```

These filters are expressed identically in the scoring component
(`scorePapersBat.js`), the new chat component (`chatNewRAG.js`),
and the server-side scoring worker (`scoring_jobs.py`'s
`_chroma_query`).

The new chat UI surfaces this concretely with two controls:

- A *Paper* dropdown (filterable by id or title) that constrains
  retrieval to one paper or leaves it corpus-wide.
- Two checkboxes (*include references*, *include card*) — both off
  by default.

The same retrieval call also returns the chunks themselves, which
are rendered in a collapsible *Retrieved chunks (N)* panel next to
each assistant answer — full provenance, page numbers, section
tags, raw text.

### 2.7 Generation (prompt construction)

**Legacy chat** uses LangChain's `ConversationalRetrievalQAChain`,
which:

1. Calls the LLM once to rewrite the user question + memory into a
   standalone query.
2. Embeds that, retrieves top-$k$.
3. Calls the LLM again with a standard `Use the following pieces of
   context to answer the user's question.` template.

The intermediate question-rewriting call doubles latency for any
multi-turn exchange and is opaque to the user.

**NewRAG chat** (`chatNewRAG.js`) does one LLM call per turn:

```
You are an expert assistant answering questions about academic papers using the provided context.
Rules:
- Answer ONLY from the context excerpts below. If the context is insufficient, say so plainly — do not invent facts.
- When you cite, include the chunk reference in brackets (e.g. [P:001 §method p.4]).
- Be concise. Quote the paper verbatim where it strengthens the answer.

Conversation so far:
{chat_history}

Question: {question}

Context excerpts:
----
[chunk 1 | P:… §… p.…]
{retrieved text}
...
----

Answer:
```

The retrieved chunks are placed verbatim, the history is appended
as a single string, and the citation format is explicit
(`[P:001 §method p.4]`). One LLM round-trip per turn; the user sees
exactly what the LLM saw.

**NewRAG scoring** uses a deliberately stricter template asking
for **JSON only** with three keys:

```
You are scoring an academic paper against a single criterion.
Return ONLY a strict JSON object with keys:
  score (integer in {scale}),
  justification (1-2 sentences),
  evidence (a direct short quote from the context that supports the
            score, or "").
Criterion: {criterion}
Context excerpts from the paper (treat as the only evidence available):
---
{context}
---
JSON:
```

Combined with Ollama's `format: "json"` decoder constraint, this
yields a parse-rate above 95 % on modern judges and gives every
score a paired evidence quotation — required for the faithfulness
audit in `PAPER_METHODOLOGY.md` §7.5.

---

## 3. Cross-cutting improvements

### 3.1 Idempotency

Legacy `Chroma.fromTexts` / `.fromDocuments` generates **random UUIDs**
as chunk IDs. Running ingestion twice over the same corpus produces
duplicate vectors with no way to detect them after the fact.

NewRAG derives **deterministic, content-addressable** chunk IDs:

```python
sha1(chunk_text)[:10]                # short content hash
id = f"{paper_id}-{section}-{chunk_index}-{sha1[:10]}"
```

Before writing a chunk, the worker checks with one Chroma `get`
whether its `id` is already present and skips it. Net effect:
re-running ingestion over an unchanged corpus is **approximately
free** (cost: one round-trip per chunk to verify existence);
re-running over a partially-changed corpus only re-embeds the
changed chunks. This is what makes the multi-LLM experimental
protocol cheap — one ingestion produces the data source for *all*
subsequent judge runs.

### 3.2 Failure resilience

Legacy: a single Ollama timeout, a single oversize chunk, a single
malformed PDF page — any of these throws, the React component shows
"upload failed", the partial state in Chroma is whatever was written
before the throw, and the user starts over.

NewRAG isolates failures at three levels:

1. **Per-chunk in ingestion.** `embed_text` retries up to 3 times
   with exponential backoff on timeouts, connection errors, and
   5xx responses. On a context-overflow error (the embedding
   model's max-sequence-length, not Ollama's `num_ctx`), the text
   is halved and re-tried — up to three halvings. A chunk that
   still fails after all of that is recorded as `failed_chunks`,
   surfaced in the manifest with the underlying error string, and
   the *paper continues*. Only if every chunk fails does the
   paper get marked `embed_error`. Net effect: a paper with one
   bad page produces 51 good chunks instead of 0.
2. **Per-cell in scoring.** The same wrapping protects each
   `_score_one_cell` call. A failing cell records `score=null`
   with the error in `justification` and the loop continues.
3. **Per-job at the worker level.** The whole `_run_job` body is
   wrapped in a top-level try/except that captures any uncaught
   exception into the job's `error` and `error_traceback` fields,
   so the React side sees the cause instead of the thread silently
   dying.

The dual benefit: the operator gets actionable error messages, and
no transient failure aborts an hour of LLM work.

### 3.3 Sanitisation against silent embedding failures

Several real PDFs in the test corpus produce strings containing
null bytes, lone-surrogate code points, and control characters.
Ollama's `/api/embeddings` accepts these but returns an empty
embedding silently. Legacy code stores the (wrong) empty result and
proceeds — every query against that chunk returns spurious matches.

NewRAG's `_sanitize_for_embedding` strips:
- `\x00` (null bytes)
- code points in the lone-surrogate range `[0xD800, 0xE000)`
- non-newline, non-tab control characters (`< 0x20` except `\n`,
  `\t`)

before any embed call. Adds a deterministic ~5 µs per chunk; removes
a class of silent failures that are nearly impossible to diagnose
downstream.

### 3.4 ChromaDB integration

Legacy uses LangChain JS's `Chroma` wrapper, which itself uses
`chromadb-js`. NewRAG bypasses both and talks to the ChromaDB v2
HTTP API directly. Rationale documented in
`CHANGELOG.md` §6.1: the language SDK emits a `configuration` JSON
field whose `_type` key is rejected by the server when client and
server versions disagree (`KeyError('_type')`). The error surfaces
not only on `create_collection` but also on `get`, which makes
existing collections unreachable until they are dropped.

By talking HTTP directly:

- Omitting the `configuration` field forces the server to use its
  built-in default — no schema mismatch is possible.
- A custom `ChromaHTTP` class normalises the multiple response
  shapes across chromadb 0.4 / 0.5 / 1.x.
- The bridge gains a `/delete_collection` endpoint for recovering
  collections corrupted by other clients.

This decouples NewRAG entirely from the chromadb language-SDK
version space.

### 3.5 Audit trail

The legacy stack records *nothing* outside Chroma itself. A scoring
result is whatever the user copies out of the browser before they
close the tab.

NewRAG anchors every commitment on a private Antelope (Spring)
blockchain through the `sscore` smart contract:

- `startrun` — commits the experimental setup hashes (criteria,
  parameters, corpus, embedding model) *before* any score is
  produced, signed by the named human analyst (`k1_recover`).
- `logcell` — one transaction per scored `(paper, criterion)`
  cell, carrying the SHA-256 hash of the canonical payload and an
  optional `mongo_oid` pointing at the full payload in MongoDB.
- `sealrun` — closes the run by committing a SHA-256 Merkle root
  over the run's cell hashes, again analyst-signed.
- `logimport` — anchors each corpus ingestion event with a
  hash-chain link to the previous ingestion of the same
  collection.
- `verifycell` — anyone can later push this action with a
  candidate `payload_hash` and a Merkle path; the contract walks
  the path with `sha256` and prints `VERIFIED|run_id=…|leaf
  authentic` iff inclusion holds.

The methodology paper (`PAPER_METHODOLOGY.md` §4.5–4.11) goes into
the cryptographic detail. The practical effect for a reader of the
follow-up paper: every score in the published CSV can be
independently verified on chain by anyone — no trust in the
authors' database is required.

### 3.6 Job-based scoring

The legacy stack has no concept of a *job*. A scoring run is the
browser executing a for-loop; if the tab closes, the run ends; if
it crashes, the run ends; if the user wants to come back tomorrow,
they have to start over.

NewRAG's scoring is a first-class server-side job, persistent in
MongoDB and orchestrated by a daemon thread (`scoring_jobs.py`).
Lifecycle:

| state | meaning | thread alive | UI controls |
|---|---|---|---|
| `queued` | created, thread spinning up | yes (briefly) | (none) |
| `running` | actively scoring | yes | Pause · Cancel · View |
| `paused` | pause flag set, thread sleeping | yes | Resume · Cancel · View |
| `interrupted` | thread died (worker crash / Mongo flap) | no | Resume (fresh thread) · Cancel · View |
| `completed` | every cell scored + Merkle-sealed | no | Download CSV · Delete |
| `cancelled` | operator stopped it | no | Delete |
| `error` | uncaught exception in worker | no | View traceback · Resume · Delete |

Pause and resume are real (no busy-loop); resume **skips
already-saved cells** by querying `sscore_cells` for that
`(job_id, paper_id, criterion_id)` triple, so a 4 000-cell run
that's paused mid-way and resumed an hour later picks up at cell
$k+1$ without re-scoring cells $1 \dots k$. On worker startup,
`mark_orphans_interrupted()` sweeps any job stuck in `running` or
`paused` to `interrupted` (the previous worker process exited
mid-flight; the operator must explicitly resume), and every
`/jobs` poll re-checks thread liveness in the new process and
auto-corrects zombies — the same job state can survive a worker
crash, a Mongo flap, or a browser close.

### 3.7 Configuration durability

Legacy: configuration lives in `localStorage` keys
(`selectedOllama`, `selectedChromaDB`, `selectedLLMModel`,
`chatTempreture`, retriever settings) — fine for a single-user
machine but lost when a colleague opens the URL.

NewRAG keeps the same localStorage keys (so existing users see no
disruption) but adds three durable persistence layers:

- The Chroma collection's metadata (`embed_model`, `hnsw:space`).
- MongoDB collections `sscore_jobs`, `sscore_cells`, `sscore_runs`,
  `sscore_imports`, `sscore_analysts` — the canonical state of
  every run and ingestion.
- The Antelope chain (commitments and signatures).

A scoring CSV downloaded today and a CSV downloaded six months
from now over the same run's `job_id` are byte-identical, modulo
ordering.

---

## 4. Comparative tables (compact)

### 4.1 Per-stage feature matrix

Columns mark whether the feature is **active by default** for that
collection type — most §6 enrichments are profile-flagged and
inactive on `academic_paper` (rationale: §6.10).

| feature | legacy | NewRAG (paper) | NewRAG (novel) | NewRAG (manual) |
|---|:-:|:-:|:-:|:-:|
| Server-side ingestion | ✗ | ✓ | ✓ | ✓ |
| pdfplumber + pypdf fallback | ✗ | ✓ | ✓ | ✓ |
| Header/footer stripping | ✗ | ✓ | ✓ | ✓ |
| De-hyphenation | ✗ | ✓ | ✓ | ✓ |
| Section detection (IMRaD / drop-cap / hierarchical) | ✗ | ✓ IMRaD | ✓ chapter+drop-cap | ✓ chapter+sub-section |
| Title / DOI / arXiv extraction | ✗ | ✓ | ✓ title only | ✓ title+version |
| Title-extractor drop-cap repair (`_normalize_for_title`) | ✗ | ✓ | ✓ | ✓ |
| Two-tier (card + body) chunks | ✗ | ✓ | ✓ | ✓ |
| Reference chunks tagged | ✗ | ✓ | ✓ | ✓ |
| Per-chunk `paper_id` metadata | ✗ | ✓ | ✓ | ✓ |
| Per-chunk page numbers | ✗ | ✓ | ✓ | ✓ |
| Content-hashed chunk IDs | ✗ | ✓ | ✓ | ✓ |
| Idempotent re-ingestion | ✗ | ✓ | ✓ | ✓ |
| Separate embedding model field | ✗ | ✓ | ✓ | ✓ |
| Embed-model recorded on collection | ✗ | ✓ | ✓ | ✓ |
| Embed-model mismatch guard | ✗ | ✓ | ✓ | ✓ |
| Profile recorded on collection + mismatch guard | ✗ | ✓ | ✓ | ✓ |
| Per-paper retrieval filter | ✗ | ✓ | ✓ | ✓ |
| Section-filtered retrieval (profile-defaulted) | ✗ | ✓ | ✓ | ✓ |
| Card-gate cheap relevance pre-check | ✗ | ✓ | ✓ | ✓ |
| `content_type` metadata (prose/code/procedure/…) | ✗ | ✓ | ✓ | ✓ |
| NER entities (`ent_person`, `ent_loc`, `ent_org`, `ent_gpe`) | ✗ | ✗ *(noisy on papers)* | ✓ | ✗ |
| Atomic chunking for procedures + code | ✗ | ✗ *(no patterns)* | ✗ *(no patterns)* | ✓ |
| LLM-generated synopses | ✗ | ✗ *(card serves this)* | ✓ (chapter+part) | ✓ (chapter) |
| Hierarchy-aware synopsis spans | ✗ | n/a | ✓ | ✓ |
| Coreference resolution at ingest | ✗ | ✗ *(rare pronouns)* | ✓ | ✗ |
| First-person attribution (protagonist substitution) | ✗ | ✗ *(n/a)* | ✓ *(if `protagonist_name` set)* | ✗ |
| Read-time coref hint in chat system prompt | ✗ | ✗ *(dormant)* | ✓ | ✗ |
| Multi-hop entity-walk chips in chat | ✗ | ✗ *(dormant)* | ✓ | ✗ |
| Stop-button granularity (per-chunk vs per-PDF) | ✗ | ✓ | ✓ | ✓ |
| Four-tier progress bars (files / chunks / synopsis / coref) | ✗ | files+chunks only | ✓ all four | files+chunks+synopsis |
| Strict-JSON LLM scoring | ✗ | ✓ | ✓ | ✓ |
| Per-cell failure isolation | ✗ | ✓ | ✓ | ✓ |
| Embedding retry + auto-truncate | ✗ | ✓ | ✓ | ✓ |
| Direct Chroma HTTP (no SDK trap) | ✗ | ✓ | ✓ | ✓ |
| MongoDB-resident full payloads | ✗ | ✓ | ✓ | ✓ |
| Blockchain commitments + Merkle root | ✗ | ✓ | ✓ | ✓ |
| Analyst-signed run boundaries | ✗ | ✓ | ✓ | ✓ |
| Pause / Resume / Cancel scoring | ✗ | ✓ | ✓ | ✓ |
| Survives browser close / worker crash | ✗ | ✓ | ✓ | ✓ |
| Auto-recovery of zombie jobs | ✗ | ✓ | ✓ | ✓ |
| Retrieved-chunks panel in chat | ✗ | ✓ | ✓ | ✓ |

### 4.2 Failure-mode matrix

| failure | legacy outcome | NewRAG outcome |
|---|---|---|
| One PDF with rasterised text | silently empty in vector store | `skipped_no_text` in manifest with reason |
| One chunk over embed-model token limit | whole document fails | chunk auto-truncated; if still over, recorded as failed; document continues |
| Ollama times out on a chunk | exception bubbles to UI; ingestion aborted | retry with exponential backoff; if exhausted, recorded as failed_chunk |
| ChromaDB v2 `_type` schema mismatch | UI shows opaque KeyError | bridge talks HTTP directly; no schema sent; works |
| Browser tab closed mid-ingestion | progress lost | server-side ingestion continues |
| Browser tab closed mid-scoring | progress lost | server-side job continues |
| Worker process killed | n/a | jobs marked `interrupted` on next start; operator resumes |
| Mongo unreachable mid-job | n/a | endpoints return 503; UI shows degraded banner; jobs marked interrupted by zombie sweep on recovery |
| Embedding model swapped under existing collection | silent garbage retrieval | refused at ingestion; warned at scoring; never silent |
| User re-ingests same corpus | duplicates everything | no-op (content-hash IDs) |
| Same `(paper, criterion)` scored twice | duplicate cells, duplicate chain trx | second attempt skipped (Mongo cell lookup) |

### 4.3 Quantitative impact (565-PDF blockchain-voting corpus)

| metric | legacy | NewRAG | factor |
|---|---:|---:|---:|
| Wall-clock ingestion of full corpus | ~∞ (browser OOM) | ~14 min | n/a |
| Re-ingestion (no changes) | ~∞ | ~30 s (existence checks only) | n/a |
| Vector count | ~21 K (estimated) | ~30 K (more chunks; card+section split) | 1.4× |
| % of chunks usable for section-targeted criteria | 0 | 88 | n/a |
| First-cell LLM calls per criterion across full corpus | 565 | 565 (cards) → ~170 (after gate) | 0.3× |
| Number of follow-on body-LLM calls | 565 | ~170 (after card-gate rejection) | 0.3× |
| Chain transactions per run | 0 | $N \cdot M + 2$ | n/a |
| MongoDB documents per run | 0 | $N \cdot M + 1$ | n/a |
| Audit-trail provenance | none | chain trx + Merkle proof per cell | n/a |
| Crash recovery cost (4 000-cell run, 50 % through, worker restart) | full re-run (~80 min) | resume from cell 2001 (~40 min) | 0.5× |

---

## 5. What carried over unchanged

Not everything in legacy was wrong — three components were
deliberately preserved:

- **ChromaDB as the vector backend.** The metadata-filtering
  capabilities Chroma exposes via its `where` clause are what make
  per-paper isolation tractable; nothing motivated swapping for a
  different store.
- **Ollama as the LLM / embedding host.** Same HTTP surface, same
  model tags, same conventions. Switching judges in a NewRAG
  experiment is *the same operator action* as switching LLMs in
  the legacy chat (set `selectedLLMModel`).
- **`localStorage` for app-wide settings.** The eight keys the
  legacy stack uses (`selectedOllama`, `selectedChromaDB`,
  `selectedLLMModel`, `chatTempreture`, …) all keep their meaning
  in NewRAG, so the legacy components continue to function and
  share state with the new ones.

The legacy `dbFromPDF.js`, `dbFromText.js`, `dbFromWEB.js` and
`chatFromDB.js` components remain in the codebase and remain
functional. Users who want to ingest a single ad-hoc PDF and ask
quick questions about it still have the legacy path; users who
want to score a corpus, audit a study, or do multi-judge
experiments use NewRAG.

---

## 6. Corpus generalisation: from academic-paper-only to profile-driven

NewRAG as initially shipped (sections §1–§5 above) assumed the
corpus was a set of academic papers — every regex, every card-chunk
recipe, every metadata field encoded that assumption. The
second-round work — Phases 2, 3, 5h, 5j, 5k of the generalisation
roadmap in `RAG_GENERALIZATION.md` — refactors those assumptions
behind a `CorpusProfile` abstraction and adds five ingestion-time
enrichments, each profile-gated. **Existing academic-paper
collections are byte-identical to pre-generalisation NewRAG**;
nothing in this round was a breaking change for the original
corpus type. §6.10 makes that explicit.

### 6.1 The `CorpusProfile` abstraction

Every corpus-type-specific choice — section regexes, card recipe,
document-id rule, retrieval exclusions, chunking defaults, plus
the new enrichment flags below — is now encapsulated in a
`CorpusProfile` dataclass and registered by name
(`newRAG/corpus_profiles.py`). The registry currently ships three:

| profile | section regexes | card recipe | default chunk/overlap | excluded from retrieval |
|---|---|---|---:|---|
| **academic_paper** | IMRaD (abstract, introduction, method, results, discussion, references, …) | title + abstract + keywords | 800 / 120 | `references` |
| **novel** | prologue / part / chapter / interlude / epilogue / appendix / glossary, **plus** drop-cap chapter detector | title + prologue opener (or first chapter) | 900 / 200 | *(none — appendices and glossaries are primary sources for "who is X?")* |
| **manual** | preface / overview / introduction / chapter (incl. `CHAPTER\nONE\nTITLE` three-line headers) / `\d+\.\d+` subsection / procedure / appendix / glossary / index / references | title + version + first overview/intro | 1 200 / 120 | `references` |

The profile is **per-collection**, enforced at ingest time and
inspected at retrieval time. A collection ingested with `novel`
cannot be re-ingested with `manual` against the same name — the
embed-model guard from §2.5 is mirrored for profiles in
`get_collection_for_embed`. The chat component reads the profile
from `/collection_info` so the section-exclude defaults adapt
automatically per collection.

### 6.2 Content-type classification — Phase 2b (all profiles)

Every chunk's metadata now carries a `content_type` tag derived from
a cheap regex classifier (`classify_content_type` in
`ingest_corpus.py`): one of `prose / dialog / code / procedure /
list / table_text`. Cost is sub-millisecond per chunk; the
classifier never raises. Used by no profile-specific logic in this
round — purely a metadata enrichment that downstream consumers
(retrieval filters, chat UI, future per-type prompt templates) can
filter on. **On by default for all three profiles**, including
`academic_paper`, because the field is broadly useful (e.g. *"find
code blocks in this paper"*) and the classifier is free.

### 6.3 Named-entity extraction — Phase 2c (novel-only by default)

Optional spaCy NER pass at ingestion. When `extract_entities=True`,
each chunk's metadata gains `ent_person`, `ent_loc`, `ent_org`,
`ent_gpe` fields (comma-joined per kind — Chroma metadata disallows
lists). spaCy or the `en_core_web_sm` model missing → silently
noop, no failure. **Enabled only on the `novel` profile**, where
character and place queries are the dominant retrieval pattern.
Disabled on `academic_paper` by design (would mostly surface
affiliations as `ORG`, adding retrieval noise) and on `manual`
(technical writing names nouns explicitly; NER adds nothing).

### 6.4 Atomic chunking for procedures + code — Phase 3 (manual-only)

The naive sliding splitter happily cuts a twelve-step numbered
procedure or a 200-line code block in half. A pre-pass
(`split_chunks_with_atomic`) detects regex-defined *atomic*
regions and emits each as a single chunk, capped at the
profile's `atomic_block_ceiling` (default 4 000 chars). The
**manual** profile defines three patterns: numbered-step
procedures (≥3 consecutive `\d+\.\s` lines), fenced code
blocks (` ``` `), and 4-space-indented code (≥3 lines).
`academic_paper` and `novel` define no atomic regions, so the
splitter short-circuits and their behaviour is unchanged.

### 6.5 LLM-generated synopses — Phase 5k (novel + manual)

For each detected section whose name is in
`profile.synopsize_sections`, the worker calls the local LLM to
produce a 4–6 sentence synopsis and emits it as an extra
`section: 'synopsis'` chunk, embedded alongside the natural body
chunks. Two design notes worth recording:

- **Idempotent on a hash of (source text + profile prompt)**, not
  the synopsis text. Re-ingest skips LLM calls whose target id
  already exists in the collection — re-ingesting a 30-chapter
  novel after a tiny tweak does not re-pay 30 LLM calls.
- **Span end is hierarchy-aware**: a chapter's synopsis input is
  the chapter PLUS all its sub-sections, not just the 40-char
  heading. Important on tutorials like the Python docs where the
  raw `chapter` span computed by `detect_sections` ends at the
  next `\d+\.\d+` sub-section heading — without the hierarchy
  fix in `build_synopsis_records`, every chapter would feed the
  LLM only its title.

Applicability:

- **academic_paper**: disabled. The existing card chunk
  (title + abstract + keywords, §2.2) is the synopsis equivalent.
- **novel**: synopsizes `chapter / part / interlude / prologue /
  epilogue` — typically ~30–50 LLM calls per book.
- **manual**: synopsizes `chapter` only — sub-sections are
  individually too short to be worth a per-section LLM call.

### 6.6 Coreference resolution — Phase 5h (novel-only)

The biggest quality lever for narrative corpora and by far the
most expensive enrichment. When `coref=True`, each body chunk is
passed to the LLM for pronoun-to-name rewriting, using the
previous chunk's trailing text as antecedent context. The
**rewritten text is what gets embedded**; the **original prose is
what gets stored** as the Chroma document — so display + LLM-reader
citation stay verbatim, while retrieval ranks on the resolved
semantics. Pre-pass: chunks with fewer than three third-person
pronouns are skipped, saving 30–50 % of calls on a typical novel.

Cost dominates the run: one LLM call per surviving chunk → about
2 000 calls per 500-page novel → 3–6 hours on a single consumer
GPU (V100 with `mistral:latest`). The chunk-id format gains a
`-coref-p<sha6>` suffix so toggling `coref` on/off — or changing
the protagonist name (§6.7) — invalidates the embed cache instead
of silently reusing stale vectors.

Applicability:

- **academic_paper**: disabled. Papers don't have pronoun chains
  — *"we proposed X"* names X already; *"it"* rarely crosses
  sentences; the card chunk already disambiguates.
- **manual**: disabled. Technical writing actively avoids
  pronoun ambiguity by convention.
- **novel**: enabled.

### 6.7 First-person attribution + read-time interpretation hint

A wrinkle surfaced in testing on a first-person novel: third-person
coref does **not** resolve *"I / me / my"* to the protagonist's
name, so a query like *"who is X's silent slave?"* fails to bridge
to the embedded text *"my slave Anu"*. Two complementary fixes:

1. **Ingest-time, embed-side.** An optional `protagonist_name` opt
   extends the same coref prompt with a clause instructing the LLM
   to substitute first-person pronouns **outside direct quoted
   dialogue** with the named protagonist. Dialogue lines like
   *"Anu said, 'I cannot.'"* are preserved (the speaker is Anu,
   not the protagonist). The protagonist name is hashed into the
   chunk-id suffix so swapping it invalidates the cache.
2. **Query-time, read-side.** When retrieved chunks carry a
   `coref_protagonist` metadata field, the chat's system prompt
   is augmented (`buildCorefHint` in `chatNewRAG.js`) with a
   note instructing the LLM-reader to interpret first-person
   pronouns in narrative passages as the named protagonist while
   preserving them inside dialogue. Zero re-ingest — pure prompt
   augmentation, conditional on collection metadata.

Both are required for first-person novels: (1) makes the *right*
chunks rank high; (2) gives the reader the bridge from natural
prose to the protagonist's name at answer time. Either alone
leaves a visible reasoning gap. Neither applies to academic_paper
or manual.

### 6.8 Multi-hop entity walks in chat — Phase 5j (novel)

For collections with `ent_*` metadata, the chat panel renders the
top-12 mentioned entities under each assistant message as
clickable chips, colour-coded by kind (`person / location / org /
gpe`). Clicking a chip re-queries the collection with
`whereDocument: {$contains: <name>}` — substring filter on the raw
chunk text, matching what the LLM would actually read. The walked
answer carries its own chips, so the user can chain hops manually
(BFS-style lineage walk). No backend cost — retrieval reuses the
existing Chroma client.

Chips appear only when at least one retrieved chunk has an
`ent_*` field set. **In practice this means novel collections
only** — the academic_paper and manual profiles default
`extract_entities=False`, so chips never appear and the feature
is dormant.

### 6.9 Operational fixes that surfaced with long ingestion runs

Two minor but visible changes triggered by ingestion runs going
from seconds-per-document (academic) to minutes-per-document
(novel + manual):

- **Stop button granularity.** Previously checked only between
  PDFs; now polled inside the per-chunk embed loop and the
  per-chunk coref loop. Worst-case latency dropped from
  "remainder of the current PDF" (minutes on a 600-page novel)
  to "current chunk" (seconds).
- **Multi-tier progress card.** The ingestion UI now renders up
  to **four** bars: files, chunks-of-current-file, synopsis
  (one tick per chapter LLM call), and coref (one tick per chunk
  LLM call). The coref bar is rendered in orange as a visual cue
  that the slow phase is active. Synopsis-bar tick rate is also
  the basis for the user noticing prep delays — for a 600-page
  novel with spaCy NER enabled, `synopsis_total` is set only
  after ~30 s of section-detection + NER work, so the bar appears
  later than the file/chunk bars.
- **`_normalize_for_title()` helper.** A shared title-extraction
  pre-processor used by all three profiles fixes two PDF
  artefacts: (a) drop-cap line splits (`"J\nohn"` → `"John"`)
  and (b) decorative-letter Unicode (`"Å"` U+00C5 → `"A"` via
  NFKD-decompose-then-strip-combining-marks). Applied only during
  title extraction; chunk content keeps the original glyphs.
- **Section-dedup window.** `detect_sections` previously
  collapsed all consecutive same-name matches; now only matches
  within 500 chars are collapsed. Required for novels where ~48
  drop-cap chapter starts must each define their own span;
  academic-paper IMRaD detection is unaffected because IMRaD
  sections each appear once with no near-adjacent duplicates.

### 6.10 What this round means for the academic-paper corpus

By design the `academic_paper` profile inherits the new
infrastructure but activates only the broad-utility enrichments —
every other new feature defaults to off because the cost/benefit
doesn't justify it for academic prose:

| feature | academic_paper | rationale |
|---|:-:|---|
| `content_type` metadata | ✓ | cheap, broadly useful (e.g. *"find code blocks in this paper"*) |
| `_normalize_for_title` drop-cap repair | ✓ | applies to any PDF with stylised typography |
| Section-dedup window relaxation | ✓ | same behaviour; IMRaD sections still each appear once |
| Profile-aware retrieval-default filters | ✓ | `references` exclusion now sourced from profile metadata, not hard-coded |
| NER entities (`ent_person`, …) | ✗ | would mostly tag author affiliations as ORG; noisy |
| Atomic procedure/code chunking | ✗ *(n/a)* | no `atomic_block_patterns` defined; code-block detection could be added in a future round |
| LLM synopses | ✗ | the existing `card` chunk already serves the per-paper synopsis role |
| Coreference resolution | ✗ | papers name subjects explicitly; pronoun chains rare |
| First-person attribution | ✗ | academic prose uses *"we"* / *"the authors"* which are already named in metadata |
| Multi-hop entity-walk chips in chat | ✗ *(n/a)* | dormant without `ent_*` metadata |
| Read-time coref hint | ✗ *(n/a)* | dormant without `coref_protagonist` metadata |

**The existing 565-PDF blockchain-voting collection is unaffected**
— chunk IDs, the `where`-filter semantics, and the section-exclude
default (`references`) are byte-identical to pre-generalisation
NewRAG. A *re-ingest* would add `content_type` to chunks (purely
additive metadata) and benefit from the title-extraction repair on
any PDFs with drop-cap title pages; nothing else changes for
academic-paper users. No re-ingest is required for the next
multi-judge scoring run.

---

## 7. Implications for the follow-up paper

The shift from legacy to NewRAG is what makes the next paper
*possible*. Without:

- **Per-paper retrieval filter** → no defensible inter-paper
  comparison, only inter-corpus.
- **Per-cell hashes + Merkle root** → no chain-of-custody claim
  beyond "trust the authors' CSV".
- **Analyst signatures on chain** → no human accountability beyond
  metadata.
- **Embed-model collection guard** → no replicable retrieval; a
  reviewer cannot reproduce the same vectors.
- **Strict-JSON judge prompt** → no parse-rate above ~60 % on
  smaller models; agreement metrics degrade.
- **Server-side jobs** → no multi-LLM experimental design at
  4 000-cell scale.

Each of these is a methodological prerequisite, not a software
convenience. The legacy stack supports neither the experimental
design nor the audit-trail claim that the paper's Sections 5–7
describe (see `PAPER_METHODOLOGY.md`).

The paper can therefore frame the contribution as:

> *"We extend conventional RAG with paper-level metadata
> filtering, two-tier chunking with section awareness,
> hash-anchored per-cell receipts, and analyst-signed run
> boundaries — converting a chunk-bag into an auditable,
> reproducible scoring substrate suitable for multi-judge
> evaluation studies."*

Each of those clauses maps onto one section of this document
(§2.4, §2.2 / §2.3, §3.5, §3.5 again).

---

## 8. Evaluation plan for a RAG-architecture paper

`PAPER_METHODOLOGY.md` covers the *scoring* paper — what the
NewRAG substrate enables for multi-judge methodology studies.
A separate paper could be written about the **architecture itself**:
profile-driven, structure-aware RAG with per-corpus enrichments.
The hooks below propose what it would take to defend that paper's
claims experimentally, organised the way a reviewer would read it.

### 8.1 The claim that drives the evaluation

The strongest single-sentence framing of an architecture paper:

> *"Profile-gated ingestion-time enrichments (content-type
> classification, NER, atomic chunking, hierarchical synopses, and
> coreference resolution) deliver measurable retrieval and answer
> quality gains across heterogeneous corpus types, at engineering
> costs that scale predictably with corpus size."*

Three sub-claims fall out of that, each independently testable:

1. **Retrieval-quality claim.** The embed-time changes (atomic
   chunking, NER-aware metadata, coref) lift standard IR metrics
   on profile-appropriate queries.
2. **Answer-quality claim.** The read-time hooks (synopsis
   chunks, the §6.7 first-person interpretation hint, multi-hop
   entity walks) lift answer correctness *beyond* what retrieval
   gains alone explain.
3. **Generalisation claim.** The profile abstraction makes
   adding a new corpus type a measurably small effort, and the
   off-by-default decisions in §6.10 are defensible (turning a
   feature on where the profile says off would actively hurt).

### 8.2 Test corpora — at least 3 per profile

Single-corpus evaluation is the chronic weakness of systems
papers. The minimum to make any of the three claims generalise:

| profile | corpora needed | candidate sources |
|---|---|---|
| `academic_paper` | 3 | the existing 565-PDF blockchain-voting corpus + one ML/NLP corpus from arXiv + one biomedical corpus (PMC) — diversity protects against single-domain artefacts |
| `novel` | 3 | one first-person novel (e.g. *Robinson Crusoe* or *Jane Eyre*, public domain) + one third-person classic (e.g. *Pride and Prejudice*) + one contemporary novel — first-person vs third-person is itself a finding |
| `manual` | 3 | Python Tutorial + Pro Git + one hardware/device manual — procedure density varies dramatically across these |

Reusing the existing 565-paper collection costs zero re-ingest
(it's already on disk + chain-committed) and gives the academic
sub-claim a *grounded baseline corpus* the paper can cite.

### 8.3 Metrics, grouped by what they measure

#### 8.3.1 Retrieval quality

Standard IR metrics over a labelled `(question → relevant-chunk-id)`
dataset built once per corpus (50–200 questions each is sufficient
for ablations):

- **nDCG@10**, **Recall@10**, **Recall@100**, **MRR**
- **Per-feature ablations** — turn off one profile flag at a time
  (`extract_entities`, `atomic_block_patterns`, `coref`,
  `protagonist_name`) and re-measure on the same corpus; the
  delta is the feature's contribution
- **Pronoun-heavy query subset** specifically for the coref
  claim — questions where the gold-relevant chunk uses only
  pronouns to refer to the subject; without coref these should
  drop, with coref they should recover

#### 8.3.2 Answer quality

The read-time stack (synopsis surfacing, the §6.7 interpretation
hint, walked-context augmentation) must be evaluated separately
from retrieval, because retrieval gains can be wasted by a weak
reader:

- **RAGAS** metrics: faithfulness, answer relevance, context
  precision, context recall
- **Hedging rate** — fraction of answers containing
  *"not mentioned"* / *"unclear"* / *"cannot determine"*. Directly
  captures the LLM-reader bridge failure observed on first-person
  novels (chunks retrieved but reader unwilling to commit because
  natural prose still says *"my slave John"* rather than the
  protagonist's name)
- **LLM-as-judge** with at least two readers (e.g. `mistral:latest`
  + `llama-3.1-70b`); inter-judge agreement (Cohen's κ) reported
  *separately* — the validity of LLM-judge is itself a finding,
  not an assumption
- **Human-annotated anchor set** of ~100 Q/A pairs per corpus —
  small enough to be affordable, large enough to ground the
  LLM-judge numbers against ground truth

#### 8.3.3 Cost and operational metrics

The engineering claim ("scales predictably") needs concrete
numbers:

- **LLM calls per book / per chapter / per chunk**, broken down
  by enrichment phase (synopsis vs coref). Already instrumented
  in `status["synopsis_total"]` / `status["coref_total"]`
- **Wall-clock per profile per page** on a reference GPU
  (V100; report alongside an industrial-GPU multiplier for
  external relevance)
- **Storage overhead per chunk** with vs without each enrichment
  — important because `coref` adds metadata fields,
  `extract_entities` adds 4 `ent_*` fields, etc.
- **Re-ingest idempotency** — % of chunks that hit the existence
  cache after a same-config re-ingest. Should be ≈100 % by
  design (`process_paper` `existing` set); deviations are bugs
- **Embedding-token consumption** for the embed-side coref
  rewrite (resolved text is typically longer than the original)

#### 8.3.4 Generalisation claim

Two concrete experiments make this falsifiable instead of
hand-waved:

- **Time-and-LOC-to-add-a-fourth-profile.** Pick a corpus type
  the current code doesn't ship (e.g. *legal opinions* with
  numbered paragraphs, or *transcripts* with speaker turns).
  Implement it. Report LOC delta and engineering hours. A claim
  of "~80 LOC per profile" (the figure in `RAG_GENERALIZATION.md`
  §5) is only credible after a fourth, blind-to-the-architecture
  profile lands cleanly
- **Cross-profile leakage** — enable each "off by default"
  flag on the wrong profile (NER on `academic_paper`, coref on
  `manual`) and report the metric deltas. If the off-by-default
  decision in §6.10 is correct, enabling them should *hurt*.
  A null result on this experiment is the most defensible way
  to justify the design decisions to a sceptical reviewer

### 8.4 Baselines

A systems paper without baselines reads as advocacy. Three are
strictly necessary:

| baseline | what it isolates |
|---|---|
| **Vanilla `RecursiveCharacterTextSplitter` + flat top-K** | the value of structured chunking + metadata filtering |
| **LangChain stock RAG** (no `where` filter, no card chunks) | the value of the per-paper filter |
| **`text-embedding-3-large` + flat retrieval** (commercial reference) | a strong embedding baseline; protects against reviewer attribution to local-embedder weakness |

For the §6 enrichments specifically: each profile-flag ablation
*is* a baseline within the same architecture, so the matrix is
"profile-feature ON vs OFF" rather than "NewRAG vs other system".

### 8.5 Standard frameworks worth reusing

Reinventing evaluation harnesses is a paper-quality risk. Three
frameworks already model what this paper wants to measure:

- **BEIR** — zero-shot IR evaluation protocol for the academic
  sub-claim; gives a battery of metrics and a corpus-conversion
  format that reviewers will recognise instantly
- **NarrativeQA** / **NovelQA** / **BookQA** — long-form
  question answering benchmarks for the novel sub-claim; the
  underlying tasks (pronoun resolution, multi-hop entity
  tracking) are exactly what §6.3 / §6.6 / §6.7 target
- **RAGAS** — RAG-specific metric library; faithfulness + context
  precision + context recall in one library, with the
  LLM-as-judge prompt scaffolding already worked out
- **ARES** — academic RAG-evaluation framework with LLM-judge
  validation built in; useful as the prior-art citation when
  justifying the metric choices

### 8.6 Limitations to flag preemptively

A paper that names its limitations before the reviewer does is
harder to reject. The honest list as of this round:

- **Single LLM-reader at test time conflates retrieval and
  reader.** The first-person-novel debugging session in §6.7 is
  the documented example — embed-time coref improved retrieval,
  but the reader (Mistral 7B) still hedged until the read-time
  system-prompt hint was added. Run at least two readers,
  ideally three of different size classes, before claiming an
  end-to-end win
- **Small N per profile.** Three corpora is the minimum, not
  "enough". The paper should claim *"consistent improvement
  across our sample"*, not *"universally generalisable"*
- **Coref evaluation is bounded by the rewriter LLM.** With a
  Mistral 7B rewriter, the resolution is only as good as Mistral's
  pronoun-tracking; a stronger rewriter (Llama 3.1 70B) would
  yield different numbers. Report both, or report rewriter
  sensitivity as a separate experiment
- **Pre-pass threshold is hand-tuned** (3 third-person pronouns,
  §6.6). A sweep across {1, 2, 3, 5, 10} on one corpus would
  show whether 3 is anywhere near optimal
- **Cost numbers are consumer-hardware-specific** (V100, single
  GPU, single-process Ollama). Multi-GPU + batched generation
  would change the wall-clock numbers by ~10× but not the
  *per-LLM-call* ratios
- **The audit-trail / blockchain claim is methodologically
  orthogonal** to the retrieval claims and should not be folded
  into the same paper. Keep that for `PAPER_METHODOLOGY.md`'s
  scope; cite it from this paper rather than repeat it

### 8.7 What the paper does *not* need to do

Stating boundaries up front prevents scope creep:

- **No new embedding model.** The paper claims architectural
  gains on top of an arbitrary off-the-shelf embedder, not a
  new embedder
- **No new LLM reader.** Same logic — `mistral:latest` and any
  comparable 7-13 B local model is the reader
- **No claim of optimality for the prompt templates** (synopsis
  prompt, coref prompt, system-prompt hint). Report the prompts
  used and that they were not tuned beyond initial sanity-check;
  prompt-tuning is a different paper
- **No claim of generalisation beyond corpus types tested.**
  Three profiles is the experimental scope; "transcripts"
  / "legal opinions" / "memoirs" remain conjecturally addressable

---

## 9. Pointers into the implementation

| feature in this document | source file:function |
|---|---|
| **Sections §1–§5 (original NewRAG)** | |
| Section regexes (now profile-defined) | `newRAG/corpus_profiles.py` per-profile `section_patterns` |
| Header/footer rule | `newRAG/ingest_corpus.py` `remove_repeating_headers` |
| Card chunk construction (now profile-defined) | `newRAG/corpus_profiles.py` per-profile `build_card` |
| Per-paper retrieval filter (scoring) | `newRAG/scoring_jobs.py` `_score_one_cell` |
| Per-paper retrieval filter (chat) | `src/component/chatNewRAG.js` `buildFilter` |
| Embed-model collection guard | `newRAG/ingest_corpus.py` `get_collection_for_embed` |
| Profile collection guard | `newRAG/ingest_corpus.py` `get_collection_for_embed` (same fn; rejects profile mismatch) |
| Direct-HTTP Chroma client | `newRAG/ingest_corpus.py` class `ChromaHTTP` |
| Content-hashed chunk IDs | `newRAG/ingest_corpus.py` `process_paper` (id construction) |
| Embedding retry + truncation | `newRAG/ingest_corpus.py` `embed_text` |
| Strict-JSON scoring prompt | `newRAG/scoring_jobs.py` `SCORING_PROMPT_TPL` |
| Server-side scoring job | `newRAG/scoring_jobs.py` `_run_job_inner` |
| Pause / resume / cancel state machine | `newRAG/scoring_jobs.py` (state constants + endpoints) |
| Zombie reconciliation | `newRAG/scoring_jobs.py` `_reconcile_zombies_inplace` |
| Chain bridge | `newRAG/chain_bridge.py` |
| Smart contract | `blockchain/sscore.cpp` |
| **Section §6 (corpus generalisation, this round)** | |
| `CorpusProfile` dataclass + registry | `newRAG/corpus_profiles.py` class `CorpusProfile`, `register`, `get_profile`, `list_profiles` |
| `/profiles` endpoint (UI metadata feed) | `newRAG/ingest_corpus.py` `profiles()` |
| Title-extraction repair (drop-cap + NFKD) | `newRAG/corpus_profiles.py` `_normalize_for_title` |
| Section-dedup window relaxation | `newRAG/ingest_corpus.py` `detect_sections` (`dedup_window = 500`) |
| Content-type classifier | `newRAG/ingest_corpus.py` `classify_content_type` |
| spaCy NER pass | `newRAG/ingest_corpus.py` `_load_spacy`, `extract_entities` |
| Atomic chunking pre-pass | `newRAG/ingest_corpus.py` `split_chunks_with_atomic` |
| LLM synopsis pass (hierarchy-aware spans) | `newRAG/ingest_corpus.py` `build_synopsis_records`, `ollama_generate` |
| Coreference resolution helper | `newRAG/ingest_corpus.py` `count_third_person_pronouns`, `ollama_resolve_pronouns`, `_build_protagonist_clause`, `run_coref_pass` |
| Stop-button per-chunk poll | `newRAG/ingest_corpus.py` `process_paper` (loop with `status["stop_requested"]`) |
| Four-tier progress bars | `src/component/dbFromCorpusPapers.js` (`status.chunk_done` / `synopsis_done` / `coref_done`) |
| Profile picker + LLM-model + protagonist fields in UI | `src/component/dbFromCorpusPapers.js` |
| Read-time coref hint (system-prompt augmentation) | `src/component/chatNewRAG.js` `buildCorefHint` |
| Multi-hop entity walks (chips + walk handler) | `src/component/chatNewRAG.js` `buildEntityFrequencies`, `walkOnEntity` |
| **Reference docs** | |
| User-facing how-to | `newRAG/EXPLANATION.md` |
| Publication-grade methodology | `newRAG/PAPER_METHODOLOGY.md` |
| Generalisation roadmap (incl. cost analyses) | `newRAG/RAG_GENERALIZATION.md` |
| Full change history + rationale | `newRAG/CHANGELOG.md` |

---

## 10. One-paragraph elevator summary (for a paper abstract or intro)

> NewRAG augments a conventional dense-retrieval RAG pipeline with
> three architectural shifts: (i) structured, hash-addressed chunk
> metadata that captures paper identity, section, and pagination —
> turning the corpus from an undifferentiated chunk-bag into a
> queryable graph and enabling per-paper-isolated retrieval via
> standard vector-store metadata filtering; (ii) a server-side
> orchestration layer with persistent, idempotent ingestion and
> pause-resume-able scoring jobs that survive browser closure and
> worker restart; and (iii) an on-chain audit trail that commits
> per-cell SHA-256 payload hashes, a per-run SHA-256 Merkle root,
> and analyst signatures verified via `eosio::recover_key`,
> producing a scoring substrate whose outputs are independently
> verifiable by any reviewer with the published CSV and the chain
> endpoint. The combination is what makes a multi-judge,
> reproducibility-claiming methodology paper realistic on
> corpora at the 500–10 000-paper scale.
