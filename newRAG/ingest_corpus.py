"""
Corpus ingestion worker for the paper-scoring RAG.

- Reads PDFs from a folder (default: ./newRAG/corpus)
- Extracts text section-aware (Abstract / Introduction / Methods / Results /
  Discussion / Conclusion / References / etc.)
- Builds a per-paper "card" chunk = Title + Abstract + Keywords
- Splits body text per-section into smaller chunks (default 800/120)
- Embeds with Ollama (default mxbai-embed-large) and writes to ChromaDB
  in a single collection, with rich metadata so retrieval can be filtered
  to a single paper at query time.

Two ways to run:
  1) CLI:
       python newRAG/ingest_corpus.py --cli \
         --corpus newRAG/corpus \
         --chroma http://127.0.0.1:8000 \
         --ollama http://127.0.0.1:11434 \
         --collection papers_corpus \
         --embed-model mxbai-embed-large

  2) Server (default — used by the React UI):
       python newRAG/ingest_corpus.py
     Then POST /start, GET /status, GET /papers, POST /stop on port 8010.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import pdfplumber
except Exception:
    pdfplumber = None
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None
# NOTE: we intentionally do NOT import the chromadb Python client.
# Recent chromadb versions emit a `configuration` JSON whose `_type` key the
# server rejects (`KeyError('_type')`) when client and server versions disagree
# — and the same key is also referenced when reading existing collections that
# were created by a different client (e.g. the JS chromadb@1.10.4 used by the
# React app). We bypass that by talking to the chromadb v2 HTTP API directly
# and never sending `configuration`, which makes the server fall back to its
# default config (see chromadb.server.fastapi.process_create_collection).


# ---------- Section detection (delegated to corpus_profiles) ----------
#
# The four corpus-specific assumptions (document-id extraction, section
# regexes, card recipe, references-as-noise list) live in
# newRAG/corpus_profiles.py. The /start endpoint accepts a "profile"
# field; the default is "academic_paper", which reproduces v1 NewRAG
# behaviour byte-for-byte. New profiles are added without touching this
# file — see RAG_GENERALIZATION.md.

from corpus_profiles import (
    get_profile as _get_corpus_profile,
    list_profiles as _list_corpus_profiles,
)


def paper_id_from_filename(fname: str) -> str:
    """Back-compat shim — only the academic_paper rule. New code should
    use `profile.document_id_from_filename(fname)`."""
    return _get_corpus_profile("academic_paper").document_id_from_filename(fname)


def extract_pages(pdf_path: str):
    """Return [(page_num, text), ...] using pdfplumber, falling back to pypdf."""
    pages = []
    if pdfplumber is not None:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages, start=1):
                    try:
                        t = page.extract_text() or ""
                    except Exception:
                        t = ""
                    pages.append((i, t))
            if sum(len(t) for _, t in pages) > 200:
                return pages
        except Exception:
            pages = []
    if PdfReader is not None:
        pages = []
        try:
            r = PdfReader(pdf_path)
            for i, p in enumerate(r.pages, start=1):
                try:
                    pages.append((i, p.extract_text() or ""))
                except Exception:
                    pages.append((i, ""))
        except Exception:
            return []
    return pages


def clean_text(t: str) -> str:
    if not t:
        return ""
    t = t.replace("\x00", " ")
    t = re.sub(r"-\n([a-z])", r"\1", t)              # de-hyphenate
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def remove_repeating_headers(pages):
    """Drop lines that repeat verbatim across >=40% of pages (headers/footers)."""
    if len(pages) < 4:
        return pages
    counts = {}
    for _, t in pages:
        for line in (t.splitlines()[:2] + t.splitlines()[-2:]):
            line = line.strip()
            if 3 < len(line) < 120:
                counts[line] = counts.get(line, 0) + 1
    threshold = max(2, int(0.4 * len(pages)))
    boilerplate = {line for line, c in counts.items() if c >= threshold}
    out = []
    for num, t in pages:
        lines = [ln for ln in t.splitlines() if ln.strip() not in boilerplate]
        out.append((num, "\n".join(lines)))
    return out


def detect_sections(full_text: str, profile=None):
    """Return list of (section_name, start_offset, end_offset).

    When `profile` is None, falls back to the academic_paper profile
    (preserving original behaviour for any caller that still passes
    just full_text).

    Dedup policy: consecutive matches of the SAME section name within
    `dedup_window` characters of each other collapse to the first
    (catches a header echoed in the ToC, or a duplicate "Abstract"
    line on the title page + body). Matches further apart stay
    separate — vital for novels where ~48 "chapter" drop-caps must
    each define their own span, not collapse into one giant section."""
    if profile is None:
        profile = _get_corpus_profile("academic_paper")
    matches = []
    for name, rx in profile.compiled_section_patterns():
        for m in rx.finditer(full_text):
            matches.append((m.start(), name))
    matches.sort()
    dedup_window = 500           # chars; tuned for ToC echoes
    deduped = []
    for off, name in matches:
        if deduped and deduped[-1][1] == name and off - deduped[-1][0] <= dedup_window:
            continue
        deduped.append((off, name))
    if not deduped:
        return [("body", 0, len(full_text))]
    spans = []
    for i, (off, name) in enumerate(deduped):
        end = deduped[i + 1][0] if i + 1 < len(deduped) else len(full_text)
        spans.append((name, off, end))
    if spans[0][1] > 0:
        spans.insert(0, ("body", 0, spans[0][1]))
    return spans


def split_chunks(text: str, chunk_size: int, overlap: int):
    text = text.strip()
    if len(text) <= chunk_size:
        return [text] if text else []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            sp = text.rfind(" ", start + int(chunk_size * 0.6), end)
            if sp > start:
                end = sp
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


# `extract_title`, `find_doi`, `find_arxiv` were previously hard-coded
# academic-paper helpers. They now live inside the profile's
# `extract_doc_metadata` callable. The shims below preserve the old
# signatures so any external caller keeps working — internal callers
# should switch to `profile.extract_doc_metadata(full_text)`.

def extract_title(pages):
    if not pages:
        return ""
    full = "\n".join(t or "" for _, t in pages[:2])
    return _get_corpus_profile("academic_paper") \
        .extract_doc_metadata(full).get("title", "")


def find_doi(full_text: str):
    return _get_corpus_profile("academic_paper") \
        .extract_doc_metadata(full_text).get("doi", "")


def find_arxiv(full_text: str):
    return _get_corpus_profile("academic_paper") \
        .extract_doc_metadata(full_text).get("arxiv_id", "")


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


# ---------- Phase 2b: content_type heuristic classifier ----------

# Pre-compiled patterns for the classifier. Order matters: code > procedure
# > list > table > dialog > prose. The first rule to "win" by threshold is
# the assigned type. Thresholds are tuned to avoid false positives on
# normal prose — the cost of a wrong tag is worse than a missing one.
_RX_CODE_FENCE      = re.compile(r"```[a-z]*\n[\s\S]*?\n```")
_RX_CODE_KEYWORDS   = re.compile(
    r"\b(def|function|class|return|import|require|var|let|const|"
    r"public|private|static|void|null|None|True|False|true|false)\b"
)
_RX_CODE_SYMBOLS    = re.compile(r"[{};=<>]|->|=>|::|\+\+|--")
_RX_NUMBERED_STEP   = re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE)
_RX_BULLET          = re.compile(r"^\s*[-*•·]\s+\S", re.MULTILINE)
_RX_TABLE_PIPE      = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_RX_TABLE_SPACES    = re.compile(r"\S+ {3,}\S+ {3,}\S+")  # 3+ columns of whitespace gaps
_RX_DIALOG_OPEN     = re.compile(r"[“”‘’\"']")
_RX_DIALOG_VERBS    = re.compile(
    r"\b(said|asked|replied|whispered|shouted|murmured|answered|"
    r"exclaimed|muttered|cried)\b",
    re.IGNORECASE,
)


def classify_content_type(text: str) -> str:
    """Return a coarse content_type label for `text`.

    Categories: code, procedure, list, table_text, dialog, prose.
    The classifier is intentionally cheap (no model load) and conservative:
    when no rule fires strongly, returns 'prose'. The chunk's section name
    is *not* used here — section is structural; content_type is textual."""
    if not text:
        return "prose"
    t = text.strip()
    if not t:
        return "prose"

    # CODE: fences are a hard signal; otherwise need keywords + symbol density.
    if _RX_CODE_FENCE.search(t):
        return "code"
    n_kw = len(_RX_CODE_KEYWORDS.findall(t))
    n_sym = len(_RX_CODE_SYMBOLS.findall(t))
    if n_kw >= 3 and n_sym >= 5:
        return "code"

    # PROCEDURE: 3+ numbered steps within the chunk.
    if len(_RX_NUMBERED_STEP.findall(t)) >= 3:
        return "procedure"

    # LIST: 3+ bullets — but only if it dominates (avoids tagging a quote-
    # block that happens to contain one bullet).
    n_bullets = len(_RX_BULLET.findall(t))
    if n_bullets >= 3 and n_bullets * 80 >= len(t):  # rough density check
        return "list"

    # TABLE_TEXT: pipe-tables OR repeated multi-column whitespace gaps.
    n_pipe_rows = len(_RX_TABLE_PIPE.findall(t))
    if n_pipe_rows >= 3:
        return "table_text"
    if len(_RX_TABLE_SPACES.findall(t)) >= 4:
        return "table_text"

    # DIALOG: quotation marks AND a dialog verb in close proximity.
    if len(_RX_DIALOG_OPEN.findall(t)) >= 4 and _RX_DIALOG_VERBS.search(t):
        return "dialog"

    return "prose"


# ---------- Phase 2c: optional spaCy-based NER ----------

# spaCy is heavy and optional. The model object is cached on the run-state
# dict so it's loaded at most once per ingestion job. Any failure (missing
# package, missing en_core_web_sm) flips `ner_available=False` and we
# silently return [] from extract_entities for the rest of the run.

def _load_spacy(state):
    """Lazily load spaCy + en_core_web_sm; cache on the state dict.

    Returns the nlp object, or None if unavailable. Never raises."""
    if "ner_nlp" in state:
        return state["ner_nlp"]
    nlp = None
    try:
        import importlib
        spacy = importlib.import_module("spacy")
        try:
            nlp = spacy.load("en_core_web_sm",
                             disable=["parser", "tagger", "lemmatizer"])
        except Exception:
            # Model not downloaded — try the blank pipeline with the
            # default NER component as a last-ditch fallback. Usually this
            # also fails, in which case we simply disable NER.
            nlp = None
    except Exception:
        nlp = None
    state["ner_nlp"] = nlp
    return nlp


def extract_entities(text: str, state, kinds=None, max_per_kind=10) -> dict:
    """Run NER and return {kind: [unique_strings_capped]} or {}.

    Cheap when disabled — returns {} immediately if spaCy is not loadable.
    `kinds` filters which labels to keep (default PERSON/LOC/ORG/GPE)."""
    if not text:
        return {}
    nlp = _load_spacy(state)
    if nlp is None:
        return {}
    wanted = set(kinds or ["PERSON", "LOC", "ORG", "GPE"])
    try:
        doc = nlp(text[:20000])     # spaCy slows quadratically past ~20k chars
    except Exception:
        return {}
    bucket = {}
    for ent in doc.ents:
        if ent.label_ not in wanted:
            continue
        s = ent.text.strip()
        if not s or len(s) > 80:
            continue
        bucket.setdefault(ent.label_, [])
        if s not in bucket[ent.label_]:
            bucket[ent.label_].append(s)
    # Cap and order — keeps metadata bounded for Chroma's small-value limits.
    out = {}
    for k, vs in bucket.items():
        if vs:
            out[k] = vs[:max_per_kind]
    return out


# ---------- Phase 3a: atomic-block-aware chunking ----------

def split_chunks_with_atomic(text, chunk_size, overlap,
                             atomic_patterns, atomic_ceiling):
    """Like split_chunks(), but preserves atomic regions as single chunks
    when they fit under `atomic_ceiling` characters.

    `atomic_patterns` is a list of (name, compiled_regex). Regions match
    non-overlapping; oversized atomic regions degrade to ordinary
    chunking (we'd rather have a split procedure than a 50k-char chunk
    that blows past the embedding model's context)."""
    text = text.strip()
    if not text:
        return []
    if not atomic_patterns:
        return split_chunks(text, chunk_size, overlap)

    # Collect all atomic matches, sort by start, drop overlaps (earliest wins).
    matches = []
    for _name, rx in atomic_patterns:
        for m in rx.finditer(text):
            matches.append((m.start(), m.end()))
    matches.sort()
    deduped = []
    last_end = -1
    for s, e in matches:
        if s >= last_end:
            deduped.append((s, e))
            last_end = e

    if not deduped:
        return split_chunks(text, chunk_size, overlap)

    out = []
    cursor = 0
    for s, e in deduped:
        # Normal-chunk the gap before this atomic region.
        if s > cursor:
            gap = text[cursor:s].strip()
            if gap:
                out.extend(split_chunks(gap, chunk_size, overlap))
        block = text[s:e].strip()
        if not block:
            cursor = e
            continue
        if len(block) <= atomic_ceiling:
            # Preserve as one chunk — this is the whole point of atomic_block.
            out.append(block)
        else:
            # Too big to keep whole. Fall back to ordinary splitting so we
            # at least don't ship an embed-overflowing payload.
            out.extend(split_chunks(block, chunk_size, overlap))
        cursor = e
    # Tail after the last atomic match.
    if cursor < len(text):
        tail = text[cursor:].strip()
        if tail:
            out.extend(split_chunks(tail, chunk_size, overlap))
    return [c for c in out if c]


# ---------- Direct ChromaDB v2 HTTP client ----------

class ChromaHTTPError(RuntimeError):
    pass


class ChromaHTTP:
    """Tiny client for ChromaDB's v2 HTTP API that deliberately omits the
    `configuration` body field so the server uses its defaults (avoids the
    `KeyError('_type')` schema-mismatch bug between client and server)."""

    def __init__(self, url, tenant="default_tenant", database="default_database",
                 timeout=30):
        self.base = url.rstrip("/")
        self.tenant = tenant
        self.database = database
        self.timeout = timeout

    def _api(self):
        return f"{self.base}/api/v2/tenants/{self.tenant}/databases/{self.database}"

    def _raise(self, r, action):
        if r.ok:
            return
        body = r.text or ""
        if "_type" in body:
            raise ChromaHTTPError(
                f"{action} failed ({r.status_code}). The chromadb server raised "
                f"KeyError('_type') — this is a known client/server schema mismatch. "
                f"Fixes: drop the offending collection (POST /delete_collection on "
                f"this worker, or click 'Drop collection' in the UI), then retry; "
                f"or align chromadb versions (`pip install --upgrade chromadb`). "
                f"Server said: {body[:300]}"
            )
        raise ChromaHTTPError(f"{action} failed ({r.status_code}): {body[:400]}")

    def heartbeat(self):
        r = requests.get(f"{self.base}/api/v2/heartbeat", timeout=5)
        return r.ok

    def list_collections(self):
        r = requests.get(f"{self._api()}/collections", timeout=self.timeout)
        self._raise(r, "list_collections")
        data = r.json()
        # Different chromadb versions return different shapes; normalize.
        out = []
        for c in data or []:
            if isinstance(c, str):
                out.append({"name": c, "id": None, "metadata": {}})
            elif isinstance(c, dict):
                out.append({
                    "name": c.get("name"),
                    "id": c.get("id"),
                    "metadata": c.get("metadata") or {},
                })
        return out

    def get_collection_by_name(self, name):
        for c in self.list_collections():
            if c.get("name") == name:
                return c
        return None

    def create_collection(self, name, metadata):
        """Create a fresh collection. Sends NO `configuration` field; server uses defaults."""
        body = {"name": name, "metadata": metadata or {}, "get_or_create": False}
        r = requests.post(f"{self._api()}/collections", json=body, timeout=self.timeout)
        self._raise(r, f"create_collection('{name}')")
        return r.json()

    def update_metadata(self, collection_id, metadata):
        body = {"new_metadata": metadata}
        r = requests.put(f"{self._api()}/collections/{collection_id}",
                         json=body, timeout=self.timeout)
        # update sometimes returns empty body; treat 200/2xx as success
        if not r.ok:
            return False
        return True

    def delete_collection(self, name):
        r = requests.delete(f"{self._api()}/collections/{name}", timeout=self.timeout)
        if not r.ok and r.status_code != 404:
            self._raise(r, f"delete_collection('{name}')")
        return r.ok or r.status_code == 404

    def count(self, collection_id):
        r = requests.get(f"{self._api()}/collections/{collection_id}/count",
                         timeout=self.timeout)
        if r.ok:
            try:
                return int(r.json())
            except Exception:
                return 0
        return 0

    def upsert(self, collection_id, ids, embeddings, documents, metadatas):
        body = {"ids": ids, "embeddings": embeddings,
                "documents": documents, "metadatas": metadatas}
        r = requests.post(f"{self._api()}/collections/{collection_id}/upsert",
                          json=body, timeout=180)
        self._raise(r, "upsert")
        return True

    def get_by_ids(self, collection_id, ids):
        body = {"ids": ids, "include": []}
        r = requests.post(f"{self._api()}/collections/{collection_id}/get",
                          json=body, timeout=60)
        if not r.ok:
            return {"ids": []}
        return r.json()

    def get_page(self, collection_id, limit, offset, include=("metadatas",)):
        body = {"limit": limit, "offset": offset, "include": list(include)}
        r = requests.post(f"{self._api()}/collections/{collection_id}/get",
                          json=body, timeout=120)
        self._raise(r, "get(page)")
        return r.json()


# ---------- Collection embed-model guard ----------

def get_collection_for_embed(client, name, embed_model, profile_name="academic_paper"):
    """Get or create the collection (via HTTP) and verify the embed_model
    AND profile tags. Mixing profiles inside one collection corrupts
    retrieval semantics (different section vocabularies + card recipes)
    just as mixing embed models corrupts the vector space — both are
    refused at the gate."""
    existing = client.get_collection_by_name(name)
    if existing is not None:
        meta = dict(existing.get("metadata") or {})
        prev_embed = meta.get("embed_model")
        prev_profile = meta.get("profile")
        if prev_embed and prev_embed != embed_model:
            raise ValueError(
                f"Collection '{name}' was built with embed_model='{prev_embed}', "
                f"but '{embed_model}' was requested. Refusing — these vectors "
                f"are in different embedding spaces. Use a different collection "
                f"name (e.g. '{name}__{embed_model.replace(':', '_')}') or "
                f"pass --embed-model {prev_embed}."
            )
        if prev_profile and prev_profile != profile_name:
            raise ValueError(
                f"Collection '{name}' was built with profile='{prev_profile}', "
                f"but profile='{profile_name}' was requested. Refusing — "
                f"section semantics and card recipes differ across profiles. "
                f"Use a different collection name (e.g. "
                f"'{name}__{profile_name}') or pass --profile {prev_profile}."
            )
        if not prev_embed or not prev_profile:
            new_meta = {**meta, "embed_model": embed_model, "profile": profile_name}
            client.update_metadata(existing["id"], new_meta)
            existing["metadata"] = new_meta
        return existing

    meta = {"hnsw:space": "cosine",
            "embed_model": embed_model,
            "profile":     profile_name}
    created = client.create_collection(name=name, metadata=meta)
    # Some servers return only {id, name}; normalize
    if "metadata" not in created:
        created["metadata"] = meta
    return created


# ---------- Ollama embeddings ----------

def _sanitize_for_embedding(text: str) -> str:
    """Strip characters that occasionally cause Ollama's embedding endpoint to
    return 500 / empty: null bytes, lone surrogates, and other control chars
    (keep \\n \\t)."""
    if not text:
        return ""
    text = text.replace("\x00", " ")
    text = "".join(c for c in text if c == "\n" or c == "\t" or 0x20 <= ord(c) < 0xD800 or ord(c) > 0xDFFF)
    return text.strip()


_CTX_OVERFLOW_HINTS = ("context length", "input length", "exceeds",
                        "too long", "max_seq_length", "maximum sequence")


def _looks_like_context_overflow(body: str) -> bool:
    low = (body or "").lower()
    return any(h in low for h in _CTX_OVERFLOW_HINTS)


def embed_text(ollama_url: str, model: str, text: str, timeout=180,
               attempts=3, backoff=2.0):
    """Embed `text` with Ollama's /api/embeddings, with two kinds of recovery:

    - **transient errors** (timeouts, 5xx other than context, connection
      resets): exponential-backoff retry up to `attempts` times.
    - **context-length overflow** (the embedding model's max-seq-length cap,
      e.g. 512 tokens for mxbai-embed-large, which is *independent of*
      Ollama's `num_ctx`): auto-truncate `text` to 50%, then 25%, retrying
      each time. Stops if even ~200 chars are rejected.
    """
    text = _sanitize_for_embedding(text)
    if not text:
        raise ValueError("empty text after sanitize")
    url = ollama_url.rstrip("/") + "/api/embeddings"
    last_err = None
    cur = text
    truncations = 0
    for i in range(max(1, attempts)):
        try:
            r = requests.post(url, json={"model": model, "prompt": cur}, timeout=timeout)
            body = r.text or ""
            if r.status_code == 500 and _looks_like_context_overflow(body):
                # Truncate and try again — does not consume a retry attempt.
                if truncations < 3 and len(cur) > 250:
                    cur = cur[: max(250, len(cur) // 2)]
                    truncations += 1
                    continue
                raise RuntimeError(
                    f"ollama context overflow even after {truncations} truncation(s): "
                    f"{body[:200]}"
                )
            if r.status_code >= 500 or r.status_code == 408:
                last_err = RuntimeError(f"ollama {r.status_code}: {body[:200]}")
            else:
                r.raise_for_status()
                emb = r.json().get("embedding")
                if isinstance(emb, list) and emb:
                    return emb
                last_err = RuntimeError("ollama returned empty embedding")
        except requests.exceptions.Timeout as e:
            last_err = e
        except requests.exceptions.ConnectionError as e:
            last_err = e
        except Exception as e:
            last_err = e
        if i < attempts - 1:
            time.sleep(backoff * (2 ** i))
    raise last_err if last_err else RuntimeError("embedding failed")


# ---------- Phase 5k: LLM-generated synopsis pass ----------

# A fallback prompt for profiles that flip `synopsize=True` without
# customising their own template. Kept short and corpus-agnostic.
_GENERIC_SYNOPSIS_PROMPT = (
    "Summarise the passage below in 4-6 self-contained sentences for "
    "retrieval. Name the central subjects, key events or arguments, "
    "and concrete locations or objects. Be specific. Do not preface. "
    "Output only the synopsis text.\n\nPASSAGE:\n{text}\n\nSYNOPSIS:"
)


def ollama_generate(ollama_url, model, prompt, num_predict=320,
                    temperature=0.1, timeout=180, attempts=2):
    """Synchronous text generation through Ollama's /api/generate.
    Used by the synopsis pass. Errors propagate after the retry budget
    is exhausted so the caller can decide whether to skip the chunk."""
    url = ollama_url.rstrip("/") + "/api/generate"
    body = {
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": float(temperature),
                    "num_predict": int(num_predict)},
    }
    last_err = None
    for i in range(max(1, attempts)):
        try:
            r = requests.post(url, json=body, timeout=timeout)
            r.raise_for_status()
            out = r.json().get("response", "")
            if not isinstance(out, str):
                raise RuntimeError(f"ollama returned non-string response: {type(out)}")
            text = out.strip()
            if not text:
                raise RuntimeError("ollama returned empty synopsis")
            return text
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(1.5 * (2 ** i))
    raise last_err if last_err else RuntimeError("synopsis generation failed")


def _head_tail(text, max_chars):
    """If text exceeds `max_chars`, keep the first 60% from the head and
    the last 40% from the tail. Preserves opening setup + final reveal
    in long chapters without exploding the prompt."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    return text[:head] + "\n\n[…]\n\n" + text[-tail:]


def build_synopsis_records(profile, spans, full_text, page_for,
                           paper_id, fname, doc_meta, pages_total,
                           ingest_run, opts, state, client, collection_id):
    """For each span whose section is in `profile.synopsize_sections`,
    generate (lazily, only if the target id is not already in the
    collection) a synopsis chunk via the local LLM. Returns three
    parallel lists (docs, ids, metas) that the caller appends to the
    main pending-set before the embed loop runs.

    Idempotency: the synthetic chunk id includes a hash of the SOURCE
    passage. A second ingestion call with the same source text skips the
    LLM entirely (Chroma already has it) — re-ingesting a 30-chapter
    novel after a tiny tweak does not re-pay 30 LLM calls."""
    docs, ids, metas = [], [], []
    if not profile.synopsize:
        return docs, ids, metas
    wanted = set(profile.synopsize_sections or [])
    if not wanted:
        return docs, ids, metas

    llm_url = opts.get("llm_url") or opts.get("ollama_url")
    llm_model = opts.get("llm_model")
    if not llm_model:
        # No model configured -> skip silently. Profile authors who
        # turn synopsize=True must ensure callers pass an llm_model.
        return docs, ids, metas

    prompt_template = profile.synopsis_prompt or _GENERIC_SYNOPSIS_PROMPT
    status = state.setdefault("status", {})

    # First pass: assemble candidates with deterministic ids derived
    # from the source span (NOT from the synopsis text), so we can
    # ask Chroma which already exist BEFORE paying any LLM cost.
    #
    # Hierarchy-aware end offset: a "chapter" span computed by
    # detect_sections ends at the next ANY-name match, so on a manual
    # like the Python Tutorial — where each chapter is followed by
    # numbered sub-sections (2.1, 2.2 ...) — the raw chapter span is
    # just the 40-char header. We therefore re-span each
    # synopsize-eligible match against the NEXT eligible match (or end
    # of document), so the synopsis input covers the full chapter
    # including its sub-sections.
    #
    # Experimental override: when opts["synopsis_naive"] is true the
    # hierarchy-aware re-spanning is bypassed and the original span end
    # (from detect_sections) is used instead. This produces the ~40-char
    # input that motivated the fix in the first place, and is the N
    # condition for the conference paper's §V.B ablation. We also bypass
    # the `len(src) < 400` "too short to be worth a synopsis" filter,
    # because for N the entire point IS that synopses get fed a tiny
    # input — the LLM still has to be called, even if it produces a
    # degenerate output.
    naive_span = bool(opts.get("synopsis_naive"))
    eligible_indices = [i for i, (name, _, _) in enumerate(spans) if name in wanted]
    candidates = []
    for k, idx in enumerate(eligible_indices):
        name, s, _orig_e = spans[idx]
        if naive_span:
            e = _orig_e
        else:
            next_idx = eligible_indices[k + 1] if k + 1 < len(eligible_indices) else None
            e = spans[next_idx][1] if next_idx is not None else len(full_text)
        src = full_text[s:e].strip()
        if not naive_span and len(src) < 400:
            # Too short to be worth a synopsis (likely a stub or
            # mis-detected section header alone). Bypassed in the naive
            # mode so the comparison against H is apples-to-apples on
            # the same set of eligible spans.
            continue
        clipped = _head_tail(src, profile.synopsis_max_input_chars)
        src_hash = sha1(clipped + "|" + (profile.synopsis_prompt or ""))
        # Suffix `-naive` so the N and H collections cannot collide even
        # if the user accidentally ingests both into the same collection
        # name — independent of the source-hash differing between modes.
        naive_suffix = "-naive" if naive_span else ""
        target_id = f"{paper_id}-synopsis-{idx}-{src_hash[:10]}{naive_suffix}"
        candidates.append({
            "idx": idx, "section": name, "src_text": clipped,
            "src_hash": src_hash, "target_id": target_id,
            "page_from": page_for(s),
            "page_to": page_for(e - 1) if e > s else page_for(s),
            "input_chars": len(clipped),
        })

    if not candidates:
        return docs, ids, metas

    # Cheap round-trip: which target_ids already live in the collection?
    existing = set()
    try:
        got = client.get_by_ids(collection_id, [c["target_id"] for c in candidates])
        existing = set(got.get("ids") or [])
    except Exception:
        existing = set()

    status["synopsis_total"] = sum(1 for c in candidates if c["target_id"] not in existing)
    status["synopsis_done"] = 0
    if status["synopsis_total"] == 0:
        return docs, ids, metas

    for cand in candidates:
        if status.get("stop_requested"):
            break
        if cand["target_id"] in existing:
            continue
        prompt = prompt_template.replace("{text}", cand["src_text"])
        try:
            synopsis = ollama_generate(
                llm_url, llm_model, prompt,
                num_predict=profile.synopsis_max_output_tokens,
            )
        except Exception:
            # Skip silently — body chunks of this section still get
            # ingested. A second ingest after the LLM recovers will
            # produce the synopsis (the id is still missing).
            status["synopsis_done"] = status.get("synopsis_done", 0) + 1
            continue
        # Embed a header so the LLM-reader knows what this chunk is.
        body = f"[Synopsis of §{cand['section']} ({cand['idx']})]\n\n{synopsis}"
        docs.append(body)
        ids.append(cand["target_id"])
        meta = {
            "paper_id": paper_id, "filename": fname,
            "title": doc_meta.get("title", ""),
            "doi": doc_meta.get("doi", ""),
            "arxiv_id": doc_meta.get("arxiv_id", ""),
            "section": "synopsis",
            "synopsized_section": cand["section"],
            "synopsized_section_idx": cand["idx"],
            "page_from": cand["page_from"],
            "page_to": cand["page_to"],
            "chunk_index": 0,
            "total_chunks": 1,
            "pages_total": pages_total,
            "content_hash": sha1(body),
            "source_hash": cand["src_hash"],
            "ingest_run": ingest_run,
            "profile": profile.name,
            # Smoking-gun fields for the conference paper §V.B ablation:
            # synopsis_input_chars is the length of the source passage
            # fed to the LLM, and synopsis_naive flags which mode the
            # synopsis was generated under.
            "synopsis_input_chars": cand["input_chars"],
            "synopsis_naive": bool(naive_span),
        }
        if profile.classify_content_type:
            meta["content_type"] = "prose"      # synopses are always prose
        metas.append(meta)
        status["synopsis_done"] = status.get("synopsis_done", 0) + 1

    return docs, ids, metas


# ---------- Phase 5h: coreference resolution pass ----------

# Generic fallback prompt for profiles that flip `coref=True` without
# customising. Less narrative-specific than the novel default; usable
# for memoir/biography/transcript profiles too.
_GENERIC_COREF_PROMPT = (
    "Rewrite the PASSAGE below so that every third-person pronoun "
    "(he, she, it, they, him, her, them, his, her, their, its, "
    "himself, herself, itself, themselves) is replaced by the named "
    "entity it refers to. Use the CONTEXT to find antecedents that "
    "aren't in the passage.{protagonist_clause} Preserve all other "
    "words and punctuation. Do not summarise. Output only the "
    "rewritten passage.\n\n"
    "CONTEXT:\n{context}\n\nPASSAGE:\n{passage}\n\nREWRITTEN PASSAGE:"
)

# Third-person pronouns we care about. Used for the cheap pre-pass that
# skips chunks below the threshold. Case-insensitive, whole-word.
_RX_THIRD_PERSON_PRONOUN = re.compile(
    r"\b(he|she|it|they|him|her|them|his|hers|their|theirs|its|"
    r"himself|herself|itself|themselves)\b",
    re.IGNORECASE,
)


def count_third_person_pronouns(text: str) -> int:
    """Cheap pronoun count used by the coref pre-pass. We skip the
    LLM call when this is below profile.coref_pronoun_threshold —
    saves ~30-50% of calls on a typical novel."""
    return len(_RX_THIRD_PERSON_PRONOUN.findall(text or ""))


def _build_protagonist_clause(name: str) -> str:
    """Construct the prompt clause that injects first-person attribution.
    Empty when `name` is empty — keeps the rest of the template
    free of leftover whitespace/punctuation."""
    if not name:
        return ""
    # The clause is inserted as a SECOND instruction inside the same
    # paragraph; leading space ensures it joins the preceding sentence
    # cleanly. The "outside direct dialogue" qualifier is crucial:
    # dialog lines like 'Anu said, "I cannot."' must NOT be rewritten
    # to 'Anu said, "[Protagonist] cannot."' — the speaker is Anu.
    return (
        f" Additionally, when OUTSIDE direct quoted dialogue, replace "
        f"first-person pronouns (I, me, my, mine, myself) with the "
        f"protagonist's name '{name}' or the appropriate possessive "
        f"form '{name}’s'. INSIDE quoted speech (text inside "
        f"single, double, or curly quotation marks), leave first-person "
        f"pronouns UNCHANGED — the speaker may not be the protagonist."
    )


def ollama_resolve_pronouns(ollama_url, model, context, passage,
                            prompt_template, protagonist_name="",
                            num_predict=2200, temperature=0.05,
                            timeout=240, attempts=2):
    """Single coref call. Returns the resolved text on success, or
    None on failure (caller should fall back to the original passage)."""
    prompt = prompt_template.replace("{context}", context or "(none)")
    prompt = prompt.replace("{passage}", passage)
    prompt = prompt.replace("{protagonist_clause}",
                            _build_protagonist_clause(protagonist_name))
    url = ollama_url.rstrip("/") + "/api/generate"
    body = {
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": float(temperature),
                    "num_predict": int(num_predict)},
    }
    last_err = None
    for i in range(max(1, attempts)):
        try:
            r = requests.post(url, json=body, timeout=timeout)
            r.raise_for_status()
            out = r.json().get("response", "")
            if not isinstance(out, str):
                raise RuntimeError(f"ollama returned non-string: {type(out)}")
            text = out.strip()
            if not text:
                raise RuntimeError("ollama returned empty resolution")
            # Sanity: a model that hallucinated a much-shorter rewrite
            # almost certainly summarised instead of resolving. Reject —
            # we keep the original rather than embed garbage.
            if len(text) < 0.5 * len(passage):
                raise RuntimeError(
                    f"resolved text suspiciously short "
                    f"(orig={len(passage)}, rewritten={len(text)}); "
                    f"likely summarisation"
                )
            return text
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(1.5 * (2 ** i))
    return None     # caller falls back to original


def run_coref_pass(profile, body_chunks, opts, state):
    """For each entry in body_chunks (a list of {text, section, ...}),
    return a parallel list of resolved-text strings (or None to mean
    'no resolution; embed the original').

    The resolution call uses the PREVIOUS chunk's trailing text as
    `context`, so antecedents that aren't named in the current chunk
    can still be found. The pre-pass count_third_person_pronouns
    short-circuits chunks below the profile threshold.

    Stops early and returns whatever has been resolved so far when
    status['stop_requested'] flips. Failure on any single chunk is
    silent — that chunk simply embeds its original text."""
    if not profile.coref:
        return [None] * len(body_chunks)
    llm_url = opts.get("llm_url") or opts.get("ollama_url")
    llm_model = opts.get("llm_model")
    if not llm_model:
        return [None] * len(body_chunks)

    prompt_template = profile.coref_prompt or _GENERIC_COREF_PROMPT
    protagonist_name = (opts.get("protagonist_name") or "").strip()
    status = state.setdefault("status", {})
    ctx_chars = int(profile.coref_context_chars or 0)
    max_input = int(profile.coref_max_input_chars or 8000)
    threshold = int(profile.coref_pronoun_threshold or 0)

    # First pass: decide which chunks pay an LLM call. Helps the
    # progress bar show a meaningful total (only the chunks we'll
    # actually call on, not the ones we'd skip immediately).
    payable = []
    for i, ch in enumerate(body_chunks):
        text = ch["text"]
        n_pron = count_third_person_pronouns(text)
        if n_pron < threshold:
            continue
        if len(text) > max_input:
            continue
        payable.append(i)
    status["coref_total"] = len(payable)
    status["coref_done"] = 0

    resolved = [None] * len(body_chunks)
    if not payable:
        return resolved

    payable_set = set(payable)
    for i, ch in enumerate(body_chunks):
        if status.get("stop_requested"):
            break
        if i not in payable_set:
            continue
        passage = ch["text"]
        # Build context from the previous chunk's tail. For chunk 0 or
        # if the previous chunk was from a different section we still
        # pass *some* context — semantics is gentler than a hard split.
        prev_text = body_chunks[i - 1]["text"] if i > 0 else ""
        context = prev_text[-ctx_chars:] if ctx_chars else ""
        if len(context) + len(passage) > max_input:
            # Shrink context to fit. Keep the FULL passage; context can
            # be partial without much loss.
            context = context[-(max_input - len(passage)):]
        out = ollama_resolve_pronouns(
            llm_url, llm_model, context, passage,
            prompt_template,
            protagonist_name=protagonist_name,
            num_predict=profile.coref_max_output_tokens,
        )
        if out:
            resolved[i] = out
        status["coref_done"] = status.get("coref_done", 0) + 1

    return resolved


# ---------- Main per-paper pipeline ----------

def process_paper(pdf_path, opts, state):
    profile = state["profile"]              # CorpusProfile instance
    fname = os.path.basename(pdf_path)
    paper_id = profile.document_id_from_filename(fname)
    pages = extract_pages(pdf_path)
    if not pages or sum(len(t) for _, t in pages) < 200:
        return {"paper_id": paper_id, "filename": fname, "status": "skipped_no_text", "chunks": 0}

    pages = remove_repeating_headers(pages)

    page_offsets = []
    parts = []
    cursor = 0
    for num, t in pages:
        t = clean_text(t)
        page_offsets.append((cursor, cursor + len(t), num))
        parts.append(t)
        cursor += len(t) + 1
    full_text = "\n".join(parts)

    # All profile-specific decisions are delegated here. The metadata
    # dict carries title plus whatever the profile considers useful
    # (doi/arxiv for papers; version for manuals; nothing extra for
    # novels). We propagate the whole dict into each chunk's metadata
    # so retrieval can filter on it.
    doc_meta = profile.extract_doc_metadata(full_text) or {}
    title = doc_meta.get("title", "")
    doi   = doc_meta.get("doi", "")
    arxiv = doc_meta.get("arxiv_id", "")
    spans = detect_sections(full_text, profile=profile)

    def page_for(off):
        for a, b, num in page_offsets:
            if a <= off < b:
                return num
        return page_offsets[-1][2] if page_offsets else 1

    docs, ids, metas = [], [], []
    ingest_run = state["ingest_run"]

    # Build a {section_name: section_text} dict for the card recipe.
    # Multiple matches of the same section are concatenated (e.g. when a
    # manual has multiple "procedure" blocks).
    sections_by_name = {}
    for name, s, e in spans:
        if name not in sections_by_name:
            sections_by_name[name] = full_text[s:e]
        else:
            sections_by_name[name] += "\n\n" + full_text[s:e]

    # Card chunk (profile decides what goes into it; may be "").
    card_body = profile.build_card(sections_by_name, doc_meta,
                                   profile.card_budget_chars) or ""
    if card_body:
        h = sha1(card_body)
        docs.append(card_body)
        ids.append(f"{paper_id}-card-{h[:10]}")
        card_meta = {
            "paper_id": paper_id, "filename": fname, "title": title,
            "doi": doi, "arxiv_id": arxiv,
            "section": "card", "page_from": 1, "page_to": 1,
            "chunk_index": 0, "total_chunks": 1,
            "pages_total": len(pages), "content_hash": h, "ingest_run": ingest_run,
            "profile": profile.name,
        }
        if profile.classify_content_type:
            card_meta["content_type"] = classify_content_type(card_body)
        metas.append(card_meta)

    # Body chunks per profile-detected section.
    chunk_size = int(opts.get("chunk_size") or profile.chunk_size)
    overlap    = int(opts.get("overlap")    or profile.chunk_overlap)
    atomic_patterns = profile.compiled_atomic_patterns()
    atomic_ceiling = int(profile.atomic_block_ceiling or 4000)
    body_chunks = []
    for name, s, e in spans:
        seg_text = full_text[s:e].strip()
        if not seg_text:
            continue
        if atomic_patterns:
            sub = split_chunks_with_atomic(
                seg_text, chunk_size, overlap,
                atomic_patterns, atomic_ceiling,
            )
        else:
            sub = split_chunks(seg_text, chunk_size, overlap)
        for j, c in enumerate(sub):
            global_off = full_text.find(c, s)
            if global_off < 0:
                global_off = s
            body_chunks.append({
                "text": c, "section": name,
                "page_from": page_for(global_off),
                "page_to": page_for(global_off + len(c) - 1),
            })

    # Phase 5h: coref resolution. Returns a parallel array where
    # resolved_texts[i] is the rewritten version of body_chunks[i]'s
    # text, OR None when the chunk was skipped/failed (we embed the
    # original in those cases). When profile.coref is False the
    # function short-circuits to all-None for zero cost.
    resolved_texts = run_coref_pass(profile, body_chunks, opts, state)

    # When coref is enabled we suffix the chunk id so toggling coref
    # on/off doesn't silently reuse stale embeddings — different
    # embedding source → different vector → different id, period.
    # The protagonist name is folded into the suffix the same way:
    # ingesting "novel + protagonist=Aragorn" must NOT silently reuse
    # vectors from a previous "novel + protagonist=Frodo" run.
    coref_suffix = ""
    if profile.coref:
        proto_for_id = (opts.get("protagonist_name") or "").strip()
        if proto_for_id:
            coref_suffix = f"-coref-p{sha1(proto_for_id)[:6]}"
        else:
            coref_suffix = "-coref"

    # Map chunk_id -> text-to-EMBED. The text we STORE as the Chroma
    # document is always the original (for natural display + LLM
    # citation), so the dict only differs from the document when
    # coref produced a non-None resolution.
    embed_for_id = {}

    total = max(1, len(body_chunks))
    for idx, ch in enumerate(body_chunks):
        h = sha1(ch["text"])
        chunk_id = f"{paper_id}-{ch['section']}-{idx}-{h[:10]}{coref_suffix}"
        docs.append(ch["text"])
        ids.append(chunk_id)
        meta = {
            "paper_id": paper_id, "filename": fname, "title": title,
            "doi": doi, "arxiv_id": arxiv,
            "section": ch["section"],
            "page_from": ch["page_from"], "page_to": ch["page_to"],
            "chunk_index": idx, "total_chunks": total,
            "pages_total": len(pages), "content_hash": h, "ingest_run": ingest_run,
            "profile": profile.name,
        }
        if profile.classify_content_type:
            meta["content_type"] = classify_content_type(ch["text"])
        if profile.extract_entities:
            ents = extract_entities(ch["text"], state,
                                    kinds=profile.entity_kinds)
            # Chroma forbids list-valued metadata; flatten to
            # comma-joined strings per kind.
            for kind, vals in ents.items():
                meta[f"ent_{kind.lower()}"] = ", ".join(vals)
        if profile.coref:
            rt = resolved_texts[idx] if idx < len(resolved_texts) else None
            if rt:
                embed_for_id[chunk_id] = rt
                meta["coref"] = True
                meta["coref_model"] = opts.get("llm_model", "")
                proto_for_meta = (opts.get("protagonist_name") or "").strip()
                if proto_for_meta:
                    meta["coref_protagonist"] = proto_for_meta
            else:
                meta["coref"] = False     # tried but skipped/failed
        metas.append(meta)

    client = state["client"]
    collection_id = state["collection_id"]

    # Phase 5k: synopsis pass. Lives here (between body-chunk metadata
    # assembly and the pending/embed loop) so synopses ride the same
    # embedder + batch upsert path as natural chunks. The function
    # returns ([], [], []) when synopsize=False or no llm_model is
    # configured, keeping this branch zero-cost for paper ingestion.
    syn_docs, syn_ids, syn_metas = build_synopsis_records(
        profile, spans, full_text, page_for,
        paper_id, fname, doc_meta, len(pages),
        ingest_run, opts, state, client, collection_id,
    )
    if syn_docs:
        docs.extend(syn_docs)
        ids.extend(syn_ids)
        metas.extend(syn_metas)

    existing = set()
    try:
        got = client.get_by_ids(collection_id, ids)
        existing = set(got.get("ids") or [])
    except Exception:
        existing = set()

    pending = [(d, i, m) for d, i, m in zip(docs, ids, metas) if i not in existing]
    if not pending:
        return {"paper_id": paper_id, "filename": fname, "status": "already_indexed",
                "chunks": len(ids), "title": title}

    # The Stop button toggles status["stop_requested"]; we honour it
    # *inside* this per-chunk loop so the UI gets a response within
    # roughly one chunk's worth of latency, not "after the current PDF
    # finishes" (which on a 600-page novel could be several minutes).
    # Also publish chunk-grain progress so the UI can render a meaningful
    # bar when total_pdfs is small (single big-file ingestion sits at
    # 0/1 for minutes otherwise).
    status = state.setdefault("status", {})
    status["chunk_total"] = len(pending)
    status["chunk_done"] = 0
    batch_docs, batch_ids, batch_metas, batch_embs = [], [], [], []
    BATCH = 16
    ok_chunks, failed_chunks, errors = 0, 0, []
    stopped = False
    for d, i, m in pending:
        if status.get("stop_requested"):
            stopped = True
            break
        # When coref produced a resolution for this chunk we EMBED the
        # resolved text (so a search for 'Aragorn' surfaces chunks that
        # said 'he') while STORING the original `d` as the document
        # (preserving natural prose for display). embed_for_id is only
        # populated when profile.coref produced a non-None resolution,
        # so the .get(i, d) fall-back keeps the non-coref path
        # behaviourally identical.
        text_to_embed = embed_for_id.get(i, d)
        try:
            emb = embed_text(state["ollama_url"], state["embed_model"], text_to_embed)
        except Exception as e:
            failed_chunks += 1
            status["chunk_done"] = ok_chunks + failed_chunks
            if len(errors) < 3:
                errors.append(f"chunk {m.get('chunk_index')} ({m.get('section')}): {e}")
            continue
        if not emb:
            failed_chunks += 1
            status["chunk_done"] = ok_chunks + failed_chunks
            continue
        batch_docs.append(d); batch_ids.append(i); batch_metas.append(m); batch_embs.append(emb)
        ok_chunks += 1
        status["chunk_done"] = ok_chunks + failed_chunks
        if len(batch_docs) >= BATCH:
            try:
                client.upsert(collection_id, batch_ids, batch_embs, batch_docs, batch_metas)
            except Exception as e:
                if len(errors) < 5:
                    errors.append(f"upsert: {e}")
            batch_docs, batch_ids, batch_metas, batch_embs = [], [], [], []
    # Flush any partial batch — even on stop we keep already-embedded
    # chunks so a resumed run can skip them via the idempotency check.
    if batch_docs:
        try:
            client.upsert(collection_id, batch_ids, batch_embs, batch_docs, batch_metas)
        except Exception as e:
            errors.append(f"upsert(final): {e}")

    if stopped:
        return {"paper_id": paper_id, "filename": fname, "status": "stopped",
                "chunks": ok_chunks, "failed_chunks": failed_chunks,
                "title": title,
                "error": "; ".join(errors) if errors else ""}
    if ok_chunks == 0:
        return {"paper_id": paper_id, "filename": fname, "status": "embed_error",
                "chunks": 0, "failed_chunks": failed_chunks, "title": title,
                "error": "; ".join(errors) or "all chunks failed to embed"}
    return {"paper_id": paper_id, "filename": fname,
            "status": "ok" if failed_chunks == 0 else "partial",
            "chunks": ok_chunks, "failed_chunks": failed_chunks,
            "title": title,
            "error": ("; ".join(errors) if errors else "")}


def run_ingestion(opts, status):
    # Resolve the corpus profile up front so failures surface before any
    # heavy work. Defaults to academic_paper for backward compatibility.
    profile_name = opts.get("profile") or "academic_paper"
    try:
        profile = _get_corpus_profile(profile_name)
    except ValueError as e:
        status["error"] = str(e); status["running"] = False; return

    # Synopsis + coref passes both need an LLM model; surface the
    # missing-config failure here instead of silently skipping every
    # downstream LLM call.
    if not opts.get("llm_model"):
        needs_llm = []
        if profile.synopsize and profile.synopsize_sections:
            needs_llm.append("synopses")
        if profile.coref:
            needs_llm.append("coreference resolution")
        if needs_llm:
            status["error"] = (
                f"Profile '{profile.name}' needs an LLM model for: "
                f"{', '.join(needs_llm)}. Set the 'LLM model' field in the "
                f"UI, or pass --llm-model on the CLI."
            )
            status["running"] = False
            return

    try:
        client = ChromaHTTP(opts["chroma_url"])
        if not client.heartbeat():
            raise RuntimeError(f"chromadb not reachable at {opts['chroma_url']}/api/v2/heartbeat")
        collection = get_collection_for_embed(
            client, opts["collection"], opts["embed_model"],
            profile_name=profile.name,
        )
    except Exception as e:
        status["error"] = f"Chroma connect / mismatch: {e}"; status["running"] = False; return

    state = {
        "client": client,
        "collection_id": collection["id"],
        "ollama_url": opts["ollama_url"],
        "embed_model": opts["embed_model"],
        "ingest_run": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "status": status,                 # so process_paper can poll stop_requested
    }

    pdfs = sorted([f for f in os.listdir(opts["corpus_dir"])
                   if f.lower().endswith(".pdf")])
    if opts.get("limit"):
        pdfs = pdfs[: int(opts["limit"])]

    status["total"] = len(pdfs)
    status["done"] = 0
    status["errors"] = 0
    status["results"] = []
    status["start_ts"] = time.time()
    status["last_file"] = ""
    status["chunk_done"] = 0
    status["chunk_total"] = 0
    status["synopsis_done"] = 0
    status["synopsis_total"] = 0
    status["coref_done"] = 0
    status["coref_total"] = 0
    status["running"] = True
    status["stop_requested"] = False

    manifest_path = os.path.join(opts["corpus_dir"], "..",
                                 f"manifest_{opts['collection']}.json")
    manifest = {"started": state["ingest_run"], "results": []}

    for fname in pdfs:
        if status.get("stop_requested"):
            status["error"] = "stopped by user"
            break
        path = os.path.join(opts["corpus_dir"], fname)
        status["last_file"] = fname
        try:
            res = process_paper(path, opts, state)
        except Exception as e:
            res = {"paper_id": paper_id_from_filename(fname), "filename": fname,
                   "status": "exception", "error": str(e),
                   "trace": traceback.format_exc()[-400:]}
        if res.get("status") not in ("ok", "already_indexed"):
            status["errors"] += 1
        status["done"] += 1
        status["results"].append(res)
        manifest["results"].append(res)
        if status["done"] % 5 == 0 or status["done"] == status["total"]:
            try:
                with open(manifest_path, "w") as f:
                    json.dump(manifest, f, indent=2)
            except Exception:
                pass

    try:
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
    except Exception:
        pass

    # Phase 5n: server-side chain logimport. Previously this was triggered
    # by the React client when it observed status.running flip true→false.
    # That broke when the browser tab was closed or the page reloaded
    # during a long run (3-6 h novel coref): the worker would finish but
    # the audit log would never get the import event. Now the worker
    # makes the call itself via a self-loopback to its own
    # /chain/logimport route. Failures are recorded in status so the UI
    # can surface them; they never abort the ingestion.
    if status.get("done", 0) > 0 and not status.get("error"):
        status["chain_logimport"] = "pending"
        try:
            chain_payload = {
                "analyst":      opts.get("analyst") or "anonymous",
                "collection":   opts["collection"],
                "embed_model":  opts["embed_model"],
                "n_papers":     status["done"],
                "manifest_ref": f"manifest_{opts['collection']}.json",
                "corpus_ref": {
                    "collection":  opts["collection"],
                    "embed_model": opts["embed_model"],
                    "n_papers":    status["done"],
                },
            }
            worker_port = int(os.environ.get("INGEST_PORT", "8010"))
            r = requests.post(
                f"http://127.0.0.1:{worker_port}/chain/logimport",
                json=chain_payload, timeout=60,
            )
            if r.ok:
                status["chain_logimport"] = "ok"
                try:
                    body = r.json()
                    if isinstance(body, dict):
                        # surface txid if the chain bridge returned one
                        for k in ("transaction_id", "tx_id", "txid"):
                            if body.get(k):
                                status["chain_logimport_tx"] = body[k]
                                break
                except Exception:
                    pass
                print(f"[chain] logimport ok for '{opts['collection']}'")
            else:
                status["chain_logimport"] = "error"
                status["chain_logimport_msg"] = (r.text or "")[:300]
                print(f"[chain] logimport failed: {r.status_code} {(r.text or '')[:200]}")
        except Exception as e:
            status["chain_logimport"] = "error"
            status["chain_logimport_msg"] = str(e)[:300]
            print(f"[chain] logimport exception: {e}")
    else:
        status["chain_logimport"] = "skipped"

    status["running"] = False
    status["finished_ts"] = time.time()


# ---------- Flask server ----------

app = Flask(__name__)
CORS(app)
STATUS = {"running": False, "total": 0, "done": 0, "errors": 0,
          "last_file": "", "error": None, "results": [],
          "chunk_done": 0, "chunk_total": 0,
          "synopsis_done": 0, "synopsis_total": 0,
          "coref_done": 0, "coref_total": 0,
          "chain_logimport": "", "chain_logimport_tx": "",
          "chain_logimport_msg": ""}
WORKER = {"thread": None}

# Mount the chain-bridge blueprint. It registers /chain/* endpoints and
# is tolerant of pyntelope / MongoDB being absent or the sscore contract
# not yet deployed — see newRAG/chain_bridge.py.
try:
    from chain_bridge import bp as chain_bp
    app.register_blueprint(chain_bp)
    print("[chain] /chain/* endpoints registered")
except Exception as e:
    print(f"[chain] blueprint NOT registered: {e}")

# Mount the scoring-jobs blueprint. Registers /jobs/* endpoints for the
# server-side scoring orchestrator. On startup, any job left in
# running/paused state by a previous worker process is marked
# 'interrupted' (operator must explicitly POST /jobs/<id>/resume).
try:
    from scoring_jobs import bp as jobs_bp, mark_orphans_interrupted
    app.register_blueprint(jobs_bp)
    print("[jobs] /jobs/* endpoints registered")
    try:
        n = mark_orphans_interrupted()
        if n:
            print(f"[jobs] marked {n} orphan job(s) as 'interrupted' "
                  f"(operator must resume them via POST /jobs/<id>/resume)")
    except Exception as e:
        print(f"[jobs] orphan-sweep skipped: {e}")
except Exception as e:
    print(f"[jobs] blueprint NOT registered: {e}")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "running": STATUS["running"],
        "profiles": _list_corpus_profiles(),
    })


@app.route("/profiles", methods=["GET"])
def profiles():
    """Return the list of registered corpus profiles with their public
    defaults (chunk size / overlap / card budget / excluded sections /
    section names). Used by the ingestion UI to populate the profile
    picker and show what each profile does."""
    out = []
    for name in _list_corpus_profiles():
        p = _get_corpus_profile(name)
        out.append({
            "name":                            p.name,
            "chunk_size":                      p.chunk_size,
            "chunk_overlap":                   p.chunk_overlap,
            "card_budget_chars":               p.card_budget_chars,
            "exclude_from_default_retrieval":  sorted(p.exclude_from_default_retrieval),
            "section_names":                   [n for n, _ in p.section_patterns],
            "extract_entities":                bool(p.extract_entities),
            "synopsize":                       bool(p.synopsize),
            "synopsize_sections":              list(p.synopsize_sections or []),
            "coref":                           bool(p.coref),
        })
    return jsonify({"profiles": out, "default": "academic_paper"})


@app.route("/list_dir", methods=["GET"])
def list_dir():
    """Server-side directory browser. Returns subdirs + pdf count of `path`."""
    raw = request.args.get("path") or os.getcwd()
    try:
        path = os.path.abspath(os.path.expanduser(raw))
    except Exception as e:
        return jsonify({"error": f"bad path: {e}"}), 400
    if not os.path.isdir(path):
        return jsonify({"error": f"not a directory: {path}"}), 404
    try:
        entries = os.listdir(path)
    except PermissionError as e:
        return jsonify({"error": f"permission denied: {e}"}), 403
    subdirs = []
    pdfs = []                                # [{name, size}], sorted by name
    for name in sorted(entries, key=str.lower):
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full):
                subdirs.append(name)
            elif name.lower().endswith(".pdf"):
                try:
                    size = os.path.getsize(full)
                except Exception:
                    size = None
                pdfs.append({"name": name, "size": size})
        except Exception:
            pass
    parent = os.path.dirname(path) if path not in ("/", os.path.sep) else None
    return jsonify({
        "path": path,
        "parent": parent if parent and parent != path else None,
        "subdirs": subdirs,
        # Cap the returned file list to keep big directories cheap to render
        # on the UI side. pdf_count is the *total*; pdfs[] may be truncated.
        "pdf_count": len(pdfs),
        "pdfs": pdfs[:500],
        "pdfs_truncated": len(pdfs) > 500,
        "cwd": os.getcwd(),
    })


@app.route("/start", methods=["POST"])
def start():
    if STATUS["running"]:
        return jsonify({"error": "already running"}), 409
    body = request.get_json(force=True, silent=True) or {}
    opts = {
        "corpus_dir":   os.path.abspath(body.get("corpus_dir", "newRAG/corpus")),
        "chroma_url":   body.get("chroma_url", "http://127.0.0.1:8000"),
        "ollama_url":   body.get("ollama_url", "http://127.0.0.1:11434"),
        "collection":   body.get("collection", "papers_corpus"),
        "embed_model":  body.get("embed_model", "mistral"),
        "chunk_size":   int(body.get("chunk_size", 0)) or None,   # None → profile default
        "overlap":      int(body.get("overlap", 0)) or None,
        "limit":        body.get("limit"),
        "profile":      body.get("profile") or "academic_paper",
        # Phase 5k: synopsis pass. llm_url defaults to ollama_url (same
        # box typically serves both). llm_model is required when the
        # selected profile has synopsize=True; otherwise it's ignored.
        "llm_url":      body.get("llm_url") or body.get("ollama_url") or "http://127.0.0.1:11434",
        "llm_model":    body.get("llm_model") or "",
        # Phase 5h+: when set on a coref-enabled profile, the coref
        # pass *also* substitutes first-person pronouns (I/me/my/...)
        # with this name outside direct dialogue. Without this, a
        # first-person novel's protagonist is never embedded by name,
        # so queries like "who is X's slave?" don't bridge "my slave"
        # to X. Empty string -> first-person substitution is skipped
        # (third-person coref still runs).
        "protagonist_name": body.get("protagonist_name") or "",
        # Phase 5n: analyst name is now propagated to the worker so the
        # server-side chain logimport (at end of run_ingestion) can
        # attribute the import to the right user. Was previously only
        # read by the React client at finish-detection time.
        "analyst": body.get("analyst") or "anonymous",
        # Conference-paper §V.B ablation: when true, the synopsis pass
        # uses the *naïve* span computation (the ~40-char chapter heading)
        # instead of the hierarchy-aware re-spanning. Only useful for the
        # N-config ingestion of the synopsis-span experiment. Default
        # False → normal behaviour.
        "synopsis_naive": bool(body.get("synopsis_naive", False)),
    }
    if not os.path.isdir(opts["corpus_dir"]):
        return jsonify({"error": f"corpus_dir not found: {opts['corpus_dir']}"}), 400

    for k in list(STATUS.keys()):
        STATUS[k] = None if k == "error" else (False if k == "running" else 0 if isinstance(STATUS[k], int) else "" if isinstance(STATUS[k], str) else [])

    t = threading.Thread(target=run_ingestion, args=(opts, STATUS), daemon=True)
    WORKER["thread"] = t
    t.start()
    return jsonify({"started": True, "options": opts})


@app.route("/status", methods=["GET"])
def status():
    s = dict(STATUS)
    if isinstance(s.get("results"), list):
        s["results"] = s["results"][-50:]
    return jsonify(s)


@app.route("/stop", methods=["POST"])
def stop():
    STATUS["stop_requested"] = True
    return jsonify({"stop_requested": True})


@app.route("/collection_info", methods=["GET"])
def collection_info():
    """Return whether a collection exists and which embed_model it was tagged with."""
    chroma_url = request.args.get("chroma", "http://127.0.0.1:8000")
    coll = request.args.get("collection", "papers_corpus")
    try:
        client = ChromaHTTP(chroma_url)
        existing = client.get_collection_by_name(coll)
    except Exception as e:
        return jsonify({"exists": False, "name": coll, "warning": str(e)})
    if existing is None:
        return jsonify({"exists": False, "name": coll})
    try:
        cnt = client.count(existing["id"]) if existing.get("id") else 0
    except Exception:
        cnt = 0
    meta = dict(existing.get("metadata") or {})
    # Enrich with the profile's default exclude list so the chat/scoring
    # UIs can build a profile-aware retrieval filter without a separate
    # /profiles round-trip.
    profile_name = meta.get("profile")
    if profile_name:
        try:
            prof = _get_corpus_profile(profile_name)
            meta["exclude_from_default_retrieval"] = \
                sorted(prof.exclude_from_default_retrieval)
            meta["section_names"] = [n for n, _ in prof.section_patterns]
        except Exception:
            pass
    return jsonify({
        "exists": True, "name": coll, "id": existing.get("id"),
        "count": cnt,
        "embed_model": meta.get("embed_model"),
        "profile": profile_name,
        "metadata": meta,
    })


@app.route("/delete_collection", methods=["POST"])
def delete_collection():
    """Drop a (possibly broken) collection. Used to recover from chromadb
    KeyError('_type') schema-mismatch on existing collections."""
    body = request.get_json(force=True, silent=True) or {}
    chroma_url = body.get("chroma_url") or request.args.get("chroma", "http://127.0.0.1:8000")
    coll = body.get("collection") or request.args.get("collection")
    if not coll:
        return jsonify({"error": "collection name required"}), 400
    try:
        client = ChromaHTTP(chroma_url)
        ok = client.delete_collection(coll)
        return jsonify({"deleted": bool(ok), "name": coll})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/papers", methods=["GET"])
def papers():
    """Return distinct paper_ids in a collection, with title and chunk count."""
    chroma_url = request.args.get("chroma", "http://127.0.0.1:8000")
    coll = request.args.get("collection", "papers_corpus")
    try:
        client = ChromaHTTP(chroma_url)
        existing = client.get_collection_by_name(coll)
        if existing is None:
            return jsonify({"count": 0, "papers": [], "embed_model": None,
                            "collection_metadata": {}, "exists": False})
        coll_id = existing["id"]
        coll_meta = dict(existing.get("metadata") or {})
        n = client.count(coll_id)
        out = {}
        offset, BATCH = 0, 2000
        while offset < n:
            got = client.get_page(coll_id, BATCH, offset)
            for md in got.get("metadatas") or []:
                pid = (md or {}).get("paper_id") or "?"
                row = out.setdefault(pid, {"paper_id": pid, "title": "", "filename": "",
                                          "chunks": 0, "pages": 0})
                row["chunks"] += 1
                if not row["title"] and md.get("title"):
                    row["title"] = md["title"]
                if not row["filename"] and md.get("filename"):
                    row["filename"] = md["filename"]
                if md.get("pages_total"):
                    row["pages"] = md["pages_total"]
            offset += BATCH
        rows = sorted(out.values(), key=lambda r: r["paper_id"])
        return jsonify({
            "count": len(rows), "papers": rows,
            "embed_model": coll_meta.get("embed_model"),
            "collection_metadata": coll_meta,
            "exists": True,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------- CLI ----------

def main_cli():
    p = argparse.ArgumentParser()
    p.add_argument("--cli", action="store_true")
    p.add_argument("--corpus", default="newRAG/corpus")
    p.add_argument("--chroma", default="http://127.0.0.1:8000")
    p.add_argument("--ollama", default="http://127.0.0.1:11434")
    p.add_argument("--collection", default="papers_corpus")
    p.add_argument("--embed-model", default="mistral",
                   help="Ollama model used for embeddings. Defaults to 'mistral' "
                        "to match the app's selectedLLMModel; pass e.g. mxbai-embed-large "
                        "if you want a dedicated embedding model.")
    p.add_argument("--chunk-size", type=int, default=0,
                   help="Override the profile's chunk size. 0 = use profile default.")
    p.add_argument("--overlap", type=int, default=0,
                   help="Override the profile's chunk overlap. 0 = use profile default.")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--port", type=int, default=8010)
    p.add_argument("--profile", default="academic_paper",
                   help="Corpus profile: academic_paper (default), novel, manual. "
                        "See newRAG/corpus_profiles.py.")
    p.add_argument("--list-profiles", action="store_true",
                   help="Print registered profiles and exit.")
    p.add_argument("--llm-model", default="",
                   help="Ollama model for the LLM-generated synopsis pass "
                        "(required when the chosen profile enables synopses, "
                        "e.g. novel or manual).")
    p.add_argument("--llm-url", default="",
                   help="Ollama URL for the synopsis pass. Defaults to --ollama "
                        "if not given (same host typically serves both).")
    p.add_argument("--protagonist", default="",
                   help="When the chosen profile has coref enabled, the coref "
                        "pass also substitutes first-person pronouns (I/me/my) "
                        "with this name outside dialogue. Required for "
                        "first-person novels where the protagonist is never "
                        "named in their own narration. Empty -> third-person "
                        "coref only.")
    p.add_argument("--analyst", default="anonymous",
                   help="Name of the analyst running this ingestion, "
                        "attributed in the chain logimport audit event "
                        "fired at end of run. Defaults to 'anonymous'.")
    p.add_argument("--synopsis-naive", action="store_true",
                   help="Use the *naïve* synopsis span (the ~40-char chapter "
                        "heading from detect_sections, no hierarchy-aware "
                        "re-spanning). For the conference paper's §V.B "
                        "ablation only. Produces a separate, non-colliding "
                        "collection thanks to the '-naive' chunk-id suffix.")
    args = p.parse_args()

    if args.list_profiles:
        for name in _list_corpus_profiles():
            prof = _get_corpus_profile(name)
            sections = ", ".join(n for n, _ in prof.section_patterns)
            print(f"{name:18s}  chunk={prof.chunk_size}/{prof.chunk_overlap}  "
                  f"card_budget={prof.card_budget_chars}  "
                  f"sections=[{sections}]")
        return

    if args.cli:
        opts = {
            "corpus_dir": os.path.abspath(args.corpus),
            "chroma_url": args.chroma, "ollama_url": args.ollama,
            "collection": args.collection, "embed_model": args.embed_model,
            "chunk_size": args.chunk_size or None, "overlap": args.overlap or None,
            "limit": args.limit, "profile": args.profile,
            "llm_url":   args.llm_url or args.ollama,
            "llm_model": args.llm_model,
            "protagonist_name": args.protagonist,
            "analyst": args.analyst,
            "synopsis_naive": args.synopsis_naive,
        }
        run_ingestion(opts, STATUS)
        print(json.dumps({"done": STATUS["done"], "errors": STATUS["errors"],
                          "total": STATUS["total"]}))
        return

    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main_cli()
