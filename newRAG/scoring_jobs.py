"""
Scoring-jobs orchestrator — server-side, MongoDB-persisted, with full
lifecycle (create / pause / resume / cancel / delete / list /
download CSV) and on-restart-marks-orphans-as-interrupted behaviour
(operator must explicitly resume).

Endpoints (mounted at /jobs by the Flask app):

    POST   /jobs                 create + start a new scoring job
    GET    /jobs                 list jobs (newest first)
    GET    /jobs/<id>            single job status (full doc minus _id)
    POST   /jobs/<id>/pause      set pause flag — thread will sleep
    POST   /jobs/<id>/resume     clear pause OR restart from where it stopped
                                 (works for paused, interrupted, error states)
    POST   /jobs/<id>/cancel     stop the thread; mark cancelled
    DELETE /jobs/<id>            remove the job doc (cells optionally with ?with_cells=true)
    GET    /jobs/<id>/cells      list scored cells for this job
    GET    /jobs/<id>/csv        stream CSV download

Each job:
- runs in its own daemon thread,
- streams logcell + sealrun chain transactions through chain_bridge,
- persists every state change in MongoDB collection sscore_jobs,
- writes each completed cell to sscore_cells (the same collection
  /chain/logcell uses, so the existing on-chain verification still works).

Crash recovery (per the user's choice): on worker startup, any job in
state "running" or "paused" is moved to "interrupted" and NOT
auto-resumed. The operator must POST /jobs/<id>/resume to continue.
"""

from __future__ import annotations
import datetime as _dt
import json
import re
import sys
import threading
import time
import traceback
import uuid

import requests
from flask import Blueprint, jsonify, request, Response, stream_with_context

try:
    from pymongo.errors import PyMongoError
except Exception:                                   # pragma: no cover
    PyMongoError = Exception                        # fallback if pymongo missing


def _log(job_id, msg):
    """Cheap stdout logger — prefixes every line with the job's short id
    so multi-job runs can be untangled by `grep <short-id>`."""
    short = (job_id or "????????")[:8]
    print(f"[jobs:{short}] {msg}", flush=True)

# Reuse helpers from chain_bridge — single source of truth for ABI
# encoding, signing, Mongo connection, name normalisation. ChromaHTTP
# lives in ingest_corpus.py; importing it here would create a circular
# dependency at module load (ingest_corpus.py imports this blueprint
# during Flask app setup). Instead we resolve the collection id with a
# small self-contained HTTP call (_resolve_collection_id below).
from chain_bridge import (
    BC_URL,
    CONTRACT,
    _analyst_sign,
    canonical_bytes,
    derive_run_id,
    merkle_root_sha256,
    mongo_db,
    name_safe,
    push_action,
    sha256_hex,
)


def _resolve_collection(chroma_url: str, name: str):
    """Look up `name` and return the full record {id, name, metadata, ...}
    or None if the collection isn't present. The metadata is what
    carries the `profile` and `embed_model` tags."""
    url = (f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/"
           f"databases/default_database/collections")
    try:
        r = requests.get(url, timeout=15)
    except Exception:
        return None
    if not r.ok:
        return None
    try:
        data = r.json() or []
    except Exception:
        return None
    for c in data:
        if isinstance(c, dict) and c.get("name") == name:
            return c
    return None


def _resolve_collection_id(chroma_url: str, name: str):
    """Backward-compat shim. New code should use _resolve_collection."""
    c = _resolve_collection(chroma_url, name)
    return c.get("id") if c else None


def _body_excludes_for_collection(chroma_url: str, name: str) -> list:
    """Look at the collection's stored profile tag and return the
    body-retrieval exclude list it defines. Falls back to v1 behaviour
    (`["references"]`) for collections that pre-date profile tagging."""
    try:
        from corpus_profiles import get_profile
    except Exception:
        return ["references"]
    c = _resolve_collection(chroma_url, name) or {}
    profile_name = (c.get("metadata") or {}).get("profile") or "academic_paper"
    try:
        prof = get_profile(profile_name)
        return sorted(prof.exclude_from_default_retrieval)
    except Exception:
        return ["references"]

bp = Blueprint("jobs", __name__)


# Blueprint-level error handlers: when a remote dependency (MongoDB,
# network) misbehaves *during* a Flask request, return a clean 503 JSON
# response instead of a Flask 500 + full traceback. Every route in this
# blueprint (existing and future) gets this protection for free.

@bp.errorhandler(PyMongoError)
def _handle_pymongo_error(e):
    msg = str(e)
    if len(msg) > 400:
        msg = msg[:400] + "…"
    return jsonify({
        "error":  "mongo unavailable",
        "detail": msg,
        "kind":   "PyMongoError",
    }), 503


