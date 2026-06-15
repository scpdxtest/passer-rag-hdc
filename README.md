# PaSSER-RAG-HDC

**Platform for Retrieval-Augmented Generation over a scientific-paper corpus, with on-chain anchoring of scoring results.**

PaSSER-RAG-HDC is the supplementary application accompanying the PaSSER-RAG
scientific paper. It is a focused React single-page app built on top of the
`newRAG` pipeline of the original PaSSER platform, packaging only the
components needed to (1) build a vector store from a corpus of papers,
(2) score papers, (3) chat over the corpus with an LLM via RAG, and
(4) anchor scoring/audit results on an Antelope blockchain.

---

## Features (UI)

| Menu | Route | Component | Purpose |
|------|-------|-----------|---------|
| **About** | `/about` | `About.js` | Project overview & version. |
| **Create Vectorstore → From Paper Corpus** | `/dbfromcorpuspapers` | `dbFromCorpusPapers.js` | Ingest a paper corpus into ChromaDB via the `ingest_corpus.py` worker. |
| **Paper Scoring** | `/scorepapersbat` | `scorePapersBat.js` | Batch-score papers; stream results and anchor them on chain. |
| **Chat → NewRAG (paper corpus chat)** | `/chatnewrag` | `chatNewRAG.js` | Conversational RAG over the corpus (LangChain + Ollama + Chroma). |
| **Configuration → Settings** | `/selectmodel` | `SelectModel.js` | Pick Ollama / Chroma / MultiAgent endpoints and the active model. |
| **Configuration → Add/Remove Model** | `/addmodel` | `addModel.js` | Pull / delete Ollama models. |
| **ManageDB** | `/managedb` | `manageDB.js` | Inspect and manage ChromaDB collections. |
| **AnchorLogin** | `/testwharf` | `TestWharf.js` | Log in with an Anchor wallet (WharfKit) for on-chain actions. |

Shared helpers: `Nav.js`, `mylib.js`, `BCEndpoints.js`, `ErrorBoundry.js`,
`configuration.json`.

---

## Architecture

```
React SPA (this repo, src/)
   │  axios / chromadb-js / langchain
   ▼
newRAG/ Python backend
   ├── ingest_corpus.py      Flask app (:8010) — corpus → ChromaDB worker
   │     ├── scoring_jobs.py   Flask blueprint mounted at /jobs
   │     └── chain_bridge.py   Flask blueprint — mirrors scoring events on chain
   ├── corpus_profiles.py    per-corpus ingest profiles
   ├── preflight.py / debug_launch.py / inspect_collection.py   ops helpers
   ├── experiments/synopsis_span_ablation/   reproducible experiment pipeline (00–09)
   └── tools/                markdown → docx (paper) tooling
   ▼
ChromaDB (vector store)  •  Ollama (LLM + embeddings)  •  Antelope chain  •  MongoDB
```

---

## Quick start

### Frontend

```bash
npm install
npm start          # dev server (uses craco, openssl-legacy-provider)
npm run build      # production build
```

Then open **Configuration → Settings** and set your Ollama / Chroma /
ingest endpoints (defaults live in `src/component/configuration.json`).

> Requires Node 16/18. ChromaDB JS client is pinned to **1.10.4** for
> compatibility with the Python HTTP-API ingest path.

### Backend (`newRAG/`)

```bash
cd newRAG
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env     # then fill in the secrets (see below)
python ingest_corpus.py --port 8010
```

You also need a running **ChromaDB** server and an **Ollama** instance (for
the embedding model and the chat/scoring LLMs). Build a vector store by
pointing the **Create Vectorstore → From Paper Corpus** screen at your corpus,
or run the worker directly with one of the `manifest_*.json` profiles.

---

## Security / secrets

This repository is public. **No private keys are committed.** All Antelope
WIF signing keys are loaded from environment variables at runtime:

| Variable | Used by |
|----------|---------|
| `SSCORE_SIGNING_KEY` | `chain_bridge.py` — sscore@active scoring anchors |
| `SRAUDIT_SIGNING_KEY` | sraudit@active experiment audit (`logaudit`) |
| `ANALYST_KEYS_JSON` | per-analyst WIFs for unattended batch runs |

Copy `.env.example` → `.env` and fill these in locally. `.env` is gitignored.
If any of these keys were ever committed historically, **rotate them**.

Bulk data (the paper corpus, the built vector store `.bin`, large test
fixtures) is intentionally excluded — rebuild the vector store with
`ingest_corpus.py`.

---

## Repository layout

```
.
├── public/                 static assets / index.html
├── src/                    React app
│   ├── App.js, index.js
│   └── component/          UI components + configuration.json
├── newRAG/                 Python backend, experiments, docs
│   ├── *.py                ingest / scoring / chain bridge / ops
│   ├── manifest_*.json     corpus ingest profiles
│   ├── experiments/        reproducible ablation pipeline
│   ├── tools/              md → docx
│   ├── deploy/             systemd unit + deploy notes
│   └── requirements.txt
├── craco.config.js         webpack polyfills (node core → browser)
├── .env.example
└── package.json
```

---

## Provenance

Derived from the PaSSER platform (`scpdxtest/PaSSER`, `scpdxtest/PaSSER-SR`).
This trimmed distribution keeps only the corpus-RAG, paper-scoring, and
on-chain anchoring features for the PaSSER-RAG paper supplement.
