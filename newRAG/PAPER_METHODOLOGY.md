# RAG-Based Multi-LLM Relevance Scoring of Screened Scientific Papers

**A methodology specification for the experimental section of the
follow-up paper to the authors' prior work on LLM-based paper screening.**

---

## Abstract

We describe a retrieval-augmented, LLM-as-judge methodology for scoring
the relevance of scientific papers against a fixed set of criteria. The
pipeline ingests a corpus of PDFs into a vector store with paper-level
metadata, retrieves per-paper evidence chunks through a metadata-filtered
similarity search, and asks a language model to return a strict-JSON
score, justification, and supporting quotation. The system is designed
for *factorial multi-LLM experiments*: the same vector store and the same
criteria JSON are queried by an arbitrary set of judge LLMs, producing
directly comparable score matrices. We describe the algorithm,
implementation, experimental protocol, the result schema, and the
statistical procedures appropriate for interpreting agreement,
calibration and faithfulness across judges.

---

## 1. Motivation

The upstream LLM-screening step (the authors' prior work) yields a
shortlist of candidate papers per research question. The natural next
question — *"how well does each shortlisted paper actually satisfy each
criterion?"* — has no native answer in the screening step, which is
binary (include / exclude) and operates over titles/abstracts only.

Three properties make this an awkward fit for a generic chatbot-over-corpus
setup:

1. **The corpus is topically homogeneous.** All shortlisted papers
   already passed the screen, so they cluster tightly in embedding space.
   A flat top-$k$ similarity search over the whole corpus routinely
   mixes chunks from different papers when scoring a single paper.
2. **Scoring requires per-paper isolation.** Retrieval for paper $p$
   must return *only* chunks from $p$; otherwise the LLM judges a
   composite document that the corpus authors never wrote.
3. **Criteria differ in their evidence locus.** "Topical fit" reads
   from the abstract; "evaluation rigour" reads from the Experiments
   section; "novelty" reads from Related Work. Chunking and retrieval
   must preserve enough structure for the LLM to be steered toward the
   relevant region.

This document specifies a methodology that addresses all three.

---

## 2. Related work (sketch)

- **Retrieval-augmented generation (RAG)** [Lewis et al. 2020,
  Izacard & Grave 2021] decouples knowledge from parameters by injecting
  retrieved evidence into the prompt at inference time.
- **LLM-as-judge** [Zheng et al. 2023, "MT-Bench"; Liu et al. 2023,
  "G-Eval"] uses a language model to score outputs of other systems.
  Reported pathologies include positional bias, verbosity bias,
  self-enhancement and miscalibration.
- **Automated systematic-review tooling** (e.g. ASReview, Rayyan)
  historically operates on titles and abstracts; full-text relevance
  scoring with LLMs is recent and still under-studied.

Compared to prior LLM-as-judge work, the present method (i) restricts the
judged context to *one paper at a time* through metadata filtering, (ii)
requires a strict-JSON output with a textual evidence quotation to enable
faithfulness audits, and (iii) is designed as a factorial protocol where
the *judge model is a study variable*.

---

## 3. Algorithm

### 3.1 Pipeline overview

The pipeline has two off-line stages, run once each, and one on-line
stage, run per experimental condition (per judge LLM, per criteria set).

```
            ┌──────────────┐    ┌──────────────────┐    ┌──────────────┐
PDFs ─────► │  3.2 Ingest  │ ─► │  Chroma vector   │ ─► │ 3.3 Scoring  │ ─► CSV
            │              │    │  store           │    │              │
            └──────────────┘    └──────────────────┘    └──────────────┘
                                       ▲                       ▲
                                       │                       │
                          (one collection per                  │
                           embedding model)             criteria JSON,
                                                        judge LLM tag
```

### 3.2 Corpus ingestion

Input: a directory of PDFs, one file per paper, named
`NNN - <free-form>.pdf` where `NNN` is a stable paper identifier
(assigned during the screening step).

Output: one Chroma collection whose chunks carry the metadata necessary
for per-paper isolated retrieval (§3.3).

#### 3.2.1 Text extraction and de-noising

For each PDF $P$ with pages $p_1, \dots, p_n$ we attempt:

1. `pdfplumber.extract_text` page-by-page; fall back to `pypdf` if
   total extracted character count $< 200$.
2. **Repeating-header/footer removal.** Let $L_i$ be the set of the
   first two and last two non-empty lines of page $p_i$. For a line
   $\ell$, let $c(\ell) = |\{i : \ell \in L_i\}|$. Any $\ell$ with
   $3 < |\ell| < 120$ and $c(\ell) \geq \max(2, \lfloor 0.4 n \rfloor)$
   is treated as boilerplate and removed from every page. Empirically
   this strips journal mastheads, page numbers and copyright lines
   without touching body text.
3. **De-hyphenation.** A trailing `-\n` followed by lowercase is joined.
4. **Whitespace normalisation.** Repeated horizontal whitespace
   collapses to one space; runs of $\geq 3$ newlines collapse to two.

The result is a flat string $T_P$ together with an offset table mapping
character ranges back to page numbers.

#### 3.2.2 Structural parsing

We apply nine MULTILINE/IGNORECASE regexes to $T_P$ to detect the start
positions of canonical IMRaD sections — *abstract*, *keywords*,
*introduction* / *background*, *related work*, *method(s)* /
*proposed system* / *implementation*, *results* / *evaluation*
/ *experiments*, *discussion*, *conclusion(s)* / *future work*,
*references* / *bibliography*, *appendix* / *acknowledg(e)ments*.
After deduplicating consecutive matches we obtain a sequence of
non-overlapping spans

$$
\Sigma_P = \{(s_k, e_k, \text{name}_k)\}_{k=1}^K,\quad s_1 = 0,\ e_K = |T_P|.
$$

Title is extracted from the first non-trivial line of page $p_1$. DOI
(regex `10\.\d{4,9}/...`) and arXiv ID (regex `arXiv[:\s]*\d{4}\.\d{4,5}`)
are searched within the first 5 000 characters.

#### 3.2.3 Two-tier chunking

Two kinds of chunks are produced for paper $P$:

**Card chunk** (one per paper). Concatenation of `Title`, `DOI`, `arXiv`,
*abstract*, *keywords*, hard-capped at $B_{\text{card}} = 1500$
characters (~380 BPE tokens; below the 512-token max-sequence-length of
common embedding models such as `mxbai-embed-large`, `bge-large`,
`snowflake-arctic-embed`). Budget split: 75 % abstract, 25 % keywords
after fixed-text fields. Section tag: `card`.

**Body chunks.** Each detected section is split with a sliding window
of $w = 800$ characters and overlap $o = 120$, never crossing section
boundaries. Splits prefer the nearest whitespace in the right 40 % of
each window. References-section chunks are *retained* but tagged
`section = "references"`, so they remain searchable for citation-style
queries but can be excluded from criterion-scoring retrieval via a
metadata filter.

#### 3.2.4 Metadata, identifiers, embedding, persistence

Each chunk $c$ is assigned the metadata in **Table 1**, embedded by the
selected Ollama model, and upserted into Chroma with the stable ID

$$
\text{id}(c) = \langle \text{paper\_id} \rangle \texttt{-} \langle \text{section} \rangle \texttt{-} \langle \text{chunk\_index} \rangle \texttt{-} \langle \text{sha1}(text)[:10] \rangle.
$$

The hash makes ingestion idempotent: re-running the worker over the
same corpus skips chunks already present.

**Table 1 — per-chunk metadata.**

| key | example | purpose |
|---|---|---|
| `paper_id` | `"001"` | per-paper retrieval filter |
| `filename` | `001 - 2509.22965v2.pdf` | traceability |
| `title` | extracted | display |
| `doi`, `arxiv_id` | optional | citation linking |
| `section` | one of {`card`, `abstract`, `intro`, `related`, `method`, `results`, `discussion`, `conclusion`, `references`, `appendix`, `body`} | criterion-targeted retrieval |
| `page_from`, `page_to` | 4, 5 | provenance |
| `chunk_index`, `total_chunks` | 7, 42 | ordering / debugging |
| `pages_total` | 12 | context |
| `content_hash` | sha1 | idempotency |
| `ingest_run` | ISO timestamp | run lineage |

Collection-level metadata records `hnsw:space = "cosine"` and the
`embed_model` used at creation time. A subsequent run that attempts to
write the collection with a different embedding model is **refused at
the worker level**; this prevents the silent retrieval corruption that
results from mixing embedding spaces in one collection.

### 3.3 Per-paper relevance scoring

Inputs:

- a vector store collection $V$ (from §3.2),
- a paper subset $\mathcal P = \{P_1, \dots, P_N\}$,
- a criteria array $\mathcal C = \{c_1, \dots, c_M\}$ where each
  $c = (\text{id}, \text{question}, w, \text{scale})$,
- a judge LLM $L$ served by Ollama in JSON mode,
- hyperparameters $k$ (top-K), $\tau$ (LLM temperature),
  $\theta$ (card-gate threshold), $\gamma \in \{0, 1\}$ (card-gate on/off).

Output: a matrix $S \in \mathbb R^{N \times M}$ of integer scores
(with `null` for parse failures), together with per-cell `justification`
and `evidence` strings.

The procedure for one cell $(P, c)$ is shown in **Algorithm 1**.

```
Algorithm 1   Score(paper P, criterion c)
─────────────────────────────────────────
1.  if γ then                                       // optional card-gate
2.      D_card  ← V.search(c.question, k=1,
                            filter={paper_id=P, section="card"})
3.      r_card  ← LLM(prompt(c.question, D_card, c.scale))
4.      if r_card.score < θ then return r_card with source="card"
5.  D_body ← V.search(c.question, k=k,
                       filter={paper_id=P,
                               section NOT IN {"references", "card"}})
6.  if D_body = ∅ then
7.      D_body ← V.search(c.question, k=k, filter={paper_id=P})
8.  r ← LLM(prompt(c.question, D_body, c.scale))
9.  return r with source="body"
```

The prompt template is exactly:

```
You are scoring an academic paper against a single criterion.
Return ONLY a strict JSON object with keys:
  score (integer in {scale}),
  justification (1–2 sentences),
  evidence (a direct short quote from the context that supports the
            score, or "").

Criterion: {c.question}

Context excerpts from the paper (treat as the only evidence available):
---
[chunk 1 | section={σ} | p.{π}]
{retrieved text}
...
---
JSON:
```

LLM responses are parsed by extracting the first `{...}` block and
calling `JSON.parse`. Parse failures yield `score = null` but retain
the raw text for diagnostics.

#### 3.3.1 Aggregation

Per paper $P$:

$$
\bar s_P \;=\; \frac{\sum_{c \in \mathcal C} w_c \cdot s_{P,c} \cdot \mathbb 1[s_{P,c} \in \mathbb Z]}{\sum_{c \in \mathcal C} w_c \cdot \mathbb 1[s_{P,c} \in \mathbb Z]},
$$

i.e. the criterion-weighted mean over cells that parsed. Setting
$w_c = 0$ keeps the cell in the table but excludes it from
$\bar s_P$ — useful for diagnostic criteria (e.g. "is this a survey?").

---

## 4. Implementation

### 4.1 System architecture

The system is delivered as three loosely-coupled processes:

- **Ollama** (`http://…:11434`) — local LLM/embedding server.
- **ChromaDB** (`http://…:8000`) — vector store, v2 HTTP API.
- **Ingestion worker** (this work) — Python/Flask, default port
  `8010`. Exposes `POST /start`, `GET /status`, `POST /stop`,
  `GET /papers`, `GET /collection_info`, `POST /delete_collection`,
  `GET /list_dir`.

A single React UI (in the prior-work codebase) drives both stages.

### 4.2 Vector store: direct HTTP, no language SDK

The chromadb Python client emits a `configuration` JSON whose `_type`
key the Chroma server rejects when client and server versions are
misaligned, producing `KeyError('_type')` on otherwise valid requests
and persistently corrupting affected collections. The implementation
**bypasses the chromadb language SDK entirely** and talks to the v2
HTTP API directly, omitting the `configuration` field on create so the
server falls back to its own defaults (the `CollectionConfigurationInternal()`
branch in `process_create_collection`). This makes the ingestion
component immune to version drift between the JS chromadb client used
by the existing application and the Python environment of the worker.
A `POST /delete_collection` escape hatch is provided for recovering
collections corrupted by other clients.

### 4.3 Embedding via Ollama

The worker POSTs each chunk to Ollama's `/api/embeddings`. Two
robustness measures apply:

- **Sanitisation.** Null bytes, lone surrogates and other non-printable
  control characters are stripped (newline and tab preserved). These
  occasionally cause Ollama's embedding endpoint to return empty
  results.
- **Context-overflow truncation.** Embedding models cap at their
  max-sequence-length (typically 512 tokens) regardless of Ollama's
  `num_ctx` (which only affects chat LLMs). Responses matching the
  patterns `context length`, `input length`, `exceeds`, `too long`,
  `max_seq_length`, or `maximum sequence` trigger automatic halving
  of the input text and a re-attempt, up to three halvings.

Failed chunks are recorded per-chunk; a paper is marked `partial`
(rather than `embed_error`) when at least one chunk succeeded.

### 4.4 Why this is fast to re-ingest

Idempotency via content hashes means re-ingesting the same corpus
under the same embedding model is approximately free — only changed
or new chunks are re-embedded. This is critical for the experimental
protocol below: one ingestion produces the data source for *all*
subsequent judge-LLM runs.

### 4.5 Cryptographic audit trail — overview

Every score produced by the pipeline is committed to a private
Antelope (Spring) blockchain through a dedicated smart contract
`sscore`, with full payloads persisted to MongoDB and only their
hashes anchored on chain. Each commitment carries an independent
cryptographic signature from the named human analyst, recovered
on-chain via `eosio::recover_key`.

The audit trail provides three claims that the scoring methodology
relies on:

- **Existence and ordering.** Every `(paper, criterion, judge LLM,
  scoring parameters)` cell existed in this exact form before block
  $B$ at chain time $T$. Manual post-hoc editing of scores or
  evidence quotations is detectable.
- **Pre-committed experimental setup.** The hashes of the criteria
  JSON, the scoring hyperparameters, and the corpus manifest are
  committed *before* the first cell is scored. Result-shopping by
  retroactively changing criteria or parameters to match a target
  judge is detectable.
- **Analyst accountability.** A run is opened and closed by
  signatures recoverable to a pre-registered analyst pubkey. The
  worker's hot key alone is insufficient to fabricate runs — the
  analyst's private key is required at both boundaries.

The trail does *not* (in v0.2) provide independence guarantees
between judges, blinding between dual screeners, or VRF-verified
sample selection; those are scoped for v0.3–v0.5 (see §10.4).

### 4.6 On-chain data model

The `sscore` contract maintains four multi-index tables, all scoped
to the contract account:

- **`runs[run_id]`** — one row per scoring run. Stores
  `analyst, llm_model, embed_model, collection,
   corpus_hash, criteria_hash, params_hash,
   sample_seed, n_papers, n_criteria,
   started_at, finished_at, rows_root, sealed`.
- **`cells[cell_id]`** — one row per (paper, criterion) cell. Stores
  `run_id, paper_id, criterion_id, score, scale_max,
   payload_hash, mongo_oid, ts`. Secondary index by `payload_hash`
  for $O(\log n)$ lookup of any cell from its hash alone.
- **`imports[id]`** — one row per corpus ingestion event. Stores
  `analyst, collection, embed_model, corpus_hash, n_papers,
   manifest_ref, prev_hash, ts`. `prev_hash` references the
  preceding import for the same collection, giving a tamper-evident
  chain of ingestion provenance.
- **`analysts[account]`** — registry of `(account, public_key,
   active)` triples. The `setanalyst` admin action populates this
  table; `startrun`, `sealrun`, and `logimport` look it up to
  verify embedded analyst signatures.

All on-chain commitments are 32-byte SHA-256 in the native
`checksum256` type. The contract sources and version are in
[`blockchain/sscore.cpp`](../blockchain/sscore.cpp).

### 4.7 Canonical hashing

For each per-cell receipt the worker constructs the **canonical
payload object**:

```
payload = { v, run_id, paper_id, criterion_id, score, scale, source,
            justification, evidence, llm_model, embed_model,
            collection, run_started_at }
```

with the schema-version field $v = 1$. The canonical byte-string is
the JSON serialisation with: keys sorted lexicographically, no
whitespace between tokens, and UTF-8 preserved (i.e. non-ASCII
characters are not escaped to `\uXXXX`). Formally,

$$
\text{payload\_hash}
  \;=\;
  \mathrm{SHA\text{-}256}\bigl(\mathrm{canonicalise}(\text{payload})\bigr).
$$

The same canonicalisation is reproducible by any third party with
the published CSV: every column needed to reconstruct the payload
is present, so a reviewer can independently recompute `payload_hash`
and compare against the on-chain value.

For the setup hashes committed at `startrun` time —
`corpus_hash`, `criteria_hash`, `params_hash` — the same
canonicalisation is applied to (respectively) the corpus manifest
written by the ingestion worker, the criteria JSON loaded into the
scoring UI, and the scoring-hyperparameter object
$\{k, \theta, \gamma, \tau, \text{prompt\_version}\}$ from §3.3.

### 4.8 Per-cell receipts and Merkle inclusion proofs

For each (paper, criterion) cell the worker executes, in order:

1. Build the canonical payload, compute `payload_hash` (§4.7).
2. Insert the full payload into MongoDB collection
   `sscore_cells`; capture the returned `mongo_oid` (24-char
   ObjectId).
3. Push the `logcell(run_id, paper_id, criterion_id, score,
   scale_max, payload_hash, mongo_oid)` action to the chain.

When the run finishes, the worker collects the run's
`payload_hash` values in cell-insertion order and constructs a
**binary SHA-256 Merkle tree**. When a level has an odd number of
leaves the last leaf is duplicated; the tree is then carried up to
a single 32-byte root `rows_root`. The root is committed via
`sealrun(run_id, rows_root, analyst_sig)`.

The on-chain `verifycell(run_id, leaf_hash, path,
path_bits)` action recomputes the Merkle root for a candidate leaf
along the supplied path and compares it to the stored
`rows_root`. `path_bits[i] == 0` denotes "current node is on the
left at level $i$, sibling on the right"; `path_bits[i] == 1`
denotes the reverse. The contract refuses with `merkle path does
not match sealed root` if the recomputed root differs by even one
byte.

This gives the audit constant on-chain storage per run while
admitting $O(\log n)$ inclusion proofs for any of the $n$ cells.

### 4.9 Analyst-signature verification (Track 1)

Each of `startrun`, `sealrun`, and `logimport` takes a trailing
`signature analyst_sig` parameter. The contract verifies it as
follows:

1. **Repack the canonical params.** The action computes
   $\mathbf{b} = \mathrm{pack}\bigl(\mathrm{std::make\_tuple}(\theta_1, \dots, \theta_k)\bigr)$
   where $\theta_1, \dots, \theta_k$ are the action's other
   parameters in declaration order, and `pack` is Antelope's
   standard binary serialiser. The worker side
   ([`chain_bridge.py`](./chain_bridge.py) helper
   `_serialize_action_data`) reproduces the identical byte string.
2. **Digest.** $d = \mathrm{SHA\text{-}256}(\mathbf{b})$.
3. **Recover.** $\hat{K} = \mathrm{recover\_key}(d, \text{analyst\_sig})$
   using Antelope's `recover_key` host function (wraps
   `secp256k1_ecdsa_recover`).