@bp.errorhandler(OSError)
def _handle_os_error(e):
    # Catches "connection refused", DNS failures, and so on that propagate
    # from raw sockets / requests-backed calls. We deliberately do NOT
    # catch generic Exception here — real bugs should still surface as
    # Flask 500 with a traceback in the worker log.
    msg = str(e)
    if len(msg) > 400:
        msg = msg[:400] + "…"
    return jsonify({
        "error":  "network unavailable",
        "detail": msg,
        "kind":   type(e).__name__,
    }), 503

# In-memory control structures keyed by job_id. State that needs to
# survive a worker restart lives in Mongo (sscore_jobs); these
# threading objects are recreated when a job is started/resumed.
RUNTIME: dict = {}        # job_id -> {"thread", "pause_event", "cancel_event"}
RUNTIME_LOCK = threading.Lock()


# ---------- Constants ----------

JOBS_COLL = "sscore_jobs"
CELLS_COLL = "sscore_cells"

# Job states. The order roughly reflects normal progression:
#   queued -> running <-> paused -> completed
#                  |__> interrupted (only on worker restart)
#                  |__> cancelled
#                  |__> error
STATE_QUEUED      = "queued"
STATE_RUNNING     = "running"
STATE_PAUSED      = "paused"
STATE_INTERRUPTED = "interrupted"
STATE_COMPLETED   = "completed"
STATE_CANCELLED   = "cancelled"
STATE_ERROR       = "error"

SCORING_PROMPT_TPL = (
    "You are scoring an academic paper against a single criterion.\n"
    "Return ONLY a strict JSON object with keys:\n"
    "  score (integer in {scale}),\n"
    "  justification (1-2 sentences),\n"
    "  evidence (a direct short quote from the context that supports the "
    "score, or \"\").\n"
    "\n"
    "Criterion: {criterion}\n"
    "\n"
    "Context excerpts from the paper (treat as the only evidence available):\n"
    "---\n"
    "{context}\n"
    "---\n"
    "JSON:"
)

# Per-criterion timeout for one LLM call (seconds). Generous because big
# judges (llama3.3:70b) can take 30+ seconds.
LLM_TIMEOUT = 600
EMBED_TIMEOUT = 180
CHROMA_TIMEOUT = 60


# ---------- Small utilities ----------

def _now_iso() -> str:
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _parse_json_block(text: str | None):
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _ollama_embed(ollama_url: str, model: str, text: str):
    r = requests.post(
        f"{ollama_url.rstrip('/')}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=EMBED_TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("embedding")


def _ollama_generate(ollama_url: str, model: str, prompt: str, temperature: float = 0.1):
    r = requests.post(
        f"{ollama_url.rstrip('/')}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": float(temperature)},
        },
        timeout=LLM_TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("response", "")


def _chroma_query(chroma_url: str, collection_id: str, embedding: list,
                  n_results: int, where: dict | None):
    """Direct HTTP Chroma v2 similarity query (avoids the language SDK)."""
    url = (f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/"
           f"databases/default_database/collections/{collection_id}/query")
    body = {
        "query_embeddings": [embedding],
        "n_results": int(n_results),
        "include": ["documents", "metadatas"],
    }
    if where:
        body["where"] = where
    r = requests.post(url, json=body, timeout=CHROMA_TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"chroma query {r.status_code}: {r.text[:300]}")
    return r.json()


# ---------- Per-cell scoring (card-gate + body retrieval + LLM) ----------

