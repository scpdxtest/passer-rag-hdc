# Ingestion API — Remote Deployment Notes

Deploy `newRAG/ingest_corpus.py` as a systemd-managed Flask service on the
remote API host so the React frontend can switch between a developer's
local copy (`http://127.0.0.1:8010`) and the deployed copy
(`http://195.230.127.227:8010`) at runtime, based on `window.location.hostname`.

Same port (`8010`) on both sides by design — keeps the FE switch trivial
and lets a developer test the production code path by editing
`localStorage.setItem('ingestAPI', 'http://195.230.127.227:8010')`.

## Architecture summary

```
                ┌───────────── React FE ─────────────┐
                │  window.location.hostname picks    │
                │   localhost → http://127.0.0.1:8010│
                │   anything else → 195.230.127.227  │
                │   localStorage('ingestAPI') wins   │
                └────────────┬─────────┬─────────────┘
                             │         │
            HTTP POST /start │         │ HTTP POST /start
                             ▼         ▼
        ┌─── Developer Mac ───┐     ┌─── 195.230.127.227 ───┐
        │ ingest_corpus.py    │     │ systemd ingest_corpus │
        │   :8010 manual run  │     │   :8010 always-on     │
        └─────────────────────┘     └───────────────────────┘
```

Both processes call out to the same Ollama (`195.230.127.226:11850`,
GPU-powered) and Chroma (`92.247.133.89:63140`) backends — those URLs
are passed in the `/start` request body by the FE, so the ingestion
process itself is location-agnostic.

## Deploy procedure — manual (FileZilla + ssh)

Target host: **195.230.127.227** · ssh user: **boni** · deploy path:
**/home/zemelabc/Users/llmBackEnd**.

### 1. Copy code via FileZilla

Upload these files from `passer/newRAG/` on the Mac into
`/home/zemelabc/Users/llmBackEnd/` on the server. Match the relative
folder structure exactly:

| Local (Mac) | Remote |
|---|---|
| `newRAG/ingest_corpus.py` | `llmBackEnd/ingest_corpus.py` |
| `newRAG/chain_bridge.py` | `llmBackEnd/chain_bridge.py` |
| `newRAG/corpus_profiles.py` | `llmBackEnd/corpus_profiles.py` |
| `newRAG/deploy/ingest_corpus.service` | `llmBackEnd/ingest_corpus.service` (staging) |

The third file is required by `ingest_corpus.py` at import time.

Files you should **NOT** upload (they're either runtime artifacts or
contain local-only state):
- `__pycache__/`, `*.pyc`
- `manifest_*.json`, `audit_manifest*.json`
- `ingestion_debug_*.log`
- `experiments/` (local test outputs)
- The Mac's `.venv*/` — make a fresh venv on the server.

### 2. Set up the Python environment on the server (one-time)

SSH in once and create the venv in the deploy folder:
```bash
ssh boni@195.230.127.227
cd /home/zemelabc/Users/llmBackEnd
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install flask flask-cors requests pyntelope chromadb \
                     pypdf2 pdfplumber spacy beautifulsoup4 lxml \
                     base58 pymongo
.venv/bin/python -m spacy download en_core_web_sm
```

If a future commit adds an `import`, mirror it here. (Run
`grep -E '^import|^from' ingest_corpus.py chain_bridge.py corpus_profiles.py | sort -u`
to confirm the dep list.)

### 3. Install the systemd unit (one-time)

The unit file you uploaded to `/home/zemelabc/Users/llmBackEnd/` is a
staging copy; move it to the systemd location and enable it:

```bash
ssh boni@195.230.127.227
sudo cp /home/zemelabc/Users/llmBackEnd/ingest_corpus.service \
        /etc/systemd/system/ingest_corpus.service
sudo systemctl daemon-reload
sudo systemctl enable --now ingest_corpus.service
```

### 4. Verify

