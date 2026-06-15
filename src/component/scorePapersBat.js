import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import axios from 'axios';
import { InputText } from 'primereact/inputtext';
import { InputTextarea } from 'primereact/inputtextarea';
import { Button } from 'primereact/button';
import { ProgressBar } from 'primereact/progressbar';
import { DataTable } from 'primereact/datatable';
import { Column } from 'primereact/column';
import { Tag } from 'primereact/tag';
import { Dialog } from 'primereact/dialog';
import { Divider } from 'primereact/divider';
import { Panel } from 'primereact/panel';
import { Dropdown } from 'primereact/dropdown';
import { ChromaClient } from 'chromadb';
import configuration from './configuration.json';
import './scorePapersBat.css';

const CFG = (configuration && configuration.passer) || {};
const DEFAULT_INGEST_API = CFG.IngestAPI || 'http://127.0.0.1:8010';
const DEFAULT_CHROMA = (CFG.Chroma && CFG.Chroma[0] && CFG.Chroma[0].url) || 'http://127.0.0.1:8000';
const DEFAULT_OLLAMA = (CFG.Ollama && CFG.Ollama[0] && CFG.Ollama[0].url) || 'http://127.0.0.1:11434';
const MEMENTO_API = CFG.MementoAPI || 'http://blockchain2.uni-plovdiv.net:9909/wax/get_transaction';

const DEFAULT_CRITERIA = [
    { id: 'topical_fit',  criterion: 'How directly does the paper address blockchain-based e-voting?',           weight: 1, scale: '0-5' },
    { id: 'method_rigor', criterion: 'Are the proposed methods technically rigorous and well-justified?',        weight: 1, scale: '0-5' },
    { id: 'evaluation',   criterion: 'Does the paper provide concrete experiments or evaluation results?',      weight: 1, scale: '0-5' },
    { id: 'novelty',      criterion: 'How novel are the contributions versus prior work?',                       weight: 1, scale: '0-5' },
];

// Three-tier polling cadence:
//   - POLL_MS_FAST: while at least one job is actively progressing, or the
//     detail dialog is open. Cheap to refresh; the user expects ticks.
//   - POLL_MS_SLOW: while only paused / interrupted jobs exist. Polling at
//     all just so a resume triggered from another browser tab / curl is
//     reflected; once a minute is plenty.
//   - no polling: when all jobs are in a terminal state and the dialog is closed.
const POLL_MS_FAST  = 2500;
const POLL_MS_SLOW  = 60000;
const TERMINAL_STATES = new Set(['completed', 'cancelled', 'error']);
const ACTIVE_STATES   = new Set(['queued', 'running']);

const STATE_BADGE = {
    queued:      { severity: 'info',     label: 'queued'      },
    running:     { severity: 'info',     label: 'running'     },
    paused:      { severity: 'warning',  label: 'paused'      },
    interrupted: { severity: 'warning',  label: 'interrupted' },
    completed:   { severity: 'success',  label: 'completed'   },
    cancelled:   { severity: 'secondary',label: 'cancelled'   },
    error:       { severity: 'danger',   label: 'error'       },
};

// PrimeReact's <ProgressBar> keeps a subtle gradient "active" animation on
// the value bar even when value=100, which looks wrong for finished jobs.
// We use it only while the job is actively progressing; for terminal /
// suspended states we render a static coloured bar.
function ProgressBarStatic({ state, pct, height = 10 }) {
    const fillColor = (
        state === 'completed' ? '#22863a' :
        state === 'cancelled' ? '#999999' :
        state === 'error'     ? '#d73a49' :
        state === 'paused'    ? '#fab005' :
        state === 'interrupted' ? '#fd7e14' :
        '#2196f3'
    );
    if (state === 'running' || state === 'queued') {
        // Still progressing — use PrimeReact's animated ProgressBar.
        return <ProgressBar value={pct} style={{ height }} />;
    }
    return (
        <div style={{ height, background: '#e0e0e0', borderRadius: 4,
                      overflow: 'hidden', position: 'relative' }}>
            <div style={{ width: `${Math.max(0, Math.min(100, pct))}%`,
                          height: '100%', background: fillColor,
                          borderRadius: 4, transition: 'width 0.25s' }} />
        </div>
    );
}