def _score_one_cell(collection_id: str, paper: dict,
                    criterion: dict, config: dict):
    """Run the same algorithm as the React component's scorePaperCriterion()
    server-side. Returns a dict with score/justification/evidence/source."""
    coll_id = collection_id
    chroma_url = config["chroma_url"]
    ollama_url = config["ollama_url"]
    embed_model = config["embed_model"]
    llm_model = config["llm_model"]
    params = config.get("params") or {}
    top_k = int(params.get("topK", 6))
    use_card = bool(params.get("useCardGate", True))
    min_card = int(params.get("minCardScore", 2))
    temperature = float(params.get("temperature", 0.1))
    scale = criterion.get("scale", "0-5")

    q_vec = _ollama_embed(ollama_url, embed_model, criterion["criterion"])
    if not q_vec:
        return {"score": None, "justification": "empty embedding",
                "evidence": "", "source": "error"}

    def _docs_from(resp):
        docs = []
        if resp.get("documents") and resp["documents"][0]:
            for d, m in zip(resp["documents"][0], resp["metadatas"][0]):
                docs.append((d, m or {}))
        return docs

    def _call_llm(docs):
        ctx = "\n\n".join(
            f"[chunk {i+1} | section={m.get('section','?')} | "
            f"p.{m.get('page_from','?')}]\n{d}"
            for i, (d, m) in enumerate(docs)
        )
        prompt = SCORING_PROMPT_TPL.format(
            scale=scale, criterion=criterion["criterion"], context=ctx)
        text = _ollama_generate(ollama_url, llm_model, prompt,
                                temperature=temperature)
        parsed = _parse_json_block(text)
        if not parsed or not isinstance(parsed.get("score"), (int, float)):
            return {"score": None,
                    "justification": (text or "")[:220],
                    "evidence": ""}
        return {
            "score": int(parsed["score"]),
            "justification": str(parsed.get("justification", "") or "")[:1000],
            "evidence": str(parsed.get("evidence", "") or "")[:600],
        }

    # Body-section exclude list comes from the collection's profile
    # (resolved once per job in _run_job_inner and passed via config).
    # Falls back to the v1 behaviour ("references" only) for collections
    # that pre-date profile tagging.
    body_excludes = list(config.get("_body_excludes") or ["references"])
    # Always exclude "card" from body retrieval — the card-gate path
    # is what consults it, and it's never a primary evidence source.
    if "card" not in body_excludes:
        body_excludes.append("card")

    if use_card:
        try:
            card_resp = _chroma_query(
                chroma_url, coll_id, q_vec, n_results=1,
                where={"$and": [{"paper_id": paper["paper_id"]},
                                {"section": "card"}]},
            )
        except Exception as e:
            return {"score": None,
                    "justification": f"chroma card-gate query: {e}",
                    "evidence": "", "source": "error"}
        cards = _docs_from(card_resp)
        if cards:
            r = _call_llm(cards)
            if isinstance(r["score"], int) and r["score"] < min_card:
                return {**r, "source": "card"}

    try:
        body_resp = _chroma_query(
            chroma_url, coll_id, q_vec, n_results=top_k,
            where={"$and": [{"paper_id": paper["paper_id"]},
                            {"section": {"$nin": body_excludes}}]},
        )
        body_docs = _docs_from(body_resp)
        if not body_docs:
            # Fallback: just paper_id filter (no section exclusion)
            fb = _chroma_query(chroma_url, coll_id, q_vec, n_results=top_k,
                               where={"paper_id": paper["paper_id"]})
            body_docs = _docs_from(fb)
    except Exception as e:
        return {"score": None,
                "justification": f"chroma body query: {e}",
                "evidence": "", "source": "error"}
    if not body_docs:
        return {"score": 0, "justification": "no chunks retrieved",
                "evidence": "", "source": "none"}

    r = _call_llm(body_docs)
    return {**r, "source": "body"}


# ---------- Chain pushes for cell + run boundaries ----------

def _push_logcell_for_job(chain_run_id: int, paper_id: str, criterion: dict,
                          cell_result: dict, config: dict,
                          run_started_at: str, job_id: str):
    """Compute canonical payload hash, persist to MongoDB sscore_cells,
    then push sscore.logcell. Returns (payload_hash, trx_id, mongo_oid)."""
    scale = criterion.get("scale", "0-5")
    canonical = {
        "v": 1,
        "run_id": chain_run_id,
        "paper_id": str(paper_id),
        "criterion_id": criterion["id"],
        "score": cell_result.get("score"),
        "scale": scale,
        "source": cell_result.get("source"),
        "justification": cell_result.get("justification") or "",
        "evidence": cell_result.get("evidence") or "",
        "llm_model": config.get("llm_model") or "",
        "embed_model": config.get("embed_model") or "",
        "collection": config.get("collection") or "",
        "run_started_at": run_started_at,
    }
    payload_hash = sha256_hex(canonical_bytes(canonical))

    db = mongo_db()
    mongo_oid = ""
    if db is not None:
        try:
            doc = {**canonical, "payload_hash": payload_hash, "job_id": job_id}
            res = db[CELLS_COLL].insert_one(doc)
            mongo_oid = str(res.inserted_id)
        except Exception:
            mongo_oid = ""

    try:
        score_byte = int(cell_result.get("score") or 0)
    except Exception:
        score_byte = 0
    score_byte = max(0, min(255, score_byte))
    try:
        scale_max = int(str(scale).split("-")[1])
    except Exception:
        scale_max = 5

    chain = push_action("logcell", [
        {"name": "run_id",       "value": chain_run_id,                "type": "uint64"},
        {"name": "paper_id",     "value": str(paper_id),               "type": "string"},
        {"name": "criterion_id", "value": name_safe(criterion["id"]),  "type": "name"},
        {"name": "score",        "value": score_byte,                  "type": "uint8"},
        {"name": "scale_max",    "value": max(0, min(255, scale_max)), "type": "uint8"},
        {"name": "payload_hash", "value": payload_hash,                "type": "checksum256"},
        {"name": "mongo_oid",    "value": mongo_oid,                   "type": "string"},
    ])
    return payload_hash, chain.get("trx_id"), mongo_oid