```bash
# Service is up
ssh boni@195.230.127.227 'sudo systemctl status ingest_corpus --no-pager'

# Health probe (from the developer Mac, no need to ssh in)
curl -s http://195.230.127.227:8010/health
curl -s http://195.230.127.227:8010/profiles | python3 -m json.tool | head -5
```

Both should return JSON. If `/profiles` lists `academic_paper`, `novel`,
`manual` etc., the deploy is good.

### 5. Test the FE switch end-to-end

```bash
# Build the FE
cd /Users/boniradev/Downloads/testReact/passer
npm run build

# Serve the production bundle locally — hostname is still localhost,
# so the FE should call the LOCAL ingestion API (developer's :8010).
npx serve -s build -l 3000
# Open http://localhost:3000 → /start → should hit 127.0.0.1:8010.

# Now deploy the bundle to the remote host that serves it in production
# and re-open the FE from that host → /start → should hit
# 195.230.127.227:8010 instead.
```

To force a particular API while debugging:
```javascript
// In the browser DevTools console
localStorage.setItem('ingestAPI', 'http://195.230.127.227:8010');
location.reload();
```

## Updates after the first deploy

Code change → re-upload the changed `.py` via FileZilla → restart:

```bash
ssh boni@195.230.127.227 'sudo systemctl restart ingest_corpus \
                       && sudo journalctl -u ingest_corpus -n 50 --no-pager'
```

If `ingest_corpus.service` itself changes, re-upload it to
`/home/zemelabc/Users/llmBackEnd/`, then:

```bash
ssh boni@195.230.127.227 <<'EOF'
sudo cp /home/zemelabc/Users/llmBackEnd/ingest_corpus.service \
        /etc/systemd/system/ingest_corpus.service
sudo systemctl daemon-reload
sudo systemctl restart ingest_corpus
EOF
```

## Operational notes

- **Port**: 8010 is the same as local. The FE picks based on host, NOT
  on port, so do NOT change the remote port to e.g. 8011 — it would
  break the `localStorage('ingestAPI')` quick override pattern.
- **Firewall**: 195.230.127.227:8010 must be reachable from wherever the
  FE bundle is served. If the FE is on the same host, the port can stay
  bound to `127.0.0.1`; if not, it must be on `0.0.0.0` (the default
  Flask launch in `ingest_corpus.py:1755`).
- **CORS**: `ingest_corpus.py:1458` enables `flask_cors.CORS(app)` with
  no allow-list — wildcard. Tighten before exposing to the public
  internet; for the current LAN-only network this is fine.
- **Logs**: `journalctl -u ingest_corpus -f` for live tail.
  `journalctl -u ingest_corpus --since "1 hour ago"` for post-mortem.
- **Hard-coded chain keys**: `chain_bridge.py:160` (sscore) and `:167`
  (sraudit) are intentionally hard-coded. The systemd unit DOES NOT
  source them from env vars; do not "fix" this without re-reading the
  decision recorded in the project memory.
- **Stop / disable**:
  ```bash
  sudo systemctl stop ingest_corpus
  sudo systemctl disable ingest_corpus
  sudo rm /etc/systemd/system/ingest_corpus.service
  sudo systemctl daemon-reload
  ```

## Why this design (vs. alternatives considered)

- **Same port (8010) on both sides** — keeps the FE switch host-only,
  not host+port; `localStorage('ingestAPI')` overrides remain copy-pasteable
  between the two URLs without editing.
- **HTTP, no DNS** — was the explicit user choice ("IP:port direct").
  Acceptable for the current LAN deployment; revisit if the FE is ever
  served over HTTPS, in which case browser will block the mixed-content
  call to HTTP and an nginx HTTPS termination becomes mandatory (mirror
  the scpdx setup in `blockchain/https-upgrade/05-AS-BUILT-scpdx.md`).
- **systemd, not screen/tmux** — survives reboot, auto-restarts on
  crash, journal logging. Matches the user's explicit preference.
- **Runtime FE switch by `window.location.hostname`** — single React
  bundle for both environments; no separate dev/prod builds; one
  configuration.json. Two extra lines of code, no build pipeline change.
