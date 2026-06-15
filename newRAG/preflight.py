"""
Pre-flight check for PaSSER-RAG ingestion. Run BEFORE `/start` is hit.
Catches the obvious silent-death causes: dead Ollama, missing model,
GPU OOM, Chroma down, network blackhole.

All targets default to the gpuvm production values; override via env::

    OLLAMA_URL=http://… CHROMA_URL=http://… \\
    EMBED_MODEL=mxbai-embed-large LLM_MODEL=mistral:latest \\
    COLLECTION=jane_eyre \\
    python3 preflight.py

Exits 0 on full pass, 1 on any failure (with a list of what failed).
"""
import os
import sys
import time
import json
import requests


OLLAMA      = os.environ.get("OLLAMA_URL",  "http://195.230.127.226:11850")
CHROMA      = os.environ.get("CHROMA_URL",  "http://92.247.133.89:63140")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "mxbai-embed-large")
LLM_MODEL   = os.environ.get("LLM_MODEL",   "mistral:latest")
COLLECTION  = os.environ.get("COLLECTION",  "")

OK, BAD = "✓", "✗"
issues = []


def check(name, fn):
    sys.stdout.write(f"  ")
    sys.stdout.flush()
    t0 = time.time()
    try:
        msg = fn()
        dt = (time.time() - t0) * 1000
        print(f"{OK} {name:38s}  {msg}  [{dt:>5.0f} ms]")
        return True
    except Exception as e:
        dt = (time.time() - t0) * 1000
        print(f"{BAD} {name:38s}  {type(e).__name__}: {str(e)[:200]}  [{dt:>5.0f} ms]")
        issues.append((name, str(e)))
        return False


# ---- 1. Ollama process alive ----
def _ollama_version():
    r = requests.get(f"{OLLAMA}/api/version", timeout=5)
    r.raise_for_status()
    return f"v{r.json().get('version', '?')}"


# ---- 2. Required models registered ----
def _model_registered(m):
    r = requests.get(f"{OLLAMA}/api/tags", timeout=5)
    r.raise_for_status()
    names = [t["name"] for t in r.json().get("models", [])]
    # Ollama tags missing the colon default to ":latest"; accept either form.
    candidates = {m, m if ":" in m else f"{m}:latest"}
    hit = next((n for n in names if n in candidates), None)
    if not hit:
        raise ValueError(f"not in {names[:8]}…")
    return f"registered as {hit!r}"


# ---- 3. Real embedding call (catches model-load failures NVML-level) ----
def _embed_call():
    r = requests.post(f"{OLLAMA}/api/embed",
                      json={"model": EMBED_MODEL, "input": "hello world"},
                      timeout=60)
    r.raise_for_status()
    j = r.json()
    embs = j.get("embeddings") or ([j["embedding"]] if "embedding" in j else None)
    if not embs:
        raise ValueError(f"no embeddings in response: {list(j.keys())}")
    return f"{len(embs[0])} dims"


# ---- 4. Real LLM generate (one token) — catches GPU OOM, model crashed ----
def _llm_call():
    r = requests.post(f"{OLLAMA}/api/generate",
                      json={"model": LLM_MODEL, "prompt": "hi",
                            "stream": False,
                            "options": {"num_predict": 5, "temperature": 0.0}},
                      timeout=120)
    r.raise_for_status()
    j = r.json()
    return (f"got {j.get('response','')!r:.30s}, "
            f"eval={j.get('eval_count','?')}t, "
            f"done={j.get('done_reason','?')}")


# ---- 5. Chroma heartbeat ----
def _chroma_alive():
    r = requests.get(f"{CHROMA}/api/v2/heartbeat", timeout=5)
    r.raise_for_status()
    return "alive"


# ---- 6. Collection state (only if name given) ----
def _coll_state():
    r = requests.get(f"{CHROMA}/api/v2/tenants/default_tenant/databases/default_database/collections",
                      timeout=10)
    r.raise_for_status()
    for c in r.json():
        if c["name"] == COLLECTION:
            cid = c["id"]
            cr = requests.post(
                f"{CHROMA}/api/v2/tenants/default_tenant/databases/default_database/collections/{cid}/count",
                json={}, timeout=10)
            cr.raise_for_status()
            return f"id={cid[:8]}…, count={cr.json()}"
    raise ValueError(f"collection {COLLECTION!r} not found "
                     f"(have: {[c['name'] for c in r.json()][:8]})")


# ---- 7. Loaded-model snapshot in VRAM ----
def _gpu_loaded():
    r = requests.get(f"{OLLAMA}/api/ps", timeout=5)
    r.raise_for_status()
    models = r.json().get("models", [])
    if not models:
        return "(none loaded; will be lazy-loaded on first use)"
    parts = []
    for m in models:
        vram_gb = (m.get("size_vram") or 0) / (1024**3)
        parts.append(f"{m['name']}={vram_gb:.1f}GB")
    return ", ".join(parts)


# ---- 8. Disk space where THIS process actually writes logs ----
def _client_disk_free():
    """Check the volume that hosts this script's CWD — on macOS, `/` is a
    tiny read-only system volume; the real Data volume is mounted
    separately and is what hosts ~/, /Users, /Volumes, and the script's
    own log files."""
    import os
    import shutil
    target = os.getcwd()
    s = shutil.disk_usage(target)
    return f"volume hosting {target} has {s.free // (1024**3)} GB free of {s.total // (1024**3)} GB"


print(f"Pre-flight @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"  OLLAMA      = {OLLAMA}")
print(f"  CHROMA      = {CHROMA}")
print(f"  EMBED_MODEL = {EMBED_MODEL}")
print(f"  LLM_MODEL   = {LLM_MODEL}")
print(f"  COLLECTION  = {COLLECTION or '(not set — skipping collection probe)'}")
print()

check("Ollama process alive", _ollama_version)
check(f"Embed model registered", lambda: _model_registered(EMBED_MODEL))
check(f"LLM model registered",   lambda: _model_registered(LLM_MODEL))
check("Embed model can embed",   _embed_call)
check("LLM model can generate",  _llm_call)
check("Chroma reachable",        _chroma_alive)
if COLLECTION:
    check(f"Collection {COLLECTION!r} exists", _coll_state)
check("Ollama VRAM snapshot",    _gpu_loaded)
check("Client disk free",        _client_disk_free)

print()
if issues:
    print(f"PRE-FLIGHT FAILED — {len(issues)} issue(s):")
    for name, msg in issues:
        print(f"  · {name}: {msg[:200]}")
    sys.exit(1)
print("PRE-FLIGHT OK ✓ — all checks pass. Safe to start ingestion.")