def _push_startrun_for_job(job: dict):
    """Build the startrun params, sign with the analyst, push.
    Returns (chain_run_id, trx_id, error_string_or_None)."""
    cfg = job["config"]
    analyst = name_safe(cfg.get("analyst") or "anonymous")
    llm = name_safe(cfg.get("llm_model") or "")
    embed = name_safe(cfg.get("embed_model") or "")
    collection = cfg["collection"]
    criteria = cfg["criteria"]
    params = cfg.get("params") or {}
    sample_seed = cfg.get("sample_seed") or ""
    started_at = job["started_at"]

    corpus_hash = sha256_hex(canonical_bytes(
        cfg.get("corpus_ref") or {"collection": collection, "embed_model": embed}))
    criteria_hash = sha256_hex(canonical_bytes(criteria))
    params_hash = sha256_hex(canonical_bytes(params))
    chain_run_id = derive_run_id(analyst, started_at, collection, criteria_hash)

    params_for_sig = [
        {"name": "run_id",        "value": chain_run_id,        "type": "uint64"},
        {"name": "analyst",       "value": analyst,             "type": "name"},
        {"name": "llm_model",     "value": llm,                 "type": "name"},
        {"name": "embed_model",   "value": embed,               "type": "name"},
        {"name": "collection",    "value": collection,          "type": "string"},
        {"name": "corpus_hash",   "value": corpus_hash,         "type": "checksum256"},
        {"name": "criteria_hash", "value": criteria_hash,       "type": "checksum256"},
        {"name": "params_hash",   "value": params_hash,         "type": "checksum256"},
        {"name": "sample_seed",   "value": sample_seed,         "type": "string"},
        {"name": "n_papers",      "value": len(cfg["papers"]),  "type": "uint32"},
        {"name": "n_criteria",    "value": len(criteria),       "type": "uint32"},
    ]
    sig = _analyst_sign(analyst, params_for_sig)
    if not sig:
        return None, None, f"no signing key for analyst '{analyst}'"
    payload = params_for_sig + [{"name": "analyst_sig", "value": sig, "type": "signature"}]
    chain = push_action("startrun", payload)
    if not chain.get("trx_id"):
        return chain_run_id, None, chain.get("error")
    return chain_run_id, chain.get("trx_id"), None


def _push_sealrun_for_job(chain_run_id: int, payload_hashes: list[str], analyst_raw: str):
    rows_root = merkle_root_sha256(payload_hashes)
    analyst = name_safe(analyst_raw)
    params_for_sig = [
        {"name": "run_id",    "value": chain_run_id, "type": "uint64"},
        {"name": "rows_root", "value": rows_root,    "type": "checksum256"},
    ]
    sig = _analyst_sign(analyst, params_for_sig)
    if not sig:
        return rows_root, None, f"no signing key for analyst '{analyst}'"
    chain = push_action("sealrun",
                        params_for_sig + [{"name": "analyst_sig",
                                           "value": sig, "type": "signature"}])
    return rows_root, chain.get("trx_id"), chain.get("error")


# ---------- Main worker thread ----------

def _set_state(db, job_id, new_state, **extra):
    upd = {"state": new_state, **extra}
    db[JOBS_COLL].update_one({"job_id": job_id}, {"$set": upd})


def _run_job(job_id: str, pause_event: threading.Event,
             cancel_event: threading.Event):
    """Main worker entry point. Wrapped in a single try/except so any
    uncaught exception lands in the job's `error` field (with full
    traceback in stdout) instead of silently killing the thread."""
    try:
        _run_job_inner(job_id, pause_event, cancel_event)
    except Exception as e:
        tb = traceback.format_exc()
        _log(job_id, f"FATAL in worker thread: {e}")
        _log(job_id, tb)
        db = mongo_db()
        if db is not None:
            try:
                db[JOBS_COLL].update_one(
                    {"job_id": job_id},
                    {"$set": {
                        "state": STATE_ERROR,
                        "error": f"FATAL: {e}",
                        "error_traceback": tb[-1500:],
                        "finished_at": _now_iso(),
                    }},
                )
            except Exception:
                pass


