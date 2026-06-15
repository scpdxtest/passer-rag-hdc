"""
Step 3 helper #3: a single-page web UI for labelling questions.csv against
top100_H.json and top100_N.json. Browser-based — click checkboxes to mark
chunks as gold-relevant, type reference answers, autosaves back to
questions.csv on every change.

Pure stdlib — no Flask, no pip install. Just:

    cd .../synopsis_span_ablation/
    python3 06_label_ui.py [--port 8020]

Then open http://127.0.0.1:8020 in a browser.

Keyboard shortcuts (when not in textarea):
    ← / → : prev / next question
    Esc   : blur focus

Each click autosaves after 300 ms of inactivity. The bar at the top right shows:
    ●  = saved
    ⋯  = saving
    !  = save error
"""
import argparse
import csv
import json
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse


HERE = Path(__file__).resolve().parent
Q_PATH = HERE / "questions.csv"
H_PATH = HERE / "top100_H.json"
N_PATH = HERE / "top100_N.json"

Q_FIELDS = ["q_id", "subset", "target_chapter", "q_text",
            "gold_chunk_ids", "gold_synopsis_id", "reference_answer"]


def load_questions():
    with open(Q_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_questions(rows):
    # Preserve column order exactly (so the file stays diff-clean against the
    # template). Atomic-ish via write-temp-then-rename.
    tmp = Q_PATH.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=Q_FIELDS,
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in Q_FIELDS})
    tmp.replace(Q_PATH)