const ScorePapersBat = () => {
    // ---------- Config (mirrors what the server worker needs to start a job) ----------
    const [ingestAPI, setIngestAPI] = useState(localStorage.getItem('ingestAPI') || DEFAULT_INGEST_API);
    const [chromaUrl, setChromaUrl] = useState(localStorage.getItem('selectedChromaDB') || DEFAULT_CHROMA);
    const [ollamaUrl, setOllamaUrl] = useState(localStorage.getItem('selectedOllama') || DEFAULT_OLLAMA);
    const [llmModel, setLlmModel] = useState(localStorage.getItem('selectedLLMModel') || 'mistral');
    const [embedModel, setEmbedModel] = useState(
        localStorage.getItem('papersEmbedModel')
        || localStorage.getItem('selectedLLMModel')
        || 'mistral'
    );
    const [collection, setCollection] = useState(localStorage.getItem('papersCollection') || 'papers_corpus');

    const [topK, setTopK] = useState(6);
    const [useCardGate, setUseCardGate] = useState(true);
    const [minCardScore, setMinCardScore] = useState(2);
    const [temperature, setTemperature] = useState(0.1);

    const [collections, setCollections] = useState([]);
    const [papers, setPapers] = useState([]);
    const [selectedPapers, setSelectedPapers] = useState([]);
    const [criteria, setCriteria] = useState(DEFAULT_CRITERIA);
    const [criteriaText, setCriteriaText] = useState(JSON.stringify(DEFAULT_CRITERIA, null, 2));

    const [collInfo, setCollInfo] = useState(null);
    const [analystName, setAnalystName] = useState(
        localStorage.getItem('wharf_user_name')
        || localStorage.getItem('sscoreAnalyst')
        || 'boniradev'
    );

    // ---------- Jobs state ----------
    const [jobs, setJobs] = useState([]);
    const [jobsLoading, setJobsLoading] = useState(false);
    const [creating, setCreating] = useState(false);
    // Backend-degraded banner: populated when /jobs returns a 503 (Mongo
    // or upstream-network failure). Cleared on the next successful poll.
    const [backendDegraded, setBackendDegraded] = useState(null);

    // ---------- Job-detail dialog ----------
    const [detailOpen, setDetailOpen] = useState(false);
    const [detailJob, setDetailJob] = useState(null);     // job summary doc from /jobs/<id>
    const [detailCells, setDetailCells] = useState([]);   // cells from /jobs/<id>/cells

    // ---------- Chain-transaction viewer dialog ----------
    const [txDialogVisible, setTxDialogVisible] = useState(false);
    const [txLoading, setTxLoading] = useState(false);
    const [txData, setTxData] = useState(null);
    const [txError, setTxError] = useState(null);
    const [txTrxId, setTxTrxId] = useState('');

    const pollRef = useRef(null);
    const jobsApi = () => ingestAPI.replace(/\/$/, '') + '/jobs';

    // ---------- Helpers ----------

    const copyToClipboard = (text) => {
        try { navigator.clipboard.writeText(text); } catch (_) { /* no-op */ }
    };

    const viewTransaction = async (txId) => {
        if (!txId) return;
        setTxTrxId(txId);
        setTxDialogVisible(true);
        setTxData(null);
        setTxError(null);
        setTxLoading(true);
        try {
            const resp = await fetch(`${MEMENTO_API}?trx_id=${txId}`);
            const text = await resp.text();
            let data = null;
            try { data = JSON.parse(text); } catch (_) { data = { raw: text }; }
            if (!resp.ok) setTxError(`Memento ${resp.status}: ${text.slice(0, 300)}`);
            setTxData(data);
        } catch (e) {
            setTxError(`Could not fetch transaction: ${e.message}`);
        } finally {
            setTxLoading(false);
        }
    };

    // ---------- Collection discovery + probing ----------

    const listCollections = useCallback(async () => {
        try {
            const client = new ChromaClient({ path: chromaUrl });
            const cs = await client.listCollections();
            setCollections(cs.map((c, i) => ({ name: c, id: i })));
        } catch (e) {
            setCollections([]);
        }
    }, [chromaUrl]);

    const probeCollection = useCallback(async () => {
        try {
            const r = await axios.get(ingestAPI.replace(/\/$/, '') + '/collection_info', {
                params: { chroma: chromaUrl, collection },
                timeout: 5000,
            });
            setCollInfo(r.data || null);
        } catch (e) {
            setCollInfo(null);
        }
    }, [ingestAPI, chromaUrl, collection]);

    const loadPapers = useCallback(async () => {
        try {
            const r = await axios.get(ingestAPI.replace(/\/$/, '') + '/papers', {
                params: { chroma: chromaUrl, collection },
                timeout: 60000,
            });
            setPapers(r.data?.papers || []);
            setSelectedPapers(r.data?.papers || []);
            if (r.data && typeof r.data.embed_model !== 'undefined') {
                setCollInfo({ exists: true, name: collection,
                    embed_model: r.data.embed_model,
                    metadata: r.data.collection_metadata || {},
                    count: (r.data.papers || []).reduce((a, b) => a + (b.chunks || 0), 0) });
            }
        } catch (e) {
            alert('Could not load papers (is ingest_corpus.py running?): ' + (e.response?.data?.error || e.message));
        }
    }, [ingestAPI, chromaUrl, collection]);

    // ---------- Jobs ----------

    const refreshJobs = useCallback(async () => {
        setJobsLoading(true);
        try {
            const r = await axios.get(jobsApi(), { timeout: 10000 });
            setJobs(r.data?.jobs || []);
            setBackendDegraded(null);                // clear on success
        } catch (e) {
            const status = e.response?.status;
            if (status === 503) {
                const body = e.response.data || {};
                setBackendDegraded(
                    `${body.error || 'backend degraded'}` +
                    (body.detail ? ` — ${body.detail}` : '')
                );
            } else if (!e.response) {
                // No response at all → worker process is down.
                setBackendDegraded(`worker unreachable: ${e.message}`);
            }
            // Leave jobs list as-is so the user keeps seeing the last good snapshot.
        } finally {
            setJobsLoading(false);
        }
    }, [ingestAPI]);

    // Auto-refresh on the three-tier schedule described above.
    useEffect(() => {
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
        const hasActive    = jobs.some(j => ACTIVE_STATES.has(j.state));
        const hasSuspended = jobs.some(j => j.state === 'paused' || j.state === 'interrupted');
        let interval = null;
        if (hasActive || detailOpen) interval = POLL_MS_FAST;
        else if (hasSuspended)       interval = POLL_MS_SLOW;
        if (interval) {
            pollRef.current = setInterval(() => {
                refreshJobs();
                if (detailOpen && detailJob?.job_id) reloadJobDetail(detailJob.job_id);
            }, interval);
        }
        return () => { if (pollRef.current) clearInterval(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [jobs, detailOpen]);

    useEffect(() => { listCollections(); }, [listCollections]);
    useEffect(() => { probeCollection(); }, [probeCollection]);
    useEffect(() => { refreshJobs(); }, [refreshJobs]);

    // ---------- Criteria edit (JSON textarea) ----------

    const onCriteriaTextChange = (val) => {
        setCriteriaText(val);
        try {
            const parsed = JSON.parse(val);
            if (Array.isArray(parsed)) setCriteria(parsed);
        } catch (_) { /* ignore until valid */ }
    };

    const onCriteriaFile = (files) => {
        if (!files || !files.length) return;
        const reader = new FileReader();
        reader.onload = (ev) => {
            try {
                const parsed = JSON.parse(ev.target.result);
                if (!Array.isArray(parsed)) throw new Error('JSON must be an array');
                setCriteria(parsed);
                setCriteriaText(JSON.stringify(parsed, null, 2));
            } catch (e) {
                alert('Invalid criteria JSON: ' + e.message);
            }
        };
        reader.readAsText(files[0]);
    };

    // ---------- Job lifecycle calls ----------

    const createJob = async () => {
        if (!selectedPapers.length) { alert('No papers selected'); return; }
        if (!criteria.length) { alert('No criteria'); return; }
        if (collInfo && collInfo.exists && collInfo.embed_model && collInfo.embed_model !== embedModel) {
            const ok = window.confirm(
                `Embedding-model mismatch: collection was built with "${collInfo.embed_model}" `
                + `but you've selected "${embedModel}". Retrieval will return garbage. Continue?`
            );
            if (!ok) return;
        }
        setCreating(true);
        try {
            const body = {
                analyst: analystName,
                llm_model: llmModel,
                embed_model: embedModel,
                ollama_url: ollamaUrl,
                chroma_url: chromaUrl,
                collection,
                papers: selectedPapers.map(p => ({
                    paper_id: String(p.paper_id),
                    title:    p.title    || '',
                    filename: p.filename || '',
                })),
                criteria,
                params: { topK: Number(topK) || 6,
                          useCardGate: !!useCardGate,
                          minCardScore: Number(minCardScore) || 2,
                          temperature: Number(temperature) || 0.1,
                          promptVersion: 1 },
                name: `${llmModel} · ${collection} · ${selectedPapers.length}p × ${criteria.length}c`,
                corpus_ref: { collection, embed_model: embedModel },
            };
            const r = await axios.post(jobsApi(), body, { timeout: 30000 });
            await refreshJobs();
            // Auto-open the new job
            openJobDetail(r.data?.job_id);
        } catch (e) {
            const body = e.response?.data;
            alert('Failed to create job: ' + (body?.error || body || e.message));
        } finally {
            setCreating(false);
        }
    };

    // All control-button handlers call refreshJobs() in a finally block so
    // a failure (e.g. pause on a zombie returning 409) still triggers a
    // table refresh — the bridge typically transitions the job state as
    // part of handling the failure, and the next refresh surfaces it.
    const pauseJob = async (id) => {
        try { await axios.post(`${jobsApi()}/${id}/pause`); }
        catch (e) { alert(e.response?.data?.error || e.message); }
        finally { refreshJobs(); }
    };
    const resumeJob = async (id) => {
        try { await axios.post(`${jobsApi()}/${id}/resume`); }
        catch (e) { alert(e.response?.data?.error || e.message); }
        finally { refreshJobs(); }
    };
    const cancelJob = async (id) => {
        if (!window.confirm('Cancel this job? Already-scored cells are preserved on chain.')) return;
        try { await axios.post(`${jobsApi()}/${id}/cancel`); }
        catch (e) { alert(e.response?.data?.error || e.message); }
        finally { refreshJobs(); }
    };
    const deleteJob = async (id) => {
        const withCells = window.confirm(
            'Delete this job?\n\nOK = delete job + its cells from MongoDB (on-chain receipts are NOT removed).\n' +
            'Cancel will be aborted entirely.'
        );
        if (!withCells) return;
        try {
            await axios.delete(`${jobsApi()}/${id}?with_cells=true`);
            if (detailJob?.job_id === id) setDetailOpen(false);
        } catch (e) { alert(e.response?.data?.error || e.message); }
        finally { refreshJobs(); }
    };

    const reloadJobDetail = async (id) => {
        try {
            const [a, b] = await Promise.all([
                axios.get(`${jobsApi()}/${id}`,      { timeout: 10000 }),
                axios.get(`${jobsApi()}/${id}/cells`, { timeout: 30000 }),
            ]);
            setDetailJob(a.data);
            setDetailCells(b.data?.cells || []);
        } catch (e) { /* leave dialog content stale */ }
    };

    const openJobDetail = async (id) => {
        if (!id) return;
        setDetailOpen(true);
        setDetailJob(null);
        setDetailCells([]);
        reloadJobDetail(id);
    };

    const downloadCSV = (id) => {
        const url = `${jobsApi()}/${id}/csv`;
        window.open(url, '_blank');
    };

    // ---------- Aggregated ranking from detailCells ----------

    const aggregated = useMemo(() => {
        const by = {};
        const weights = {};
        criteria.forEach(c => { weights[c.id] = Number(c.weight ?? 1); });
        for (const c of detailCells) {
            const k = c.paper_id;
            if (!by[k]) by[k] = { paper_id: k, total: 0, weight: 0, count: 0 };
            if (typeof c.score === 'number') {
                const w = weights[c.criterion_id] ?? 1;
                by[k].total += c.score * w;
                by[k].weight += w;
                by[k].count += 1;
            }
        }
        return Object.values(by)
            .map(x => ({ ...x, avg: x.weight ? +(x.total / x.weight).toFixed(2) : null }))
            .sort((a, b) => (b.avg ?? -1) - (a.avg ?? -1));
    }, [detailCells, criteria]);

    // ---------- Render ----------

    return (
        <div className="scorePapersBat">
            <h2>📊 Score Papers Against Criteria</h2>

            {backendDegraded && (
                <div className="card embedGuard mismatch" style={{ marginBottom: 14 }}>
                    <strong>⚠ Backend degraded:</strong>{' '}
                    <code style={{ fontSize: 12, wordBreak: 'break-all' }}>{backendDegraded}</code>
                    <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>
                        Jobs list shows the last successful snapshot. The bridge is still
                        running — checks resume automatically as soon as the dependency comes back.
                    </div>
                </div>
            )}

            {/* ──────────────── Jobs panel (top) ──────────────── */}
            <div className="card">
                <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
                    <h3 style={{ margin: 0 }}>🧰 Scoring jobs</h3>
                    <div className="row" style={{ gap: 6 }}>
                        <Button label="Refresh" icon="pi pi-refresh" size="small"
                                onClick={refreshJobs} loading={jobsLoading} />
                    </div>
                </div>
                <DataTable value={jobs} size="small" stripedRows paginator rows={10}
                           emptyMessage="No jobs yet — configure below and click Create scoring job."
                           selectionMode="single" onRowClick={(e) => openJobDetail(e.data.job_id)}>
                    <Column header="State" style={{ width: 140 }} body={(r) => {
                        const s = STATE_BADGE[r.state] || { severity: 'secondary', label: r.state };
                        const zombie = !r.thread_alive
                            && (r.state === 'running' || r.state === 'paused');
                        return (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                                <Tag severity={s.severity} value={s.label} />
                                {zombie && (
                                    <Tag severity="warning" value="zombie"
                                         style={{ fontSize: 10, padding: '0 4px' }}
                                         title="state says running/paused but no live worker thread — the next refresh marks this 'interrupted' so you can Resume or Cancel" />
                                )}
                                {r.state === 'interrupted' && r.interrupted_reason && (
                                    <span style={{ fontSize: 9, color: '#a06400', maxWidth: 130 }}
                                          title={r.interrupted_reason}>
                                        {r.interrupted_reason.slice(0, 32)}…
                                    </span>
                                )}
                            </div>
                        );
                    }} />
                    <Column field="name" header="Name" />
                    <Column header="Progress" style={{ width: 180 }} body={(r) => {
                        const pct = r.total_cells ? Math.floor(100 * (r.done_cells || 0) / r.total_cells) : 0;
                        return (
                            <div>
                                <ProgressBarStatic state={r.state} pct={pct} />
                                <div style={{ fontSize: 11, color: '#666', marginTop: 2 }}>
                                    {r.done_cells || 0} / {r.total_cells || 0}
                                    {r.state === 'completed' && <span style={{ color: '#22863a', marginLeft: 6 }}>✓</span>}
                                </div>
                            </div>
                        );
                    }} />
                    <Column field="config.llm_model" header="LLM" style={{ width: 130 }}
                            body={(r) => r.config?.llm_model || '—'} />
                    <Column field="created_at" header="Created" style={{ width: 165 }}
                            body={(r) => (r.created_at || '').replace('T', ' ').replace('Z', '')} />
                    <Column header="Actions" style={{ width: 230 }} body={(r) => (
                        <div className="row" style={{ gap: 4 }}>
                            {r.state === 'running' && (
                                <Button icon="pi pi-pause" size="small" severity="warning"
                                        tooltip="Pause" onClick={(e) => { e.stopPropagation(); pauseJob(r.job_id); }} />
                            )}
                            {(r.state === 'paused' || r.state === 'interrupted' || r.state === 'error') && (
                                <Button icon="pi pi-play" size="small" severity="success"
                                        tooltip={r.state === 'interrupted' ? 'Resume (interrupted by worker restart)' : 'Resume'}
                                        onClick={(e) => { e.stopPropagation(); resumeJob(r.job_id); }} />
                            )}
                            {!TERMINAL_STATES.has(r.state) && (
                                <Button icon="pi pi-times" size="small" severity="danger" outlined
                                        tooltip="Cancel"
                                        onClick={(e) => { e.stopPropagation(); cancelJob(r.job_id); }} />
                            )}
                            <Button icon="pi pi-eye" size="small" severity="secondary" outlined
                                    tooltip="View details"
                                    onClick={(e) => { e.stopPropagation(); openJobDetail(r.job_id); }} />
                            {(r.done_cells || 0) > 0 && (
                                <Button icon="pi pi-download" size="small" outlined
                                        tooltip="Download CSV"
                                        onClick={(e) => { e.stopPropagation(); downloadCSV(r.job_id); }} />
                            )}
                            {TERMINAL_STATES.has(r.state) && (
                                <Button icon="pi pi-trash" size="small" severity="danger" outlined
                                        tooltip="Delete (with cells)"
                                        onClick={(e) => { e.stopPropagation(); deleteJob(r.job_id); }} />
                            )}
                        </div>
                    )} />
                </DataTable>
            </div>

            {/* ──────────────── Configuration cards (used to create a new job) ──────────────── */}

            <div className="grid-2col">
                <div className="card">
                    <h3>🗄 Source</h3>
                    <label>ChromaDB URL</label>
                    <div className="row">
                        <InputText value={chromaUrl} onChange={(e) => { setChromaUrl(e.target.value); localStorage.setItem('selectedChromaDB', e.target.value); }} style={{ flex: 1 }} />
                        <Button label="↻" size="small" onClick={listCollections} />
                    </div>
                    <label>Collection ({collections.length} available — type to filter)</label>
                    <div className="row">
                        <Dropdown
                            value={collection}
                            options={collections}
                            optionLabel="name"
                            optionValue="name"
                            onChange={(e) => { setCollection(e.value); localStorage.setItem('papersCollection', e.value); }}
                            placeholder={collections.length ? "Pick a Chroma collection" : "Click ↻ to load collections"}
                            filter
                            filterPlaceholder="Filter collections…"
                            filterBy="name"
                            showClear
                            editable
                            emptyMessage="No collections — check ChromaDB URL"
                            emptyFilterMessage="No collection matches the filter"
                            style={{ flex: 1, minWidth: 0 }}
                        />
                        <Button label="Load papers" size="small" onClick={loadPapers} disabled={!collection} />
                    </div>
                    <label>Embedding model (must match ingestion; defaults to app LLM)</label>
                    <InputText
                        value={embedModel}
                        onChange={(e) => { setEmbedModel(e.target.value); localStorage.setItem('papersEmbedModel', e.target.value); }}
                    />
                    <label>Ingestion API (also hosts the scoring-jobs worker)</label>
                    <InputText value={ingestAPI} onChange={(e) => { setIngestAPI(e.target.value); localStorage.setItem('ingestAPI', e.target.value); }} />
                </div>

                <div className="card">
                    <h3>🤖 LLM</h3>
                    <label>Analyst (signs chain commitments — must be in ANALYST_KEYS or registered with Anchor)</label>
                    <InputText value={analystName}
                               onChange={(e) => { setAnalystName(e.target.value); localStorage.setItem('sscoreAnalyst', e.target.value); }}
                               placeholder="boniradev" />
                    <label>Ollama URL</label>
                    <InputText value={ollamaUrl} onChange={(e) => { setOllamaUrl(e.target.value); localStorage.setItem('selectedOllama', e.target.value); }} />
                    <label>LLM model</label>
                    <InputText value={llmModel} onChange={(e) => { setLlmModel(e.target.value); localStorage.setItem('selectedLLMModel', e.target.value); }} />
                    <div className="grid-3col">
                        <div>
                            <label>Top-K chunks</label>
                            <InputText value={topK} onChange={(e) => setTopK(e.target.value)} />
                        </div>
                        <div>
                            <label>Card-gate min score</label>
                            <InputText value={minCardScore} onChange={(e) => setMinCardScore(e.target.value)} />
                        </div>
                        <div>
                            <label>Temperature</label>
                            <InputText value={temperature} onChange={(e) => setTemperature(e.target.value)} />
                        </div>
                    </div>
                    <div style={{ marginTop: 8 }}>
                        <label>
                            <input type="checkbox" checked={useCardGate} onChange={(e) => setUseCardGate(e.target.checked)} />
                            &nbsp;Use card-gate (cheap reject via abstract)
                        </label>
                    </div>
                </div>
            </div>

            <div className="card">
                <h3>📑 Criteria</h3>
                <div className="row">
                    <label htmlFor="critFile" style={{ marginRight: 8 }}>Load criteria JSON:</label>
                    <input id="critFile" type="file" accept=".json" onChange={(e) => onCriteriaFile(e.target.files)} />
                </div>
                <InputTextarea value={criteriaText} onChange={(e) => onCriteriaTextChange(e.target.value)}
                               rows={6} autoResize style={{ width: '100%', marginTop: 8, fontFamily: 'monospace', fontSize: 12 }} />
                <div className="hint">Format: <code>[{`{ "id":"...","criterion":"...","weight":1,"scale":"0-5" }`}]</code> &nbsp; Parsed: <b>{criteria.length}</b> criteria</div>
            </div>

            {collInfo && collInfo.exists && (
                <div className={`card embedGuard ${collInfo.embed_model && collInfo.embed_model !== embedModel ? 'mismatch' : 'match'}`}>
                    <strong>Collection check:</strong>{' '}
                    <code>{collection}</code> was built with{' '}
                    <code>embed_model = {collInfo.embed_model || '(untagged)'}</code>.
                    {collInfo.embed_model && collInfo.embed_model !== embedModel && (
                        <div className="err" style={{ marginTop: 8 }}>
                            ⚠ Current Embedding Model is <code>{embedModel}</code>. Retrieval
                            will silently return garbage. Either set Embedding Model to{' '}
                            <code>{collInfo.embed_model}</code> or pick a different collection.
                        </div>
                    )}
                </div>
            )}

            <div className="card">
                <h3>📦 Papers ({papers.length} loaded, {selectedPapers.length} selected)</h3>
                <DataTable value={papers} size="small" scrollable scrollHeight="260px"
                           selection={selectedPapers} onSelectionChange={(e) => setSelectedPapers(e.value)}
                           selectionMode="checkbox" dataKey="paper_id" paginator rows={20}
                           emptyMessage="Click 'Load papers' above. Make sure ingest_corpus.py is running.">
                    <Column selectionMode="multiple" headerStyle={{ width: 40 }} />
                    <Column field="paper_id" header="ID" sortable style={{ width: 80 }} />
                    <Column field="title" header="Title" />
                    <Column field="filename" header="File" />
                    <Column field="chunks" header="Chunks" sortable style={{ width: 90 }} />
                </DataTable>
            </div>

            <div className="card">
                <div className="row">
                    <Button label={creating ? 'Creating…' : '🚀 Create scoring job'}
                            onClick={createJob}
                            disabled={creating || !selectedPapers.length || !criteria.length} />
                </div>
                <div className="hint" style={{ marginTop: 6 }}>
                    Creates a server-side job: {selectedPapers.length} paper(s) × {criteria.length} criteria
                    = {selectedPapers.length * criteria.length} cells.
                    The job runs in a worker thread; you can close the browser and come back.
                </div>
            </div>

            {/* ──────────────── Job detail dialog ──────────────── */}
            <Dialog header={detailJob ? `Job · ${detailJob.name}` : 'Job'}
                    visible={detailOpen} style={{ width: '92vw', maxWidth: 1300 }}
                    onHide={() => setDetailOpen(false)} dismissableMask>
                {!detailJob ? (
                    <div style={{ padding: 20, textAlign: 'center' }}>
                        <i className="pi pi-spin pi-spinner" style={{ fontSize: '2rem' }} />
                        <p style={{ color: '#666' }}>Loading job…</p>
                    </div>
                ) : (
                    <>
                        {/* Status row */}
                        <div className="row" style={{ gap: 6, marginBottom: 10 }}>
                            <Tag severity={(STATE_BADGE[detailJob.state] || {}).severity}
                                 value={detailJob.state} />
                            {detailJob.thread_alive
                                ? <Tag severity="info" value="thread alive" />
                                : <Tag severity="secondary" value="no live thread" />}
                            {detailJob.chain_run_id && (
                                <Tag severity="info"
                                     value={`run_id ${String(detailJob.chain_run_id).slice(0, 10)}…`}
                                     title={String(detailJob.chain_run_id)} />
                            )}
                            {detailJob.rows_root && (
                                <Tag severity="success"
                                     value={`sealed · root ${detailJob.rows_root.slice(0, 8)}…`}
                                     title={detailJob.rows_root} />
                            )}
                        </div>

                        {detailJob.error && (
                            <div className="err" style={{ marginBottom: 10 }}>⚠ {detailJob.error}</div>
                        )}

                        {/* Progress + control buttons */}
                        <div className="row" style={{ alignItems: 'center', marginBottom: 10 }}>
                            <div style={{ flex: 1 }}>
                                <ProgressBarStatic
                                    state={detailJob.state}
                                    pct={detailJob.total_cells
                                        ? Math.floor(100 * (detailJob.done_cells || 0) / detailJob.total_cells)
                                        : 0}
                                    height={14} />
                                <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>
                                    {detailJob.done_cells || 0} / {detailJob.total_cells || 0} cells
                                    {detailJob.state === 'completed' && <span style={{ color: '#22863a', marginLeft: 6 }}>✓ done</span>}
                                    {detailJob.state === 'cancelled' && <span style={{ color: '#999',   marginLeft: 6 }}>cancelled</span>}
                                    {detailJob.state === 'error'     && <span style={{ color: '#d73a49',marginLeft: 6 }}>error</span>}
                                    {!TERMINAL_STATES.has(detailJob.state) && detailJob.current_paper &&
                                        ` · currently scoring paper ${detailJob.current_paper} / ${detailJob.current_criterion}`}
                                </div>
                            </div>
                            <div className="row" style={{ gap: 6 }}>
                                {detailJob.state === 'running' && <Button label="Pause"  icon="pi pi-pause" severity="warning" onClick={() => pauseJob(detailJob.job_id)} />}
                                {(detailJob.state === 'paused' || detailJob.state === 'interrupted' || detailJob.state === 'error') &&
                                    <Button label="Resume" icon="pi pi-play"  severity="success" onClick={() => resumeJob(detailJob.job_id)} />}
                                {!TERMINAL_STATES.has(detailJob.state) &&
                                    <Button label="Cancel" icon="pi pi-times" severity="danger" outlined onClick={() => cancelJob(detailJob.job_id)} />}
                                <Button label="Download CSV" icon="pi pi-download" severity="secondary"
                                        onClick={() => downloadCSV(detailJob.job_id)}
                                        disabled={!(detailJob.done_cells || 0)} />
                            </div>
                        </div>

                        {/* Chain boundary transactions */}
                        <Panel header="Chain receipts" toggleable>
                            <div className="row" style={{ gap: 16, flexWrap: 'wrap' }}>
                                <div>
                                    <strong>startrun:</strong>{' '}
                                    {detailJob.chain_startrun_trx
                                        ? <Button className="p-button-link p-button-sm" style={{ padding: 0, fontSize: 12 }}
                                                  onClick={() => viewTransaction(detailJob.chain_startrun_trx)}>
                                            <code>{detailJob.chain_startrun_trx.slice(0, 10)}…</code></Button>
                                        : <span style={{ color: '#999' }}>—</span>}
                                </div>
                                <div>
                                    <strong>sealrun:</strong>{' '}
                                    {detailJob.chain_sealrun_trx
                                        ? <Button className="p-button-link p-button-sm" style={{ padding: 0, fontSize: 12 }}
                                                  onClick={() => viewTransaction(detailJob.chain_sealrun_trx)}>
                                            <code>{detailJob.chain_sealrun_trx.slice(0, 10)}…</code></Button>
                                        : <span style={{ color: '#999' }}>—</span>}
                                </div>
                                <div>
                                    <strong>rows_root:</strong>{' '}
                                    {detailJob.rows_root
                                        ? <code style={{ fontSize: 12 }}>{detailJob.rows_root}</code>
                                        : <span style={{ color: '#999' }}>—</span>}
                                </div>
                            </div>
                        </Panel>

                        {/* Aggregated ranking */}
                        {aggregated.length > 0 && (
                            <Panel header={`Ranking (${aggregated.length} papers)`} toggleable
                                   style={{ marginTop: 10 }}>
                                <DataTable value={aggregated} size="small" scrollable scrollHeight="200px"
                                           paginator rows={10}>
                                    <Column field="paper_id" header="Paper" sortable style={{ width: 100 }} />
                                    <Column field="avg" header="Avg score" sortable style={{ width: 110 }} />
                                    <Column field="count" header="#crit" style={{ width: 80 }} />
                                </DataTable>
                            </Panel>
                        )}

                        {/* Per-cell scores */}
                        <Panel header={`Cells (${detailCells.length})`} toggleable
                               style={{ marginTop: 10 }}>
                            <DataTable value={detailCells} size="small" paginator rows={25}
                                       scrollable scrollHeight="320px"
                                       emptyMessage="No cells scored yet.">
                                <Column field="paper_id"     header="Paper"     sortable style={{ width: 90 }} />
                                <Column field="criterion_id" header="Criterion" sortable style={{ width: 130 }} />
                                <Column field="score"        header="Score"     sortable style={{ width: 80 }}
                                        body={(r) => r.score == null ? <Tag value="?" severity="warning" /> : r.score} />
                                <Column field="source"       header="Src"       style={{ width: 80 }} />
                                <Column field="justification" header="Justification" />
                                <Column field="evidence"     header="Evidence"
                                        body={(r) => <span style={{ fontStyle: 'italic' }}>{r.evidence}</span>} />
                                <Column header="Chain" style={{ width: 130 }} body={(r) =>
                                    r.payload_hash
                                        ? <Button className="p-button-link p-button-sm" style={{ padding: 0, fontSize: 11 }}
                                                  onClick={() => { /* per-cell trx ids are not stored on jobs; lookup via Mongo if needed */
                                                      // The cell document has no per-cell trx_id field in the schema we mirror to Mongo;
                                                      // viewing per-cell requires querying the chain by payload_hash, which is future work.
                                                  }}
                                                  tooltip={r.payload_hash}>
                                            <code>hash {r.payload_hash.slice(0, 8)}…</code>
                                          </Button>
                                        : <span style={{ color: '#999', fontSize: 11 }}>—</span>} />
                            </DataTable>
                        </Panel>
                    </>
                )}
            </Dialog>

            {/* ──────────────── Blockchain transaction viewer ──────────────── */}
            <Dialog header="Blockchain Transaction"
                    visible={txDialogVisible} style={{ width: '820px' }}
                    onHide={() => { setTxDialogVisible(false); setTxData(null); setTxError(null); }}
                    dismissableMask>
                {txLoading ? (
                    <div style={{ textAlign: 'center', padding: 32 }}>
                        <i className="pi pi-spin pi-spinner" style={{ fontSize: '2rem' }} />
                        <p style={{ marginTop: 12, color: '#666' }}>Loading transaction…</p>
                    </div>
                ) : (
                    <div>
                        <div style={{ marginBottom: 12, fontFamily: 'monospace', fontSize: 12 }}>
                            <strong>Transaction ID:</strong>{' '}
                            <code style={{ wordBreak: 'break-all' }}>{txTrxId}</code>{' '}
                            <Button icon="pi pi-copy" size="small" text
                                    onClick={() => copyToClipboard(txTrxId)} tooltip="Copy trx id" />
                        </div>
                        {txError && (
                            <div className="err" style={{ marginBottom: 12 }}>⚠ {txError}</div>
                        )}
                        {txData && (
                            <>
                                <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
                                    <Tag severity={txData.irreversible ? 'success' : 'warning'}
                                         value={txData.irreversible ? '✓ Irreversible' : '○ Pending'} />
                                    {txData.data?.trace?.status && (
                                        <Tag severity="info" value={`status: ${txData.data.trace.status}`} />
                                    )}
                                    {txData.data?.trace?.block_num && (
                                        <Tag value={`block #${txData.data.trace.block_num}`} />
                                    )}
                                </div>
                                <Divider />
                                {Array.isArray(txData.data?.trace?.action_traces) && (
                                    <Panel header="Actions" toggleable>
                                        {txData.data.trace.action_traces.map((at, i) => (
                                            <div key={i} style={{ marginBottom: 12, paddingBottom: 12,
                                                                  borderBottom: '1px dashed #ddd' }}>
                                                <div style={{ fontSize: 13 }}>
                                                    <strong>Account:</strong> <code>{at.act?.account}</code>{' · '}
                                                    <strong>Action:</strong> <code>{at.act?.name}</code>
                                                </div>
                                                <pre style={{ background: '#f6f8fa', padding: 8, borderRadius: 4,
                                                              fontSize: 11, marginTop: 6, maxHeight: 220, overflow: 'auto' }}>
{JSON.stringify(at.act?.data || at.act, null, 2)}
                                                </pre>
                                            </div>
                                        ))}
                                    </Panel>
                                )}
                                <Panel header="Raw JSON" toggleable collapsed style={{ marginTop: 12 }}>
                                    <pre style={{ background: '#f6f8fa', padding: 8, borderRadius: 4,
                                                  fontSize: 11, maxHeight: 320, overflow: 'auto' }}>
{JSON.stringify(txData, null, 2)}
                                    </pre>
                                </Panel>
                            </>
                        )}
                    </div>
                )}
            </Dialog>
        </div>
    );
};

export default ScorePapersBat;