def _run_job_inner(job_id: str, pause_event: threading.Event,
                   cancel_event: threading.Event):
    _log(job_id, "thread started")
    db = mongo_db()
    if db is None:
        _log(job_id, "no mongo connection — exiting")
        return

    job = db[JOBS_COLL].find_one({"job_id": job_id})
    if not job:
        _log(job_id, "job doc disappeared — exiting")
        return
    cfg = job["config"]
    _log(job_id, f"loaded config: analyst={cfg.get('analyst')} "
                 f"llm={cfg.get('llm_model')} "
                 f"collection={cfg.get('collection')} "
                 f"n_papers={len(cfg.get('papers') or [])} "
                 f"n_criteria={len(cfg.get('criteria') or [])}")

    # We may have been restarted — only reset started_at if missing.
    if not job.get("started_at"):
        now = _now_iso()
        db[JOBS_COLL].update_one(
            {"job_id": job_id},
            {"$set": {"started_at": now}},
        )
        job["started_at"] = now

    _set_state(db, job_id, STATE_RUNNING, last_heartbeat=_now_iso())

    # === Open chain run (or reuse if resuming) ===
    chain_run_id = job.get("chain_run_id")
    if not chain_run_id:
        _log(job_id, "pushing startrun to chain…")
        rid, trx, err = _push_startrun_for_job(job)
        _log(job_id, f"startrun result: run_id={rid} trx_id={trx} err={err}")
        if err:
            _set_state(db, job_id, STATE_ERROR,
                       error=f"startrun: {err}",
                       finished_at=_now_iso())
            return
        chain_run_id = rid
        db[JOBS_COLL].update_one(
            {"job_id": job_id},
            {"$set": {"chain_run_id": chain_run_id,
                      "chain_startrun_trx": trx}},
        )

    # === Resolve Chroma collection id + profile-based body excludes ===
    _log(job_id, f"resolving Chroma collection '{cfg['collection']}' "
                 f"at {cfg['chroma_url']}…")
    coll = _resolve_collection(cfg["chroma_url"], cfg["collection"])
    if not coll:
        _log(job_id, "Chroma collection not found — marking error")
        _set_state(db, job_id, STATE_ERROR,
                   error=f"chroma collection '{cfg['collection']}' not found "
                         f"at {cfg['chroma_url']}",
                   finished_at=_now_iso())
        return
    collection_id = coll.get("id")
    profile_name = (coll.get("metadata") or {}).get("profile") or "academic_paper"
    try:
        from corpus_profiles import get_profile as _gp
        body_excludes = sorted(_gp(profile_name).exclude_from_default_retrieval)
    except Exception:
        body_excludes = ["references"]
    cfg["_body_excludes"] = body_excludes
    _log(job_id, f"Chroma collection_id={collection_id} "
                 f"profile={profile_name} body_excludes={body_excludes}")

    # === Scoring loop ===
    papers = cfg["papers"]
    criteria = cfg["criteria"]
    payload_hashes = list(job.get("payload_hashes") or [])
    done_count = int(job.get("done_cells") or 0)
    total = len(papers) * len(criteria)
    _log(job_id, f"entering scoring loop ({total} cells total, "
                 f"{done_count} already done from a previous run)")

    cell_index = 0
    for paper in papers:
        for criterion in criteria:
            cell_index += 1
            if cancel_event.is_set():
                _log(job_id, "cancel requested — exiting loop")
                _set_state(db, job_id, STATE_CANCELLED, finished_at=_now_iso())
                return

            # Wait if paused
            paused_once = False
            while pause_event.is_set():
                if not paused_once:
                    _log(job_id, "pause requested — sleeping")
                    paused_once = True
                if cancel_event.is_set():
                    _set_state(db, job_id, STATE_CANCELLED, finished_at=_now_iso())
                    return
                cur_state_doc = db[JOBS_COLL].find_one({"job_id": job_id}, {"state": 1})
                if cur_state_doc and cur_state_doc.get("state") != STATE_PAUSED:
                    _set_state(db, job_id, STATE_PAUSED)
                time.sleep(1)
            if paused_once:
                _log(job_id, "resumed from pause")
                _set_state(db, job_id, STATE_RUNNING)

            # Already done? (resume scenario)
            existing = db[CELLS_COLL].find_one({
                "job_id": job_id,
                "paper_id": str(paper["paper_id"]),
                "criterion_id": criterion["id"],
            })
            if existing:
                if existing.get("payload_hash") and existing["payload_hash"] not in payload_hashes:
                    payload_hashes.append(existing["payload_hash"])
                continue

            db[JOBS_COLL].update_one(
                {"job_id": job_id},
                {"$set": {
                    "current_paper": paper["paper_id"],
                    "current_criterion": criterion["id"],
                    "last_heartbeat": _now_iso(),
                }},
            )

            t0 = time.time()
            try:
                result = _score_one_cell(collection_id, paper, criterion, cfg)
            except Exception as e:
                result = {"score": None, "justification": f"EXC: {e}",
                          "evidence": "", "source": "error"}
            t_score = time.time() - t0

            try:
                ph, trx, oid = _push_logcell_for_job(
                    chain_run_id, paper["paper_id"], criterion, result,
                    cfg, job["started_at"], job_id)
                payload_hashes.append(ph)
            except Exception as e:
                _log(job_id, f"logcell push failed for "
                             f"paper={paper['paper_id']} "
                             f"crit={criterion['id']}: {e}")
                ph = None

            done_count += 1
            db[JOBS_COLL].update_one(
                {"job_id": job_id},
                {"$set": {
                    "done_cells": done_count,
                    "payload_hashes": payload_hashes,
                    "last_heartbeat": _now_iso(),
                }},
            )
            # Light progress log every 10 cells (and on the first one for
            # visibility while debugging).
            if cell_index <= 3 or cell_index % 10 == 0:
                _log(job_id, f"cell {cell_index}/{total} done "
                             f"(paper={paper['paper_id']} "
                             f"crit={criterion['id']} "
                             f"score={result.get('score')} "
                             f"src={result.get('source')} "
                             f"in {t_score:.1f}s)")

    # === Sealrun ===
    seal_trx = None
    seal_err = None
    rows_root = None
    if payload_hashes:
        _log(job_id, f"pushing sealrun ({len(payload_hashes)} leaves)…")
        rows_root, seal_trx, seal_err = _push_sealrun_for_job(
            chain_run_id, payload_hashes, cfg.get("analyst") or "anonymous")
        _log(job_id, f"sealrun: trx={seal_trx} err={seal_err}")

    _set_state(
        db, job_id, STATE_COMPLETED,
        finished_at=_now_iso(),
        chain_sealrun_trx=seal_trx,
        rows_root=rows_root,
        seal_error=seal_err,
    )
    _log(job_id, "completed")