def load_top100(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {q["q_id"]: q for q in data}


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Synopsis Span Labelling</title>
<style>
  * { box-sizing: border-box; }
  body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif;
         margin: 0; background: #f5f5f5; }
  header { background: #1f2937; color: white; padding: 10px 20px;
           display: flex; align-items: center; gap: 16px; }
  header h1 { margin: 0; font-size: 16px; font-weight: 600; }
  header .meta { font-size: 12px; opacity: 0.7; }
  header .status { margin-left: auto; font-size: 18px; }
  .qbar { background: #374151; color: white; padding: 8px 20px;
          display: flex; align-items: center; gap: 12px; font-size: 13px; }
  .qbar button { padding: 4px 12px; border: 0; background: #4b5563;
                 color: white; border-radius: 4px; cursor: pointer; font-size: 13px; }
  .qbar button:hover { background: #6b7280; }
  .qbar input { width: 50px; padding: 4px; border: 1px solid #6b7280;
                background: #1f2937; color: white; border-radius: 4px; }
  .progress { display: flex; gap: 2px; margin-left: 12px; }
  .progress .dot { width: 12px; height: 12px; border-radius: 2px;
                   background: #4b5563; cursor: pointer; }
  .progress .dot.labelled { background: #10b981; }
  .progress .dot.cur { outline: 2px solid #fbbf24; }
  .qhead { padding: 12px 20px; background: white; border-bottom: 1px solid #e5e7eb; }
  .qhead .row { margin-bottom: 4px; }
  .qhead strong { color: #374151; }
  .pills { display: inline-block; padding: 2px 8px; border-radius: 9999px;
           font-size: 11px; font-weight: 600; }
  .pill-summ { background: #ddd6fe; color: #5b21b6; }
  .pill-ctrl { background: #fee2e2; color: #991b1b; }
  main { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 12px; }
  .col h3 { margin: 0 0 8px; padding: 6px 12px; background: #1f2937;
            color: white; font-size: 13px; border-radius: 4px; }
  .col h3 small { opacity: 0.6; margin-left: 6px; font-weight: 400; }
  .chunk { background: white; border: 1px solid #e5e7eb; padding: 8px 10px;
           margin-bottom: 6px; border-radius: 6px; transition: all 0.15s; }
  .chunk.synopsis { background: #fffbeb; border-color: #fbbf24; }
  .chunk.gold { background: #d1fae5; border-color: #10b981; }
  .chunk.goldsyn { background: #a7f3d0; border-color: #047857; border-width: 2px; }
  .chunk-meta { font-family: ui-monospace, Menlo, monospace; font-size: 11px;
                color: #6b7280; }
  .chunk-meta .badge { display: inline-block; padding: 1px 6px; background: #e5e7eb;
                       border-radius: 3px; margin-right: 4px; }
  .chunk-meta .badge-syn { background: #fef3c7; color: #92400e; }
  .chunk-meta .badge-chap { background: #dbeafe; color: #1e40af; }
  .chunk-meta .badge-warn { background: #fee2e2; color: #991b1b; font-weight: 600; }
  .chunk-id { font-family: ui-monospace, Menlo, monospace; font-size: 10px;
              color: #9ca3af; word-break: break-all; margin-top: 2px; }
  .chunk-snippet { margin-top: 6px; font-size: 12.5px; color: #374151;
                   max-height: 6em; overflow: hidden;
                   display: -webkit-box; -webkit-line-clamp: 4;
                   -webkit-box-orient: vertical; }
  .chunk-snippet.expanded { max-height: none; -webkit-line-clamp: unset; }
  .chunk .controls { margin-top: 6px; display: flex; gap: 12px; align-items: center;
                     font-size: 12px; }
  .chunk .controls label { cursor: pointer; user-select: none; }
  .chunk .controls input { vertical-align: middle; margin-right: 4px; }
  .chunk .controls .more { margin-left: auto; color: #6b7280; cursor: pointer;
                            font-size: 11px; }
  footer { background: white; padding: 12px 20px; border-top: 1px solid #e5e7eb; }
  footer .row { margin-bottom: 8px; }
  textarea { width: 100%; padding: 8px; border: 1px solid #d1d5db; border-radius: 4px;
             font-family: inherit; font-size: 13px; resize: vertical; min-height: 60px; }
  .goldlist { font-family: ui-monospace, Menlo, monospace; font-size: 11px;
              color: #6b7280; word-break: break-all; }
  .help { font-size: 11px; color: #9ca3af; margin-top: 8px; }
</style>
</head>
<body>
<header>
  <h1>Synopsis Span Labelling</h1>
  <div class="meta">questions.csv  ·  ←/→ to navigate</div>
  <div class="status" id="status">●</div>
</header>

<div class="qbar">
  <button onclick="prev()">← Prev</button>
  <span id="qnav" style="min-width: 110px;">Q ? of 25</span>
  <button onclick="next()">Next →</button>
  <span style="margin-left: 12px;">Jump:</span>
  <input id="jumpq" type="number" min="1" max="25">
  <span style="margin-left: 12px;">Labelled: <span id="lblct">0</span> / 25</span>
  <div class="progress" id="progress"></div>
</div>

<div class="qhead">
  <div class="row"><span id="subset-pill" class="pills"></span>
       <span style="margin-left: 8px;"><strong>Target chapter:</strong>
       <span id="chap">—</span></span></div>
  <div class="row" style="font-size: 15px;"><strong>Q:</strong> <span id="qtext"></span></div>
</div>

<main>
  <div class="col">
    <h3>H — python_tutorial <small>(hierarchy-aware synopsis)</small></h3>
    <div id="chunks-h"></div>
    <details style="margin-top: 10px;">
      <summary style="cursor: pointer; padding: 6px 10px; background: #fef3c7; border-radius: 4px; font-size: 13px;">All §synopsis chunks in top-100 (click to expand if the gold synopsis isn't in top-10)</summary>
      <div id="syn-h" style="margin-top: 6px;"></div>
    </details>
  </div>
  <div class="col">
    <h3>N — python_tut_N <small>(naïve synopsis, expect ~40-char input)</small></h3>
    <div id="chunks-n"></div>
    <details style="margin-top: 10px;">
      <summary style="cursor: pointer; padding: 6px 10px; background: #fef3c7; border-radius: 4px; font-size: 13px;">All §synopsis chunks in top-100 (click to expand if the gold synopsis isn't in top-10)</summary>
      <div id="syn-n" style="margin-top: 6px;"></div>
    </details>
  </div>
</main>

<footer>
  <div class="row">
    <strong>Reference answer (1–3 sentences):</strong>
    <textarea id="ref" placeholder="Short factual answer from the gold chunks. Used by the LLM judge to score Faithfulness."></textarea>
  </div>
  <div class="row goldlist">
    <strong>Gold chunks:</strong> <span id="goldids">(none)</span><br>
    <strong>Gold synopsis:</strong> <span id="goldsyn">(none)</span>
  </div>
  <div class="help">
    ● saved · ⋯ saving · ! error.
    A question is "labelled" when it has at least one gold chunk AND a reference answer
    (and a gold synopsis for summarisation Q's).
  </div>
</footer>

<script>
let DATA = [];
let cur = 0;
const $ = id => document.getElementById(id);
const escapeHtml = s => (s || "").replace(/[<>&"]/g,
    c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));

async function load() {
  const r = await fetch('/api/data');
  DATA = await r.json();
  buildProgress();
  render();
}

function isLabelled(q) {
  if (!q.reference_answer || !q.reference_answer.trim()) return false;
  if (!q.gold_chunk_ids || !q.gold_chunk_ids.trim()) return false;
  if (q.subset === 'summarisation' &&
      (!q.gold_synopsis_id || !q.gold_synopsis_id.trim())) return false;
  return true;
}

function buildProgress() {
  const el = $('progress');
  el.innerHTML = '';
  DATA.forEach((q, i) => {
    const dot = document.createElement('div');
    dot.className = 'dot';
    dot.title = `Q${parseInt(q.q_id)}: ${q.q_text}`;
    dot.onclick = () => { cur = i; render(); };
    el.appendChild(dot);
  });
}

function render() {
  const q = DATA[cur];
  $('qnav').textContent = `Q ${parseInt(q.q_id)} of ${DATA.length}`;
  const pill = $('subset-pill');
  pill.textContent = q.subset;
  pill.className = 'pills ' + (q.subset === 'summarisation' ? 'pill-summ' : 'pill-ctrl');
  $('chap').textContent = q.target_chapter || '—';
  $('qtext').textContent = q.q_text;
  $('ref').value = q.reference_answer || '';
  const goldset = new Set((q.gold_chunk_ids || '').split(',').map(s => s.trim()).filter(Boolean));
  const goldsynset = new Set((q.gold_synopsis_id || '').split(',').map(s => s.trim()).filter(Boolean));
  renderCol('chunks-h', q.hits_h, goldset, goldsynset, q.subset);
  renderCol('chunks-n', q.hits_n, goldset, goldsynset, q.subset);
  // The synopsis-only panels: every §synopsis chunk from the full top-100,
  // not just top-10. Same checkbox behaviour as the main view.
  renderCol('syn-h', q.syn_h_all || [], goldset, goldsynset, q.subset);
  renderCol('syn-n', q.syn_n_all || [], goldset, goldsynset, q.subset);
  updateGoldDisplay();
  refreshProgress();
}

function renderCol(elId, hits, goldset, goldsynset, subset) {
  const el = $(elId);
  el.innerHTML = '';
  hits.forEach(h => {
    const isGold = goldset.has(h.id);
    const isGoldSyn = goldsynset.has(h.id);
    const isSyn = h.section === 'synopsis';
    const div = document.createElement('div');
    div.className = 'chunk' + (isSyn ? ' synopsis' : '') + (isGold ? ' gold' : '') + (isGoldSyn ? ' goldsyn' : '');
    const inputChars = h.synopsis_input_chars;
    const inputBadge = inputChars !== null && inputChars !== undefined
      ? `<span class="badge ${inputChars < 100 ? 'badge-warn' : 'badge-syn'}">input=${inputChars}c</span>`
      : '';
    const chapBadge = (h.synopsized_section_idx !== null && h.synopsized_section_idx !== undefined)
      ? `<span class="badge badge-chap">chap.idx=${h.synopsized_section_idx}</span>`
      : '';
    const synCheckHtml = (isSyn && subset === 'summarisation')
      ? `<label><input type="checkbox" class="cb-goldsyn" data-id="${h.id}" ${isGoldSyn ? 'checked' : ''}> <strong>Gold Synopsis</strong></label>`
      : '';
    div.innerHTML = `
      <div class="chunk-meta">
        <span class="badge">rank ${h.rank}</span>
        <span class="badge">§${h.section || '?'}</span>
        ${chapBadge}
        <span class="badge">p.${h.page_from || '?'}-${h.page_to || '?'}</span>
        ${inputBadge}
        ${h.distance !== null && h.distance !== undefined ? `<span class="badge">d=${h.distance.toFixed(3)}</span>` : ''}
      </div>
      <div class="chunk-id">${escapeHtml(h.id)}</div>
      <div class="chunk-snippet">${escapeHtml(h.doc_snippet || '')}</div>
      <div class="controls">
        <label><input type="checkbox" class="cb-gold" data-id="${h.id}" ${isGold ? 'checked' : ''}> Relevant</label>
        ${synCheckHtml}
        <span class="more" onclick="this.parentElement.previousElementSibling.classList.toggle('expanded')">show full</span>
      </div>
    `;
    el.appendChild(div);
  });
  el.querySelectorAll('.cb-gold').forEach(cb => cb.addEventListener('change', onCheck));
  el.querySelectorAll('.cb-goldsyn').forEach(cb => cb.addEventListener('change', onCheck));
}

function onCheck(ev) {
  // The same chunk can appear in BOTH the top-10 main view AND the
  // "All §synopsis chunks" panel below it. Each instance has its own
  // checkbox with the same data-id. Reading :checked across the DOM
  // would leave the chunk in the gold set if any of its checkbox twins
  // happened to be checked. Instead, treat each click as an explicit
  // toggle of the clicked chunk's ID: add to the gold set if the user
  // just ticked it, remove if they just unticked it. The subsequent
  // render() then resyncs all twins to the same state.
  const cb = ev.target;
  const chunkId = cb.dataset.id;
  const fieldName = cb.classList.contains('cb-goldsyn')
    ? 'gold_synopsis_id'
    : 'gold_chunk_ids';
  const q = DATA[cur];
  const set = new Set((q[fieldName] || '').split(',').map(s => s.trim()).filter(Boolean));
  if (cb.checked) set.add(chunkId);
  else set.delete(chunkId);
  q[fieldName] = [...set].join(',');
  // Re-render to sync all twin checkboxes for this chunk to the same state.
  render();
  autosave();
}

function updateGoldDisplay() {
  const q = DATA[cur];
  $('goldids').textContent = q.gold_chunk_ids || '(none)';
  $('goldsyn').textContent = q.gold_synopsis_id || '(none)';
}

function refreshProgress() {
  let count = 0;
  document.querySelectorAll('#progress .dot').forEach((dot, i) => {
    dot.classList.toggle('labelled', isLabelled(DATA[i]));
    dot.classList.toggle('cur', i === cur);
    if (isLabelled(DATA[i])) count++;
  });
  $('lblct').textContent = count;
}

$('ref').addEventListener('input', () => {
  DATA[cur].reference_answer = $('ref').value;
  updateGoldDisplay();
  refreshProgress();
  autosave();
});

let saveTimer = null;
function autosave() {
  clearTimeout(saveTimer);
  $('status').textContent = '⋯';
  saveTimer = setTimeout(async () => {
    const q = DATA[cur];
    try {
      const r = await fetch('/api/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          q_id: q.q_id,
          gold_chunk_ids: q.gold_chunk_ids,
          gold_synopsis_id: q.gold_synopsis_id,
          reference_answer: q.reference_answer,
        })
      });
      $('status').textContent = r.ok ? '●' : '!';
    } catch (e) {
      $('status').textContent = '!';
    }
  }, 300);
}

function prev() { if (cur > 0) { cur--; render(); } }
function next() { if (cur < DATA.length - 1) { cur++; render(); } }

$('jumpq').addEventListener('change', e => {
  const n = parseInt(e.target.value);
  if (n >= 1 && n <= DATA.length) { cur = n - 1; render(); }
});

document.addEventListener('keydown', e => {
  if (['TEXTAREA', 'INPUT'].includes(e.target.tagName)) {
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  if (e.key === 'ArrowLeft') prev();
  if (e.key === 'ArrowRight') next();
});

load();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quieter logging — one line per request.
        sys.stderr.write(f"{self.command} {self.path} → {args[1] if len(args) > 1 else '?'}\n")

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/data":
            try:
                qs = load_questions()
                H = load_top100(H_PATH)
                N = load_top100(N_PATH)
            except Exception as e:
                self._send_json(500, {"error": str(e)}); return
            out = []
            for q in qs:
                qid = q["q_id"]
                hits_h_all = (H.get(qid) or {}).get("hits", [])
                hits_n_all = (N.get(qid) or {}).get("hits", [])
                # Send: the top-10 (the main candidate list) AND all synopsis
                # chunks from the full top-100 (for finding the gold synopsis
                # when the baseline missed it from the top-10). The UI shows
                # the synopsis-only list collapsed under the top-10.
                syn_h = [h for h in hits_h_all if h.get("section") == "synopsis"]
                syn_n = [h for h in hits_n_all if h.get("section") == "synopsis"]
                out.append({
                    "q_id": qid,
                    "subset": q.get("subset", ""),
                    "target_chapter": q.get("target_chapter", ""),
                    "q_text": q.get("q_text", ""),
                    "gold_chunk_ids":    q.get("gold_chunk_ids", ""),
                    "gold_synopsis_id":  q.get("gold_synopsis_id", ""),
                    "reference_answer":  q.get("reference_answer", ""),
                    "hits_h": hits_h_all[:10],
                    "hits_n": hits_n_all[:10],
                    "syn_h_all": syn_h,
                    "syn_n_all": syn_n,
                })
            self._send_json(200, out); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/save":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception as e:
                self._send_json(400, {"error": f"bad json: {e}"}); return
            qid = str(payload.get("q_id", "")).strip()
            if not qid:
                self._send_json(400, {"error": "missing q_id"}); return
            try:
                rows = load_questions()
                hit = False
                for r in rows:
                    if r["q_id"] == qid:
                        r["gold_chunk_ids"]   = payload.get("gold_chunk_ids", "") or ""
                        r["gold_synopsis_id"] = payload.get("gold_synopsis_id", "") or ""
                        r["reference_answer"] = payload.get("reference_answer", "") or ""
                        hit = True
                        break
                if not hit:
                    self._send_json(404, {"error": f"q_id {qid!r} not found"}); return
                save_questions(rows)
            except Exception as e:
                self._send_json(500, {"error": str(e)}); return
            self._send_json(200, {"ok": True, "q_id": qid}); return
        self.send_response(404); self.end_headers()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    for p in (Q_PATH, H_PATH, N_PATH):
        if not p.exists():
            print(f"missing {p.name} — run earlier steps first", file=sys.stderr)
            return 1

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"\nLabel UI ready at  {url}", file=sys.stderr)
    print(f"Edits autosave to  {Q_PATH}", file=sys.stderr)
    print("Press Ctrl-C to stop.\n", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