4. **Check registration.** The contract looks up
   `analysts[analyst]`; refuses unless `analyst` is registered,
   `active == true`, and $\hat{K} = \text{registered\_pubkey}$.

For `sealrun`, the contract additionally enforces that the analyst
whose signature it is verifying equals `runs[run_id].analyst`, so a
run cannot be opened by one analyst and closed by another.

The transaction-level authority remains the contract account's
hot key (`sscore@active`), so the worker is on the audit trail too
(it pushed the transaction). The embedded analyst signature is an
*independent* attestation that the human analyst with pubkey
$\hat{K}$ approved this specific commitment. Net guarantee on a
sealed run:

> *The worker pushed all transactions, and the human analyst with
> pubkey $\hat{K}$ signed both run boundaries.*

The analyst's private key may live in any of three places — (a)
hardcoded in the worker (the current batch-mode implementation,
matching the `backEnd.py` pattern), (b) an interactive wallet
(Anchor / `@wharfkit/session`), or (c) a hardware key. The
contract is indifferent; only the recovered pubkey matters. The
React UI in this paper's implementation produces no client-side
signature; mode (b) is documented in §4.9.2 but is deliberately
out of scope (see §10.4 *What's not yet built*).

#### 4.9.1 Analyst identity propagation

The analyst's identity flows through four layers; an inconsistency
at any layer causes the on-chain `_verify_analyst_sig` check to
reject the action. The layers are:

1. **Identity source.** One of:
   - **Anchor-wallet login** (top-nav *AnchorLogin* → `TestWharf.js`),
     which writes the user's account name into
     `localStorage.wharf_user_name` (e.g. `"boni"`).
   - **UI override field** (the *Analyst* input in the scoring
     page), persisted as `localStorage.sscoreAnalyst`.
   - **Hardcoded default** in the scoring component, evaluated only
     when both of the above are empty.
   - Resolution order is *Anchor login → UI override → default*.

2. **Antelope name normalisation.** Before any chain or dict
   lookup, the worker calls `name_safe(raw)` which lowercases,
   folds the digits disallowed by Antelope `name`
   ($\{0,6,7,8,9\} \to \{o,g,s,t,n\}$), replaces remaining
   non-`[.a-z1-5]` characters with `.`, collapses runs of `.`, and
   truncates to 12 chars. The mapping is deterministic and
   recorded per row in MongoDB (`*_raw` and `*_chain`) so reviewers
   can decode the on-chain name back to the original tag.

3. **Worker-side WIF map (`ANALYST_KEYS`).** A Python dict in
   `newRAG/chain_bridge.py` mapping the normalised analyst name to
   a WIF private key. The worker uses this to compute the embedded
   `analyst_sig` for `startrun`, `sealrun`, and `logimport`. The
   dict is *not* read from an environment variable or external
   file — it is hardcoded so that an unattended batch run cannot
   accidentally produce signatures under an unintended identity.

4. **On-chain registry (`analysts` table).** Populated via the
   admin-only `setanalyst` action. Maps the normalised analyst
   name → registered public key + active flag.

For `_verify_analyst_sig` to pass, the pubkey *derived from the
WIF in layer 3* must equal the pubkey *registered in layer 4*, and
both must be addressed by the same normalised name from layers 1+2.

#### 4.9.2 Key-management modes

Three operating modes are supported, with increasing assurance.
The contract is mode-agnostic; the choice affects the audit story
in the paper, not the protocol.

| Mode | Where the WIF lives | Identity separation | Trust assumption | Audit-claim wording |
|---|---|---|---|---|
| **A. Reused contract key** | `ANALYST_KEYS` with the same WIF as `sscore@active` | none — one keypair across contract account and analyst | anyone with `sscore@active` can sign as the analyst | *"someone with admin access committed this run"* |
| **B. Per-analyst hot key** *(recommended for batch runs in a paper)* | `ANALYST_KEYS` with a distinct, freshly-generated WIF for each analyst | analyst keypair separate from contract account | worker host trusted; the analyst's WIF must not leak from the worker | *"the worker pushed all transactions; the analyst's distinct key signed both boundaries"* |
| **C. Anchor / hardware wallet** *(designed, not pursued in this paper)* | held in the user's Anchor wallet or hardware key; never on worker host | analyst keypair separate; private key never copied | only the analyst's device is trusted | *"the analyst's wallet signed each boundary on-device; no analyst private key ever touched the worker"* |

For batch (unattended) scoring — the dominant case for any
multi-judge experimental section — modes A and B are the only
viable options. Publications that lean on the audit-trail claim
should use mode B at minimum; mode A is acceptable for single-user
internal validation but should be disclosed.

#### 4.9.3 Registration workflow

For each analyst $\alpha$ to be authorised, perform the following
once:

1. **Choose a name** $n_\alpha$ satisfying the Antelope `name`
   alphabet (`[.a-z1-5]`, ≤12 chars). If the analyst's natural
   identifier is an Anchor account, that name is usually already
   compliant.
2. **Acquire a keypair:**
   - Mode A: reuse `sscore@active`'s keypair (skip generation).
   - Mode B: `cleos create key --to-console` and save both halves.
   - Mode C: generate inside the wallet/hardware device; export
     the public half only.
3. **Install the WIF on the worker** by editing
   `newRAG/chain_bridge.py`:
   ```python
   ANALYST_KEYS = {
       "α":          "5K…WIF…",
       # …other analysts…
   }
   ```
   Restart the worker. The worker logs the registered analyst
   count on startup if instrumented; `/chain/whoami` can be used
   to confirm the pubkey the worker derives from the WIF.
4. **Register the pubkey on chain.** Either by `cleos`:
   ```bash
   cleos -u <BC_URL> push action sscore setanalyst \
     '["α", "EOS…", true]' -p sscore@active
   ```
   or via the worker's admin endpoint:
   ```bash
   curl -X POST http://127.0.0.1:8010/chain/setanalyst \
     -H 'Content-Type: application/json' \
     -d '{"analyst":"α","pubkey":"EOS…","active":true}'
   ```
5. **Verify on chain:** `cleos … get table sscore sscore analysts`
   must show $\alpha$ with the expected pubkey and `active=true`.
6. **In the React UI**, either log in via *AnchorLogin* as $\alpha$
   (writes `wharf_user_name`) or type $\alpha$ into the *Analyst*
   field on the scoring page (persists `sscoreAnalyst`).
7. **End-to-end smoke test:** run a small (≥1 paper × ≥1 criterion)
   scoring batch. The browser console must show no
   `[chain] … failed` warnings; each cell's *Chain* badge must
   render blue with an 8-character truncated trx-id.

#### 4.9.4 Common failures and their on-chain manifestations

The bridge propagates the chain's error body to the browser
console verbatim. The following matrix maps the diagnostic
message back to its operational cause:

| Failure mode | Bridge HTTP status | Diagnostic message | Remedy |
|---|---|---|---|
| UI sends a name not in `ANALYST_KEYS` | 400 | `no signing key for analyst 'X'` | Add `X` to `ANALYST_KEYS` and restart; or correct the *Analyst* field in the UI. |
| `wharf_user_name` empty *and* no UI override *and* no fallback registered | 400 | `no signing key for analyst 'anonymous'` (i.e. the React default) | Login via Anchor, fill the UI *Analyst* field, or register `anonymous` (not recommended). |
| WIF derives to pubkey different from on-chain registration | 500 from `push_transaction` | `analyst_sig does not match the registered pubkey` | Re-register the pubkey with `setanalyst`, or rotate `ANALYST_KEYS[name]` to the correct WIF. |
| Analyst registered but `active: false` | 500 from `push_transaction` | `analyst is deactivated` | `setanalyst(name, pubkey, true)`. |
| Analyst name unknown to the contract | 500 from `push_transaction` | `analyst is not registered (setanalyst first)` | Run `setanalyst`. |
| `sealrun` signed by a different analyst than the run was opened with | 500 | `analyst_sig does not match the registered pubkey` (resolved against the run-opening analyst) | Sign the seal with the same analyst account that opened the run. |

In v0.2 a successful `chain_trx_id` per cell — and a populated
`rows_root` plus sealed `sealrun` transaction — collectively
demonstrate that every layer of the analyst-identity stack agrees:
otherwise the chain would have rejected one of the three boundary
actions.

### 4.10 Third-party verification procedure

A reviewer with access only to (i) the published CSV, (ii) the
criteria JSON used, and (iii) the chain RPC endpoint can perform
the following independent checks:

1. **Per-cell hash recomputation.** For each CSV row, canonicalise
   the payload (§4.7), SHA-256 it, and look up the resulting hash
   in the `cells` table by the secondary index on `payload_hash`.
   Any post-hoc edit to `score`, `justification`, or `evidence`
   produces a hash that is absent from the chain.
2. **Per-cell inclusion proof.** For any cell, the reviewer pushes
   `verifycell(run_id, payload_hash, path, path_bits)`. The chain
   prints `VERIFIED|run_id=…|leaf authentic` iff the leaf is in the
   sealed tree.
3. **Setup commitment check.** From the CSV's `collection`,
   `run_started_at`, the criteria JSON, and the scoring
   hyperparameters, the reviewer recomputes `corpus_hash`,
   `criteria_hash`, and `params_hash` (§4.7) and compares against
   `runs[run_id]`. Mismatches reveal retroactive changes to the
   experimental setup.
4. **Analyst-signature audit.** From the explorer view of the
   `startrun` and `sealrun` transactions, the reviewer extracts
   the embedded `analyst_sig`, recomputes the digest, and runs
   off-chain `recover_key`. The recovered pubkey must equal
   `analysts[analyst].pubkey` at the block height where the
   transaction was confirmed.
5. **(Optional) Time anchor.** If `rows_root` was anchored to
   Bitcoin via OpenTimestamps (out of scope for v0.2 but
   architecturally supported), the reviewer additionally obtains
   a Bitcoin proof-of-existence at time $T$. The two layers
   compose to a transitively-verifiable statement: *this scoring
   cell existed in this exact form before block height $B_{\text{sscore}}$
   on the Spring chain and before Bitcoin block height
   $B_{\text{btc}}$ at unix time $T$.*

### 4.11 Cryptographic primitives used

| operation | implementation (worker) | implementation (contract) |
|---|---|---|
| payload canonicalisation | `json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")` | n/a (off-chain) |
| SHA-256 | `hashlib.sha256` | `eosio::sha256` host function |
| Antelope binary pack of action params | `_serialize_action_data` in `chain_bridge.py` (per-type encoding for `name, uint*, string, checksum256, signature, public_key`) | `eosio::pack(std::make_tuple(...))` |
| secp256k1 signing | pure-Python signer reused from `pyntelope.utils.sign_bytes` (loaded via `importlib.util` to bypass an unrelated pydantic-v2 incompatibility) | n/a |
| secp256k1 key recovery | n/a | `eosio::recover_key` host function (wraps `secp256k1_ecdsa_recover`) |
| Merkle tree | pairwise SHA-256 with last-leaf duplication, in `merkle_root_sha256` and `merkle_path_sha256` | walked inside `verifycell`, same construction |

No additional Python dependencies beyond the ingestion worker's
existing footprint were introduced (`base58`, `requests`,
`pycryptodome` for RIPEMD-160 in the signer, all already pulled in
by `pyntelope`).

---

## 5. Experimental protocol for multi-LLM scoring

### 5.1 Held-fixed variables

For a study comparing $J$ judge LLMs, the following are held fixed:

- The PDF corpus and its file hashes.
- The ingestion worker version and its parameters
  ($B_{\text{card}}$, $w$, $o$, repeating-header threshold).
- The embedding model and the resulting Chroma collection.
- The criteria JSON (file hash recorded).
- The scoring hyperparameters: $k$, $\tau$, $\theta$, $\gamma$, the
  prompt template.
- Ollama version, system temperature, JSON format flag.

### 5.2 Varied variable

The judge LLM tag (e.g. `gemma3:12b`, `qwen3:14b`, `llama3.3:70b`,
`mistral-nemo`, `deepseek-r1:14b`, `phi4`, `granite3.3:8b`). The UI's
LLM input is changed between runs; everything else stays.

### 5.3 Pre-registration

Before the first run the following are committed to the repository
(and ideally to a public registry, e.g. OSF):

- the criteria JSON,
- the SHA-1 of the corpus directory (the worker writes a manifest with
  per-file results that can be hashed),
- the list of judge LLMs that will be evaluated,
- the analysis plan (§7).

### 5.4 Reproducibility within a run

For a given judge LLM:

- $\tau = 0.1$ for the main results (low but non-zero to avoid pure
  greedy decoding's known pathologies); a $\tau = 0$ replication is
  recommended for sensitivity analysis.
- Ollama format is set to `json`, which constrains the decoder to
  emit valid JSON.
- Each run records `run_started_at` per row (an ISO timestamp), the
  exact `llm_model` tag, the `embed_model` tag, and the `collection`
  name.

### 5.5 Number of runs

Each judge LLM is run at least twice with identical inputs to
quantify *within-judge* variance — non-zero $\tau$ and any stochasticity
in retrieval ties will produce non-deterministic outputs. The minimum
viable design is

$$
J \cdot 2 \cdot N \cdot M \quad\text{LLM calls},
$$

where $J$ is the number of judges, $N$ the number of papers and $M$ the
number of criteria; multiplied by the card-gate factor when applicable.

---

## 6. Results collection

### 6.1 CSV schema (per row = per (paper, criterion))

| column | type | meaning |
|---|---|---|
| `paper_id` | str | from `NNN -` filename prefix |
| `title` | str | extracted from PDF |
| `criterion_id` | str | from criteria JSON |
| `score` | int / null | parsed from LLM JSON |
| `justification` | str | LLM-supplied |
| `evidence` | str | LLM-supplied quotation |
| `source` | enum | `card` / `body` / `none` / `error` |
| `llm_model` | str | judge LLM tag at the row's creation time |
| `embed_model` | str | embedding model of the collection queried |
| `collection` | str | Chroma collection name |
| `run_started_at` | ISO ts | timestamps the run-level batch |
| `chain_run_id` | str (uint64) | `runs[run_id]` primary key on `sscore` |
| `chain_trx_id` | str (64 hex) | `logcell` transaction id; resolvable via the chain explorer / Memento indexer |
| `payload_hash` | str (64 hex) | SHA-256 of canonical row (§4.7), matches `cells[*].payload_hash` |
| `mongo_oid` | str (24 hex) | MongoDB ObjectId of the full payload in `sscore_cells` |

`chain_run_id` is rendered as a string in the CSV (and in JSON
exchanges between the React UI and the worker) because uint64
values routinely exceed JavaScript's `Number.MAX_SAFE_INTEGER`
($2^{53}$); naïve numeric handling silently rounds them and breaks
the chain lookup. The CSV value is the exact decimal representation.

Filenames follow `scores_<collection>_<llm>_<YYYY-MM-DD-HHMM>.csv`,
allowing flat-directory storage of all experimental runs.

### 6.2 Auxiliary artefacts

Alongside each CSV the analyst should preserve:

- the criteria JSON used (SHA-256 of its canonical form must equal
  the `criteria_hash` committed in the corresponding `startrun`
  transaction on chain);
- the ingestion manifest (`newRAG/manifest_<collection>.json`) for
  per-paper ingestion outcomes (status, failed_chunks, error);
- the Ollama `ollama list` and `ollama show <model>` outputs to
  capture exact model digests;
- the chromadb server version;
- the `sscore` contract version and account (e.g. `sscore@active` on
  the Spring chain at `<URL>`) plus the chain id, so a reviewer
  knows which chain to query.

### 6.3 On-chain artefacts (cross-reference §4.5–4.10)

For each run, the following on-chain records are produced:

- one `startrun` transaction (commits setup hashes + analyst sig);
- $N \cdot M$ `logcell` transactions (one per scored cell, streaming);
- one `sealrun` transaction (commits Merkle root + analyst sig);
- one `logimport` transaction *per corpus ingestion* (logged once
  when the collection is first built, then again only if the
  collection is re-ingested — typically not per scoring run).

A reviewer needs only the `sscore` account name, the chain RPC
endpoint, and the published CSV to perform the verification
procedure in §4.10.

### 6.4 Pre-analysis sanity checks

Before any statistic is computed:

1. **JSON-parse rate per LLM.** Cells with `score = null` are rows
   where the LLM violated the strict-JSON instruction. A judge with
   parse-rate < 95 % on a non-trivial corpus should be treated with
   caution — its responses may already be regex-salvaged in the table
   but the underlying instruction-following is unreliable.
2. **Card-gate hit rate.** $|\{(P,c) : \text{source}(P,c) = \text{card}\}|$.
   A very high gate hit-rate means the analyst's threshold $\theta$
   was set too aggressively for this judge; the body pass never ran.
3. **Evidence faithfulness.** For a random sample (e.g. 30 rows per
   judge), verify that the `evidence` quotation appears verbatim in
   the retrieved chunks (which can be recovered by re-issuing the
   filtered search with the same query and $k$). Lift the
   exact-match rate. Any judge with faithfulness < ~80 % is showing
   hallucinated evidence and its scores should be reported with this
   caveat.
4. **Per-criterion score-range usage.** If a judge collapses to a
   single value (e.g. always 3/5) on a criterion, the criterion or
   the judge is uninformative and a different anchored scale is needed
   (see EXPLANATION.md §10.5).
5. **Chain commitment integrity.** For every row, the recomputed
   `payload_hash` (§4.7) must match the on-chain
   `cells[*].payload_hash` for the corresponding `chain_run_id` and
   `chain_trx_id`. Any mismatch indicates the CSV was edited after
   commitment. For an end-to-end check on each run, push
   `verifycell` for one randomly-chosen row per criterion; any
   `merkle path does not match sealed root` response in the response
   trace invalidates the entire run.
6. **Setup-hash check.** The analyst's recomputed `criteria_hash`
   and `params_hash` (from the criteria JSON and hyperparameters
   used) must equal the values stored in `runs[run_id]`. Mismatch
   means either the wrong criteria file was saved or the chain
   commitment was made with different settings than reported.

---

## 7. Interpretation methodology

Let $s^{(j)}_{P,c}$ denote the score from judge $j \in \{1,\dots,J\}$
on paper $P$ for criterion $c$.

### 7.1 Descriptive statistics

For each judge and each criterion, report:

- mean, standard deviation, median, IQR over papers;
- the empirical use of the score range (histogram with $|\text{scale}|$
  bins);
- the parse-failure rate and the card-rejection rate.

### 7.2 Inter-judge agreement

Per criterion, treat each judge as a rater:

- **Pairwise Spearman $\rho_{jk}$** on the score vector across papers.
  Robust to monotone score transformations; the natural measure when
  the analysis target is a *ranking*.
- **Cohen's $\kappa$** with linear or quadratic weights on the
  integer score levels. Captures absolute agreement; sensitive to
  marginal distributions (a judge that always scores 3 reduces $\kappa$
  artificially).
- **Krippendorff's $\alpha$** with an ordinal distance metric for
  $J > 2$ judges and missing data (parse failures, card-rejections).
  Recommended primary metric for the overall agreement table.
- **Intraclass correlation coefficient (ICC(2,1))** for absolute
  agreement under a random-effects model; reported per criterion.

### 7.3 Consensus ranking

The motivating downstream use is a *ranking* of papers. Two consensus
constructions are recommended:

- **Borda count.** For each judge $j$ and criterion $c$, convert scores
  to ranks $r^{(j)}_{P,c}$ (lower = better). Sum across judges and
  weighted across criteria:
  $R_P = \sum_j \sum_c w_c \cdot r^{(j)}_{P,c}$. The consensus order
  is by ascending $R_P$.
- **Mean-of-means.** For each paper, compute its mean weighted score
  per judge $\bar s^{(j)}_P$ as in §3.3.1, then average across judges:
  $\bar{\bar s}_P = \frac{1}{J} \sum_j \bar s^{(j)}_P$. Report this
  with bootstrap CIs.

Disagreement between Borda and mean-of-means rankings is itself
informative — it signals that some judges produce extreme scores while
others compress the range.

### 7.4 Calibration analysis

Construct, for each criterion $c$:

- the per-judge **mean** $m^{(j)}_c$ and **dispersion** $\sigma^{(j)}_c$;
- the **Spearman rank correlation** between each judge's vector and the
  *leave-one-out consensus* across the remaining judges.

Judges with low correlation to the leave-one-out consensus, *especially
when paired with high dispersion*, are likely the outliers driving
disagreement and warrant qualitative inspection.

### 7.5 Faithfulness audit

Beyond the §6.4 random-sample check, a corpus-wide audit is feasible:

1. For each row, re-issue the retrieval (same criterion question,
   same filter, same $k$) and obtain the candidate-evidence text.
2. Compute the longest common substring between the row's `evidence`
   field and the candidate text. Report (per judge) the distribution
   of match length / `len(evidence)`.
3. Flag rows with $< 0.5$ ratio for manual inspection.

This produces a per-judge *faithfulness score* that should be reported
alongside agreement metrics.

**Chain-anchored variant.** Because `evidence`, `justification`, and
`score` are all part of the canonical payload whose SHA-256 is
committed on chain (§4.7), the analyst (or a reviewer) can prove
that the audit was performed on the *committed* row rather than an
edited one: each audited row's `payload_hash` is reproduced from
the CSV and looked up via the `cells` table's `by_hash` secondary
index. This converts the faithfulness audit from a self-reported
metric into one with a chain-anchored chain-of-custody — relevant
when reviewers question whether borderline cases were re-scored
post-hoc.

### 7.6 Statistical tests

When the experimental claim is *"judge $j_1$ produces higher / more
selective scores than judge $j_2$"*:

- Paired Wilcoxon signed-rank on $\{s^{(j_1)}_{P,c} - s^{(j_2)}_{P,c}\}_P$
  for each criterion. Bonferroni-corrected across criteria.

When the claim is *"the consensus ranking is robust to judge choice"*:

- Leave-one-judge-out (LOJO) Kendall $\tau_b$ between full-consensus
  and ablated-consensus rankings, averaged over judges and
  bootstrap-resampled over papers.

---

## 8. Threats to validity

- **LLM-as-judge biases.** Positional, verbosity, self-enhancement and
  recency biases are documented in the literature. The strict-JSON
  output mitigates verbosity bias; the metadata-filtered retrieval
  removes self-reference of the LLM's training data only insofar as
  the LLM treats the prompt's context as authoritative — which it is
  *instructed* but not *guaranteed* to do.
- **Embedding-model semantic coverage.** A dense embedder trained on
  general English may underweight equations, code, table content
  and non-English text. Multi-embedder ablation (run §3.2 with a
  second embedder; build a second collection; re-run §3.3) is a
  recommended robustness check.
- **Section-detection failures.** When IMRaD headers are absent
  (typical for short workshop papers, posters, and some applied
  venues), the entire body collapses to a single `body` section.
  Retrieval still works, but section-targeted criterion design loses
  its value for that paper.
- **Image-only and CID-encoded PDFs.** Approximately 3–5 % of PDFs in
  the test corpus contain only rasterised text or use CID-encoded
  fonts that pdfplumber cannot decode. These are marked
  `skipped_no_text` in the manifest and excluded from scoring; OCR
  fallback is a planned extension.
- **Card-gate threshold sensitivity.** The card-gate is an LLM call
  itself, so its rejection decision inherits the same biases as the
  body-pass scoring. Reporting card-rejection rates per judge (§6.3)
  is essential for interpreting downstream score distributions.
- **Single prompt template.** The methodology fixes one prompt; prompt
  sensitivity is a known LLM-as-judge issue. A small prompt-ablation
  (e.g. with and without the evidence-quotation requirement) on a
  held-out subset is recommended for robustness.

### 8.1 Audit-trail-specific threats

The on-chain audit trail (§4.5–4.10) closes several common failure
modes — silent post-hoc edits to scores, retroactive criteria
changes, fabricated runs — but introduces its own assumptions that
should be acknowledged:

- **Trust in the analyst's private key custody.** The Track 1 sig
  binds a *key* to a run, not a *person*. If the analyst's key is
  shared, stolen, or stored in a world-readable file, the on-chain
  attestation degenerates to "someone holding $\hat K$ signed". For
  batch runs the worker holds the WIF, which is acceptable if the
  worker host is trusted; for higher-assurance future work, mode C
  from §4.9.2 (Anchor / hardware wallet) narrows the trust to the
  analyst's device — designed but not pursued for this paper.
- **Trust in the chain's continued availability and integrity.** The
  Spring (Antelope) chain `sscore` is deployed on must remain
  reachable for verification. Periodic anchoring of `rows_root` to
  a public chain via OpenTimestamps (architecturally supported, not
  in v0.2) reduces this trust to a single Bitcoin block.
- **Trust in the contract code at the time of execution.** A
  reviewer should pin the contract's WASM hash at the block height
  of each transaction; a contract upgrade between commitment and
  audit could alter verification behaviour. This is an
  Antelope-general concern, not unique to `sscore`.
- **Off-chain MongoDB tampering.** The full payloads in
  `sscore_cells` *can* be edited without on-chain notice, but doing
  so produces a `payload_hash` that no longer matches the chain;
  the §6.4 sanity check detects it. The chain anchors only the
  hash, so the MongoDB store is a hot cache, not a trust anchor.
- **Smart contract bugs.** The `_verify_analyst_sig` helper and
  `verifycell` Merkle walk are short and pure (no external
  dependencies); a formal review of `blockchain/sscore.cpp` is
  recommended before a publication that leans heavily on the
  audit-trail claim.

---

## 9. Reproducibility checklist

To make a manuscript using this methodology replicable, the following
must be reported:

1. **Corpus.** Paper IDs, SHA-1 of each PDF, source venue. The
   ingestion manifest is sufficient.
2. **Ingestion.** Embedding model tag and digest, Chroma server
   version, chunk size $w$ and overlap $o$, card budget $B_{\text{card}}$,
   the section regexes (or the worker version), Ollama version.
3. **Criteria JSON.** Verbatim contents and SHA-1.
4. **Judges.** For each judge LLM: Ollama tag, digest from
   `ollama show`, quantisation level, hardware (relevant only for
   timing, not for results).
5. **Scoring hyperparameters.** $k$, $\tau$, $\theta$, $\gamma$, the
   prompt template (verbatim), JSON-format flag, number of runs per
   judge.
6. **Analysis.** Sanity-check rates (parse, card, faithfulness),
   agreement metrics chosen, consensus construction, statistical
   tests with multiple-comparison correction.
7. **Outputs.** All run CSVs (concatenable), the worker manifest, and
   the random-sample faithfulness audit.
8. **Audit-trail provenance.**
   - The Antelope chain name and RPC endpoint (e.g.
     `blockchain2.uni-plovdiv.net:8033`) and chain id (`get_info`).
   - The `sscore` contract account, WASM SHA-256, and ABI
     SHA-256 at the block height of the experiments.
   - The analyst account name(s) and the *publication-time*
     registered pubkey(s) from `analysts`.
   - For each reported run: `chain_run_id` (the uint64 primary key
     in `runs`), the transaction id of the `startrun` and
     `sealrun` actions, the committed `rows_root`, and at least
     one independently-verifiable inclusion proof for a randomly
     chosen cell (e.g. via `verifycell` in a transaction trace).
   - For each ingestion: the `logimport` transaction id and the
     `prev_hash` linkage to the preceding import for the same
     collection.

---

## 10. Implementation references

The implementation accompanying this methodology is open-source and
lives in the same repository under:

- Ingestion worker: `newRAG/ingest_corpus.py`
- UI for ingestion: `src/component/dbFromCorpusPapers.js`
- UI for scoring: `src/component/scorePapersBat.js`
- User-facing documentation: `newRAG/EXPLANATION.md`

Concrete pointers (file:line) into the implementation:

- Section-detection regexes: `ingest_corpus.py` `SECTION_PATTERNS`.
- Header/footer removal: `ingest_corpus.py` `remove_repeating_headers`.
- Card-budget enforcement and split: `ingest_corpus.py` `process_paper`
  (the `CARD_BUDGET = 1500` block).
- Direct-HTTP Chroma client: `ingest_corpus.py` class `ChromaHTTP`.
- Embed-model collection-metadata guard: `ingest_corpus.py`
  `get_collection_for_embed`.
- Card-gate and filtered retrieval: `scorePapersBat.js`
  `scorePaperCriterion`.
- Scoring prompt template: `scorePapersBat.js` `SCORING_PROMPT`.
- Aggregation formula: `scorePapersBat.js` the `aggregated` `useMemo`.
- CSV export with run identity: `scorePapersBat.js` `exportCSV`.

**Audit trail (§4.5–4.10) — contract and bridge:**

- Smart contract: [`blockchain/sscore.cpp`](../blockchain/sscore.cpp)
  v0.2 — tables `runs`, `cells`, `imports`, `analysts`; actions
  `startrun`, `logcell`, `sealrun`, `verifycell`, `logimport`,
  `setanalyst`, `clearrun`. The signature-verification helper is
  `_verify_analyst_sig`; the Merkle walk is the loop in
  `verifycell`.
- Chain bridge: [`newRAG/chain_bridge.py`](./chain_bridge.py) —
  Flask blueprint under `/chain/*`. Key functions: `name_to_uint64`,
  `_serialize_action_data` (per-type ABI binary), `_serialize_signature`
  (66-byte K1 wire format), `_serialize_public_key` (34-byte K1 wire
  format), `_analyst_sign` (server-side analyst signing for batch
  runs), `_push_via_http` (raw `/v1/chain/push_transaction` path,
  used when pyntelope's pydantic v2 import fails),
  `merkle_root_sha256`, `merkle_path_sha256`.
- Build/deploy instructions:
  [`blockchain/README.md`](../blockchain/README.md).
- Detailed design rationale and the open-question decisions that
  shaped the contract scope:
  [`newRAG/BLOCKCHAIN_INTEGRATION_PLAN.md`](./BLOCKCHAIN_INTEGRATION_PLAN.md).
- Per-turn implementation diary, including the bugs found and fixed
  (chromadb `_type` mismatch, JS uint64 rounding of `run_id`,
  cfd-hash being 32 zero bytes vs. `sha256(b"")`):
  [`newRAG/CHANGELOG.md`](./CHANGELOG.md).

---

## 11. Notation summary

| symbol | meaning |
|---|---|
| $P$, $\mathcal P$ | a paper, the set of papers |
| $c$, $\mathcal C$ | a criterion, the criteria set |
| $L$, $j$ | judge LLM (the indexed variable in §7) |
| $V$ | the Chroma collection (vector store) |
| $w$, $o$ | chunk-window and overlap in characters |
| $B_{\text{card}}$ | card-chunk character budget (default 1500) |
| $k$ | top-K body chunks retrieved per (paper, criterion) |
| $\theta$ | card-gate minimum acceptable score |
| $\gamma$ | card-gate on/off indicator |
| $\tau$ | LLM sampling temperature |
| $w_c$ | criterion weight |
| $s_{P,c}$ | integer score in the criterion's scale |
| $\bar s_P$ | per-paper weighted mean (eq. §3.3.1) |
| $\bar{\bar s}_P$ | mean-of-means across judges (§7.3) |
| $\hat K$ | the analyst's public key recovered on-chain from `analyst_sig` (§4.9) |
| $d$ | SHA-256 digest of the canonicalised action params (§4.9) |
| `payload_hash` | SHA-256 of the canonical per-cell payload (§4.7) |
| `rows_root` | SHA-256 Merkle root over a run's `payload_hash` leaves (§4.8) |
| `chain_run_id` | the contract's `runs[]` primary key (uint64), transported as decimal string in CSV/JSON (§6.1) |

---

## 12. References (placeholders for the paper)

- Lewis, P., et al. (2020). *Retrieval-Augmented Generation for
  Knowledge-Intensive NLP Tasks*. NeurIPS.
- Izacard, G., & Grave, E. (2021). *Leveraging Passage Retrieval with
  Generative Models for Open-Domain QA*. EACL.
- Zheng, L., et al. (2023). *Judging LLM-as-a-Judge with MT-Bench and
  Chatbot Arena*. NeurIPS.
- Liu, Y., et al. (2023). *G-Eval: NLG Evaluation using GPT-4 with
  Better Human Alignment*. EMNLP.
- Krippendorff, K. (2004). *Content Analysis: An Introduction to its
  Methodology*. Sage.
- Cohen, J. (1968). *Weighted kappa: Nominal scale agreement with
  provision for scaled disagreement or partial credit*. Psychological
  Bulletin.
- Shrout, P. E., & Fleiss, J. L. (1979). *Intraclass correlations:
  Uses in assessing rater reliability*. Psychological Bulletin.
- ChromaDB documentation, v2 HTTP API
  (https://docs.trychroma.com).

---

*End of methodology specification.*