# ---------- Thread management ----------

def _start_thread(job_id: str):
    """Spawn a fresh worker thread for `job_id`. Idempotent: if a thread
    is already alive for this job, do nothing."""
    with RUNTIME_LOCK:
        rt = RUNTIME.get(job_id)
        if rt and rt["thread"].is_alive():
            return False
        pause_event = threading.Event()
        cancel_event = threading.Event()
        t = threading.Thread(
            target=_run_job,
            args=(job_id, pause_event, cancel_event),
            daemon=True,
            name=f"scoring-{job_id[:8]}",
        )
        RUNTIME[job_id] = {"thread": t, "pause_event": pause_event,
                           "cancel_event": cancel_event}
        t.start()
    return True


def mark_orphans_interrupted():
    """Called once on worker startup. Any job stuck in running/paused
    state (because the previous worker process exited mid-flight) is
    moved to 'interrupted'. The operator chooses whether/when to resume."""
    db = mongo_db()
    if db is None:
        return 0
    res = db[JOBS_COLL].update_many(
        {"state": {"$in": [STATE_RUNNING, STATE_PAUSED]}},
        {"$set": {"state": STATE_INTERRUPTED,
                  "interrupted_at": _now_iso(),
                  "interrupted_reason": "worker restart"}},
    )
    return res.modified_count


def _is_thread_alive_for(job_id):
    """Return True iff this worker process holds a live thread for the
    given job_id. The threading objects are process-local, so a
    'running' job whose owner thread is not alive HERE is a zombie."""
    with RUNTIME_LOCK:
        rt = RUNTIME.get(job_id)
    return bool(rt and rt["thread"].is_alive())


def _reconcile_zombies_inplace(rows):
    """For each job row in `rows`, fill `thread_alive` and — if the
    persisted state says running/paused but no thread is alive here —
    flip the state to 'interrupted' both in Mongo and in the response
    row. Tolerant of Mongo failure: if the update can't be written,
    the response still reflects the correction (so the UI sees the
    real picture immediately) and the next call will retry the write.

    Returns the list of job_ids that were reconciled."""
    repaired = []
    db = mongo_db()
    for r in rows:
        jid = r.get("job_id")
        alive = _is_thread_alive_for(jid)
        r["thread_alive"] = alive
        if not alive and r.get("state") in (STATE_RUNNING, STATE_PAUSED):
            reason = ("zombie: thread not alive (worker crash, OOM, or a "
                      "remote-dependency timeout killed the worker thread)")
            # Patch the row immediately so the UI sees the right state
            # in this response, even if the write below fails.
            r["state"] = STATE_INTERRUPTED
            r["interrupted_at"] = _now_iso()
            r["interrupted_reason"] = reason
            repaired.append(jid)
            if db is not None:
                try:
                    db[JOBS_COLL].update_one(
                        {"job_id": jid,
                         "state": {"$in": [STATE_RUNNING, STATE_PAUSED]}},
                        {"$set": {
                            "state": STATE_INTERRUPTED,
                            "interrupted_at": _now_iso(),
                            "interrupted_reason": reason,
                        }},
                    )
                    _log(jid, "watchdog: zombie → interrupted")
                except Exception as e:
                    _log(jid, f"watchdog: could not persist zombie state — {e}")
    return repaired


# ---------- HTTP endpoints ----------

@bp.route("/jobs", methods=["POST"])
def create_job():
    body = request.get_json(force=True) or {}
    required = ("analyst", "llm_model", "embed_model", "collection",
                "papers", "criteria")
    missing = [k for k in required if k not in body]
    if missing:
        return jsonify({"error": f"missing field(s): {missing}"}), 400

    db = mongo_db()
    if db is None:
        return jsonify({"error": "mongo unavailable"}), 500

    job_id = str(uuid.uuid4())
    job_name = (body.get("name") or
                f"{name_safe(body['llm_model'])} on "
                f"{body['collection']} "
                f"@ {_dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M')}")
    job_doc = {
        "job_id":            job_id,
        "name":              job_name,
        "state":             STATE_QUEUED,
        "config":            body,
        "total_cells":       len(body["papers"]) * len(body["criteria"]),
        "done_cells":        0,
        "payload_hashes":    [],
        "chain_run_id":      None,
        "chain_startrun_trx": None,
        "chain_sealrun_trx":  None,
        "rows_root":         None,
        "current_paper":     None,
        "current_criterion": None,
        "created_at":        _now_iso(),
        "started_at":        None,
        "finished_at":       None,
        "last_heartbeat":    None,
        "error":             None,
    }
    db[JOBS_COLL].insert_one(job_doc)
    _start_thread(job_id)
    return jsonify({"job_id": job_id, "name": job_name})


@bp.route("/jobs", methods=["GET"])
def list_jobs():
    db = mongo_db()
    if db is None:
        return jsonify({"jobs": [], "error": "mongo unavailable"})
    limit = int(request.args.get("limit", 200))
    state = request.args.get("state")  # optional filter
    q = {}
    if state:
        q["state"] = state
    rows = list(db[JOBS_COLL]
                .find(q, {"_id": 0, "config.papers": 0})
                .sort("created_at", -1)
                .limit(limit))
    # Reconcile zombie jobs: if a row claims running/paused but no
    # worker thread is alive for it here, flip to interrupted. The UI
    # then enables Resume + Cancel for the user.
    _reconcile_zombies_inplace(rows)
    return jsonify({"jobs": rows})


@bp.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    db = mongo_db()
    if db is None:
        return jsonify({"error": "mongo unavailable"}), 500
    job = db[JOBS_COLL].find_one({"job_id": job_id}, {"_id": 0})
    if not job:
        return jsonify({"error": "not found"}), 404
    # Single-row reconciliation (same logic as in list_jobs).
    _reconcile_zombies_inplace([job])
    with RUNTIME_LOCK:
        rt = RUNTIME.get(job_id)
        job["pause_flag"] = bool(rt and rt["pause_event"].is_set())
    return jsonify(job)


@bp.route("/jobs/<job_id>/pause", methods=["POST"])
def pause_job(job_id):
    with RUNTIME_LOCK:
        rt = RUNTIME.get(job_id)
    if not rt or not rt["thread"].is_alive():
        # The thread is dead — mark the persisted state to match so the
        # operator can use Resume / Cancel from the UI on the next poll.
        db = mongo_db()
        if db is not None:
            try:
                job = db[JOBS_COLL].find_one({"job_id": job_id}, {"state": 1})
                if job and job.get("state") in (STATE_RUNNING, STATE_PAUSED):
                    db[JOBS_COLL].update_one(
                        {"job_id": job_id},
                        {"$set": {
                            "state": STATE_INTERRUPTED,
                            "interrupted_at": _now_iso(),
                            "interrupted_reason":
                                "pause attempted but worker thread is no longer alive",
                        }},
                    )
            except Exception:
                pass
        return jsonify({
            "error": "no live thread on this worker — job has been marked "
                     "'interrupted' so you can Resume (restarts the thread) "
                     "or Cancel it from the UI",
        }), 409
    rt["pause_event"].set()
    return jsonify({"paused": True})


@bp.route("/jobs/<job_id>/resume", methods=["POST"])
def resume_job(job_id):
    db = mongo_db()
    if db is None:
        return jsonify({"error": "mongo unavailable"}), 500
    job = db[JOBS_COLL].find_one({"job_id": job_id})
    if not job:
        return jsonify({"error": "not found"}), 404
    if job["state"] in (STATE_COMPLETED, STATE_CANCELLED):
        return jsonify({"error": f"job is {job['state']}, cannot resume"}), 409

    with RUNTIME_LOCK:
        rt = RUNTIME.get(job_id)
    if rt and rt["thread"].is_alive():
        rt["pause_event"].clear()
        return jsonify({"resumed": True, "restarted": False})

    # No live thread (interrupted / errored / cold start) — start a new one
    _start_thread(job_id)
    return jsonify({"resumed": True, "restarted": True})


@bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    db = mongo_db()
    if db is None:
        return jsonify({"error": "mongo unavailable"}), 500
    job = db[JOBS_COLL].find_one({"job_id": job_id})
    if not job:
        return jsonify({"error": "not found"}), 404

    with RUNTIME_LOCK:
        rt = RUNTIME.get(job_id)
    if rt:
        rt["cancel_event"].set()
        rt["pause_event"].clear()   # in case it's blocked on pause

    # Eagerly mark cancelled (the thread will also do this when it wakes up)
    if job["state"] not in (STATE_COMPLETED, STATE_CANCELLED):
        db[JOBS_COLL].update_one(
            {"job_id": job_id},
            {"$set": {"state": STATE_CANCELLED, "finished_at": _now_iso()}},
        )
    return jsonify({"cancelled": True})


@bp.route("/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    db = mongo_db()
    if db is None:
        return jsonify({"error": "mongo unavailable"}), 500
    job = db[JOBS_COLL].find_one({"job_id": job_id})
    if not job:
        return jsonify({"error": "not found"}), 404
    if job["state"] in (STATE_RUNNING, STATE_PAUSED):
        return jsonify({"error": "cancel the job first"}), 409
    with_cells = (request.args.get("with_cells") == "true")
    db[JOBS_COLL].delete_one({"job_id": job_id})
    if with_cells:
        db[CELLS_COLL].delete_many({"job_id": job_id})
    return jsonify({"deleted": True, "with_cells": with_cells})


@bp.route("/jobs/<job_id>/cells", methods=["GET"])
def job_cells(job_id):
    db = mongo_db()
    if db is None:
        return jsonify({"error": "mongo unavailable"}), 500
    job = db[JOBS_COLL].find_one({"job_id": job_id})
    if not job:
        return jsonify({"error": "not found"}), 404
    cells = list(db[CELLS_COLL]
                 .find({"job_id": job_id}, {"_id": 0})
                 .sort("payload_hash", 1))
    return jsonify({"job_id": job_id, "count": len(cells), "cells": cells})


CSV_COLUMNS = [
    "paper_id", "title", "filename",
    "criterion_id", "score", "scale", "source",
    "justification", "evidence",
    "llm_model", "embed_model", "collection", "run_started_at",
    "payload_hash",
]


@bp.route("/jobs/<job_id>/csv", methods=["GET"])
def job_csv(job_id):
    db = mongo_db()
    if db is None:
        return Response("mongo unavailable", status=500)
    job = db[JOBS_COLL].find_one({"job_id": job_id})
    if not job:
        return Response("job not found", status=404)
    cells = db[CELLS_COLL].find({"job_id": job_id}, {"_id": 0})

    # Build paper_id → {title, filename} lookup from the job's config so the
    # CSV can carry per-paper provenance alongside the score. cells in
    # sscore_cells only store the canonical payload, which doesn't repeat
    # the title/filename (it carries paper_id as the foreign key). Jobs
    # created before this column was added will have an empty filename;
    # the title was already part of the config from day one.
    paper_meta = {}
    for p in (job.get("config") or {}).get("papers") or []:
        pid = str(p.get("paper_id") or "")
        if pid:
            paper_meta[pid] = {
                "title":    p.get("title") or "",
                "filename": p.get("filename") or "",
            }

    def gen():
        yield ",".join(CSV_COLUMNS) + "\n"
        for c in cells:
            pid = str(c.get("paper_id") or "")
            meta = paper_meta.get(pid, {})
            row = []
            for col in CSV_COLUMNS:
                if col in ("title", "filename"):
                    v = meta.get(col, "")
                else:
                    v = c.get(col)
                if v is None:
                    s = ""
                else:
                    s = str(v).replace('"', '""')
                row.append(f'"{s}"')
            yield ",".join(row) + "\n"

    safe = lambda s: re.sub(r"[^a-zA-Z0-9_.-]+", "_",
                            str(s or "")).strip("_") or "x"
    llm = safe((job.get("config") or {}).get("llm_model"))
    coll = safe((job.get("config") or {}).get("collection"))
    ts = (job.get("started_at") or job["created_at"])[:16].replace(":", "-")
    fname = f"job_{job_id[:8]}_{coll}_{llm}_{ts}.csv"
    return Response(stream_with_context(gen()),
                    mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# Re-exported for ingest_corpus.py to call on startup.
__all__ = ["bp", "mark_orphans_interrupted"]
