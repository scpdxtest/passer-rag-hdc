import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import axios from 'axios';
import { InputText } from 'primereact/inputtext';
import { Button } from 'primereact/button';
import { ProgressBar } from 'primereact/progressbar';
import { DataTable } from 'primereact/datatable';
import { Column } from 'primereact/column';
import { Tag } from 'primereact/tag';
import { Dropdown } from 'primereact/dropdown';
import { Dialog } from 'primereact/dialog';
import configuration from './configuration.json';
import './dbFromCorpusPapers.css';

const CFG = (configuration && configuration.passer) || {};
// When the React app is loaded from a developer's machine (localhost),
// call the local ingestion API. When the bundle is served from any other
// host (production deployment, LAN, etc.), call the remote IngestAPI on
// 195.230.127.227 — 127.0.0.1 from a remote machine refers to *that*
// machine's loopback, not the developer's. localStorage('ingestAPI')
// still wins per the useState() below if a manual override is set.
const IS_LOCAL_HOST = typeof window !== 'undefined'
    && ['localhost', '127.0.0.1', '0.0.0.0'].includes(window.location.hostname);
const DEFAULT_INGEST_API = IS_LOCAL_HOST
    ? (CFG.IngestAPI       || 'http://127.0.0.1:8010')
    : (CFG.IngestAPIRemote || 'http://195.230.127.227:8010');
const CHROMA_URLS = (CFG.Chroma || []).map((c) => c.url);
const OLLAMA_URLS = (CFG.Ollama || []).map((o) => o.url);
const DEFAULT_CHROMA = CHROMA_URLS[0] || 'http://127.0.0.1:8000';
const DEFAULT_OLLAMA = OLLAMA_URLS[0] || 'http://127.0.0.1:11434';

// PrimeReact's <ProgressBar> renders a CSS shimmer/gradient overlay that
// keeps animating even when value === 100. After ingestion finishes the
// bar would flash forever; for that case we render a flat coloured div
// instead. Same shape pattern as scorePapersBat.js' ProgressBarStatic.
function StaticBar({ pct, color = '#2196f3', height = 18 }) {
    const p = Math.max(0, Math.min(100, pct || 0));
    return (
        <div style={{ height, background: '#e0e0e0', borderRadius: 4,
                      overflow: 'hidden', position: 'relative' }}>
            <div style={{ width: `${p}%`, height: '100%',
                          background: color, borderRadius: 4,
                          transition: 'width 0.25s' }} />
        </div>
    );
}

const DBFromCorpusPapers = () => {
    const [ingestAPI, setIngestAPI] = useState(localStorage.getItem('ingestAPI') || DEFAULT_INGEST_API);
    const [corpusDir, setCorpusDir] = useState(localStorage.getItem('corpusDir') || 'newRAG/corpus');
    const [chromaUrl, setChromaUrl] = useState(localStorage.getItem('selectedChromaDB') || DEFAULT_CHROMA);
    const [ollamaUrl, setOllamaUrl] = useState(localStorage.getItem('selectedOllama') || DEFAULT_OLLAMA);
    const [collection, setCollection] = useState(localStorage.getItem('papersCollection') || 'papers_corpus');
    const [embedModel, setEmbedModel] = useState(
        localStorage.getItem('papersEmbedModel')
        || localStorage.getItem('selectedLLMModel')
        || 'mistral'
    );
    // Phase 5k: a generative LLM is needed (in addition to the embedder)
    // when the chosen profile generates per-chapter synopses. The model
    // name reuses the same conversational model the rest of the app
    // already uses (defaults to selectedLLMModel) so users don't have to
    // configure two unrelated dropdowns.
    const [llmModel, setLlmModel] = useState(
        localStorage.getItem('synopsisLLMModel')
        || localStorage.getItem('selectedLLMModel')
        || 'mistral'
    );
    // Phase 5h+: for first-person novels, the coref pass needs the
    // protagonist's name in order to substitute I/me/my with it outside
    // dialogue. Empty string is fine (third-person coref still runs)
    // but quality on questions about the protagonist himself suffers.
    const [protagonistName, setProtagonistName] = useState(
        localStorage.getItem('corefProtagonistName') || ''
    );
    const [chunkSize, setChunkSize] = useState(800);
    const [overlap, setOverlap] = useState(120);
    const [limit, setLimit] = useState('');
    // Corpus profile — see newRAG/corpus_profiles.py and
    // newRAG/RAG_GENERALIZATION.md. The available profiles are fetched
    // from the worker's /profiles endpoint on mount. Default is
    // "academic_paper", which reproduces v1 behaviour exactly.
    const [profile, setProfile] = useState(localStorage.getItem('papersProfile') || 'academic_paper');
    const [profiles, setProfiles] = useState([]);     // [{name, chunk_size, chunk_overlap, section_names, exclude_from_default_retrieval, ...}]

    const [serverOK, setServerOK] = useState(null);
    const [status, setStatus] = useState({ running: false, total: 0, done: 0, errors: 0, last_file: '', error: null, results: [], chunk_done: 0, chunk_total: 0, synopsis_done: 0, synopsis_total: 0, coref_done: 0, coref_total: 0 });
    const [papers, setPapers] = useState([]);
    const [busy, setBusy] = useState(false);
    const [collInfo, setCollInfo] = useState(null);
    const [browseOpen, setBrowseOpen] = useState(false);
    const [browseInfo, setBrowseInfo] = useState({ path: '', subdirs: [], pdf_count: 0, parent: null, cwd: '' });
    const [browseLoading, setBrowseLoading] = useState(false);
    const [finishSummary, setFinishSummary] = useState(null);
    // Live probe of the typed corpus directory: tells the user "✓ 17 PDFs
    // found" / "⚠ no PDFs here" / "⚠ path not found" without having to
    // open the browse dialog. Debounced so we don't hammer /list_dir on
    // every keystroke.
    const [corpusProbe, setCorpusProbe] = useState({ loading: false, pdfCount: null, error: null });
    const pollRef = useRef(null);
    const prevRunningRef = useRef(false);

    const speak = (text) => {
        try {
            const u = new SpeechSynthesisUtterance(text);
            window.speechSynthesis.speak(u);
        } catch (_) { /* no-op */ }
    };

    const formatDuration = (sec) => {
        if (!sec || sec < 0) return '—';
        const m = Math.floor(sec / 60), s = Math.round(sec % 60);
        return m > 0 ? `${m}m ${s}s` : `${s}s`;
    };

    const checkServer = useCallback(async () => {
        try {
            const r = await axios.get(ingestAPI.replace(/\/$/, '') + '/health', { timeout: 3000 });
            setServerOK(!!r.data.ok);
        } catch (e) {
            setServerOK(false);
        }
    }, [ingestAPI]);

    // Fetch the list of registered corpus profiles from the worker.
    // Each entry carries chunk_size / chunk_overlap defaults; selecting
    // a profile auto-fills those inputs (the user can still override).
    const fetchProfiles = useCallback(async () => {
        try {
            const r = await axios.get(ingestAPI.replace(/\/$/, '') + '/profiles', { timeout: 5000 });
            const list = r.data?.profiles || [];
            setProfiles(list);
            // If the persisted profile isn't in the list, fall back to default
            if (!list.find(p => p.name === profile)) {
                setProfile(r.data?.default || 'academic_paper');
            }
        } catch (_) {
            // Old workers without /profiles — silently default
            setProfiles([{ name: 'academic_paper',
                           chunk_size: 800, chunk_overlap: 120,
                           section_names: [], exclude_from_default_retrieval: [] }]);
        }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [ingestAPI]);

    // When the selected profile changes, refresh the chunk/overlap
    // defaults from the profile (user can still override afterwards).
    const onProfileChange = (newName) => {
        setProfile(newName);
        localStorage.setItem('papersProfile', newName);
        const p = profiles.find(x => x.name === newName);
        if (p) {
            if (p.chunk_size)    setChunkSize(String(p.chunk_size));
            if (p.chunk_overlap) setOverlap(String(p.chunk_overlap));
        }
    };

    // Helper for the dropdown options (passed to PrimeReact Dropdown).
    const profileOptions = useMemo(() => profiles.map(p => ({
        label: p.name === 'academic_paper'
                ? `academic_paper — scientific papers (default; IMRaD sections, references excluded)`
                : p.name === 'novel'
                ? `novel — long-form fiction (chapters / parts / appendices)`
                : p.name === 'manual'
                ? `manual — technical / workbook (hierarchical sections, procedures)`
                : p.name,
        value: p.name,
    })), [profiles]);

    const currentProfile = useMemo(() =>
        profiles.find(p => p.name === profile) || null,
    [profiles, profile]);

    const pollStatus = useCallback(async () => {
        try {
            const r = await axios.get(ingestAPI.replace(/\/$/, '') + '/status', { timeout: 5000 });
            const s = r.data || {};
            setStatus(s);
            // detect a true → false transition (run just finished)
            if (prevRunningRef.current && s.running === false) {
                const duration = (s.finished_ts && s.start_ts) ? (s.finished_ts - s.start_ts) : null;
                setFinishSummary({
                    done: s.done, total: s.total, errors: s.errors,
                    duration, error: s.error,
                    finishedAt: new Date().toLocaleTimeString(),
                    chain_logimport: s.chain_logimport,
                    chain_logimport_tx: s.chain_logimport_tx,
                    chain_logimport_msg: s.chain_logimport_msg,
                });
                speak(s.error
                    ? 'Ingestion finished with errors'
                    : `Ingestion finished. ${s.done} of ${s.total} papers.`);
                // Phase 5n: the chain logimport is now fired by the
                // worker itself at end of run_ingestion (see
                // newRAG/ingest_corpus.py). The previous client-side
                // POST here would have left the audit log empty whenever
                // the browser tab was closed during a long run; moving
                // it to the worker removes that dependency. The result
                // is surfaced in s.chain_logimport ("ok" / "error" /
                // "skipped") for the UI to display.
            }
            prevRunningRef.current = !!s.running;
            if (s.running === false && pollRef.current) {
                clearInterval(pollRef.current);
                pollRef.current = null;
                setBusy(false);
            }
        } catch (e) {
            // server gone; stop polling
            if (pollRef.current) {
                clearInterval(pollRef.current);
                pollRef.current = null;
            }
            setBusy(false);
        }
    }, [ingestAPI]);

    const browseAt = useCallback(async (path) => {
        setBrowseLoading(true);
        try {
            const r = await axios.get(ingestAPI.replace(/\/$/, '') + '/list_dir', {
                params: path ? { path } : {},
                timeout: 10000,
            });
            setBrowseInfo(r.data || { path, subdirs: [], pdf_count: 0, parent: null });
        } catch (e) {
            alert('Browse failed: ' + (e.response?.data?.error || e.message));
        } finally {
            setBrowseLoading(false);
        }
    }, [ingestAPI]);

    const openBrowser = () => {
        setBrowseOpen(true);
        browseAt(corpusDir || '');
    };

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

    useEffect(() => {
        checkServer();
        fetchProfiles();
        // Initial /status probe: ONLY adopt the worker's state if a job is
        // actively running (e.g. another tab kicked one off, or the user
        // reloaded mid-ingest). For an idle/completed worker we leave the
        // initial empty status untouched so a reload doesn't show the
        // previous run's "done X/Y" panel.
        (async () => {
            try {
                const r = await axios.get(ingestAPI.replace(/\/$/, '') + '/status', { timeout: 5000 });
                const s = r.data || {};
                if (s.running) {
                    setStatus(s);
                    prevRunningRef.current = true;
                    setBusy(true);
                    if (!pollRef.current) {
                        pollRef.current = setInterval(pollStatus, 1500);
                    }
                }
            } catch (_) { /* checkServer surfaces unreachable state */ }
        })();
        return () => {
            if (pollRef.current) clearInterval(pollRef.current);
        };
    }, [checkServer, pollStatus, fetchProfiles, ingestAPI]);

    useEffect(() => { probeCollection(); }, [probeCollection]);

    // Live PDF-count probe for the corpus directory input. Debounced
    // 400 ms so we don't hammer /list_dir while the user is typing.
    // Empty path or unreachable server -> clear the probe display.
    useEffect(() => {
        if (!corpusDir || !serverOK) {
            setCorpusProbe({ loading: false, pdfCount: null, error: null });
            return undefined;
        }
        const t = setTimeout(async () => {
            setCorpusProbe(p => ({ ...p, loading: true }));
            try {
                const r = await axios.get(ingestAPI.replace(/\/$/, '') + '/list_dir', {
                    params: { path: corpusDir },
                    timeout: 5000,
                });
                setCorpusProbe({
                    loading: false,
                    pdfCount: r.data?.pdf_count ?? 0,
                    error: null,
                });
            } catch (e) {
                setCorpusProbe({
                    loading: false,
                    pdfCount: null,
                    error: e.response?.data?.error || 'path not found',
                });
            }
        }, 400);
        return () => clearTimeout(t);
    }, [corpusDir, serverOK, ingestAPI]);

    const startIngest = async () => {
        if (collInfo && collInfo.exists && collInfo.embed_model
            && collInfo.embed_model !== embedModel) {
            const ok = window.confirm(
                `Collection "${collection}" was built with embed_model="${collInfo.embed_model}", ` +
                `but you are about to ingest with "${embedModel}". ` +
                `The server will refuse this — change the embedding model to "${collInfo.embed_model}" ` +
                `or use a different collection name. Continue anyway (it will fail)?`
            );
            if (!ok) return;
        }
        setFinishSummary(null);
        prevRunningRef.current = true; // arm finish-detection for this run
        setBusy(true);
        try {
            const body = {
                corpus_dir: corpusDir,
                chroma_url: chromaUrl,
                ollama_url: ollamaUrl,
                collection,
                embed_model: embedModel,
                profile,                                        // ← new
                chunk_size: Number(chunkSize) || 0,            // 0 → worker uses profile default
                overlap:    Number(overlap)    || 0,
            };
            // The LLM is needed for synopsis generation AND/OR
            // coreference resolution. Either one triggers sending the
            // model. Profiles without LLM passes (academic_paper) get
            // a clean payload without these fields.
            if (currentProfile?.synopsize || currentProfile?.coref) {
                body.llm_model = llmModel;
                body.llm_url   = ollamaUrl;
            }
            // First-person attribution. Only meaningful when coref runs;
            // otherwise the worker ignores it.
            if (currentProfile?.coref && protagonistName.trim()) {
                body.protagonist_name = protagonistName.trim();
            }
            // Phase 5n: analyst name for the server-side chain
            // logimport call (worker fires it at end of run_ingestion,
            // not the React client anymore). Same localStorage key the
            // old client-side call used so existing users don't lose
            // their attribution.
            body.analyst = localStorage.getItem('wharf_user_name') || 'anonymous';
            const lim = parseInt(limit, 10);
            if (!isNaN(lim) && lim > 0) body.limit = lim;
            localStorage.setItem('papersCollection', collection);
            localStorage.setItem('papersEmbedModel', embedModel);
            localStorage.setItem('synopsisLLMModel', llmModel);
            localStorage.setItem('papersProfile',    profile);
            localStorage.setItem('corpusDir', corpusDir);
            localStorage.setItem('ingestAPI', ingestAPI);
            await axios.post(ingestAPI.replace(/\/$/, '') + '/start', body, { timeout: 10000 });
            if (pollRef.current) clearInterval(pollRef.current);
            pollRef.current = setInterval(pollStatus, 1500);
        } catch (e) {
            alert('Could not start ingestion: ' + (e.response?.data?.error || e.message));
            setBusy(false);
        }
    };

    const stopIngest = async () => {
        try {
            await axios.post(ingestAPI.replace(/\/$/, '') + '/stop', {}, { timeout: 5000 });
        } catch (e) {
            // ignore
        }
    };

    const dropCollection = async () => {
        if (!collection) return;
        const ok = window.confirm(
            `Permanently delete collection "${collection}" from ChromaDB at ${chromaUrl}?\n` +
            `This is the recommended recovery if the worker reports KeyError('_type').`
        );
        if (!ok) return;
        try {
            const r = await axios.post(ingestAPI.replace(/\/$/, '') + '/delete_collection',
                { chroma_url: chromaUrl, collection }, { timeout: 15000 });
            alert(r.data?.deleted ? `Dropped "${collection}".` : `Did not delete: ${JSON.stringify(r.data)}`);
            await probeCollection();
        } catch (e) {
            alert('Drop failed: ' + (e.response?.data?.error || e.message));
        }
    };

    const listPapers = async () => {
        try {
            const r = await axios.get(ingestAPI.replace(/\/$/, '') + '/papers', {
                params: { chroma: chromaUrl, collection },
                timeout: 30000,
            });
            setPapers(r.data?.papers || []);
        } catch (e) {
            alert('Could not list papers: ' + (e.response?.data?.error || e.message));
        }
    };

    const pct = status.total ? Math.floor((status.done / status.total) * 100) : 0;
    const recent = (status.results || []).slice(-25).reverse();

    return (
        <div className="dbFromCorpusPapers">
            <h2>📚 Build Vectorstore from Paper Corpus</h2>

            <div className="grid-2col">
                <div className="card">
                    <h3>🛠 Ingestion Worker</h3>
                    <label>Ingestion API URL</label>
                    <div className="row">
                        <InputText value={ingestAPI} onChange={(e) => setIngestAPI(e.target.value)} style={{ flex: 1 }} />
                        <Button label="Check" size="small" onClick={checkServer} />
                    </div>
                    <div style={{ marginTop: 6 }}>
                        Server: {serverOK === null ? <Tag value="checking…" /> :
                            serverOK ? <Tag severity="success" value="reachable" /> :
                            <Tag severity="danger" value="unreachable" />}
                    </div>
                    <pre className="help">
{`Start it with:
  pip install pdfplumber pypdf chromadb flask flask-cors requests
  python newRAG/ingest_corpus.py
(default port 8010)`}
                    </pre>
                </div>

                <div className="card">
                    <h3>🗄 Targets</h3>
                    <label>ChromaDB URL</label>
                    {CHROMA_URLS.length > 1 ? (
                        <Dropdown
                            value={chromaUrl}
                            options={CHROMA_URLS}
                            editable
                            onChange={(e) => { setChromaUrl(e.value); localStorage.setItem('selectedChromaDB', e.value); }}
                        />
                    ) : (
                        <InputText value={chromaUrl} onChange={(e) => { setChromaUrl(e.target.value); localStorage.setItem('selectedChromaDB', e.target.value); }} />
                    )}
                    <label>Ollama URL</label>
                    {OLLAMA_URLS.length > 1 ? (
                        <Dropdown
                            value={ollamaUrl}
                            options={OLLAMA_URLS}
                            editable
                            onChange={(e) => { setOllamaUrl(e.value); localStorage.setItem('selectedOllama', e.value); }}
                        />
                    ) : (
                        <InputText value={ollamaUrl} onChange={(e) => { setOllamaUrl(e.target.value); localStorage.setItem('selectedOllama', e.target.value); }} />
                    )}
                    <label>Embedding Model (defaults to the app's selected LLM)</label>
                    <InputText
                        value={embedModel}
                        onChange={(e) => { setEmbedModel(e.target.value); localStorage.setItem('papersEmbedModel', e.target.value); }}
                    />
                    <label>Corpus profile (what kind of documents?)</label>
                    <Dropdown
                        value={profile}
                        options={profileOptions}
                        onChange={(e) => onProfileChange(e.value)}
                        placeholder="Loading profiles…"
                        style={{ width: '100%' }}
                    />
                    {currentProfile && (
                        <div className="hint" style={{ marginTop: 4, fontSize: 11 }}>
                            <strong>Detects:</strong>{' '}
                            <code style={{ fontSize: 10 }}>
                                {(currentProfile.section_names || []).join(', ') || '(no section patterns)'}
                            </code>
                            {currentProfile.exclude_from_default_retrieval &&
                             currentProfile.exclude_from_default_retrieval.length > 0 && (
                                <>
                                    {' · '}<strong>Excluded from default retrieval:</strong>{' '}
                                    <code style={{ fontSize: 10 }}>
                                        {currentProfile.exclude_from_default_retrieval.join(', ')}
                                    </code>
                                </>
                            )}
                            {' · '}<strong>Default chunk:</strong>{' '}
                            <code style={{ fontSize: 10 }}>
                                {currentProfile.chunk_size}/{currentProfile.chunk_overlap}
                            </code>
                            {currentProfile.synopsize && (
                                <>
                                    {' · '}<strong>Synopses:</strong>{' '}
                                    <code style={{ fontSize: 10 }}>
                                        {(currentProfile.synopsize_sections || []).join(', ') || '(all sections)'}
                                    </code>
                                </>
                            )}
                            {currentProfile.coref && (
                                <>
                                    {' · '}<strong style={{ color: '#b65c00' }}>Coref: on</strong>
                                    <span style={{ color: '#888' }}>
                                        {' '}(per-chunk pronoun rewrite — slow, large LLM cost)
                                    </span>
                                </>
                            )}
                        </div>
                    )}
                    {(currentProfile?.synopsize || currentProfile?.coref) && (
                        <>
                            <label style={{ marginTop: 6 }}>
                                LLM model
                                <span style={{ color: '#888', fontWeight: 'normal', marginLeft: 6 }}>
                                    (used for{' '}
                                    {[currentProfile?.synopsize && 'synopses',
                                      currentProfile?.coref && 'coref'].filter(Boolean).join(' + ')}
                                    ; idempotent on re-ingest)
                                </span>
                            </label>
                            <InputText
                                value={llmModel}
                                onChange={(e) => { setLlmModel(e.target.value); localStorage.setItem('synopsisLLMModel', e.target.value); }}
                                placeholder="e.g. mistral:latest"
                            />
                        </>
                    )}
                    {currentProfile?.coref && (
                        <>
                            <label style={{ marginTop: 6 }}>
                                Protagonist name (for first-person novels)
                                <span style={{ color: '#888', fontWeight: 'normal', marginLeft: 6 }}>
                                    (substitutes I/me/my with this name outside dialogue; leave blank for third-person novels)
                                </span>
                            </label>
                            <InputText
                                value={protagonistName}
                                onChange={(e) => { setProtagonistName(e.target.value); localStorage.setItem('corefProtagonistName', e.target.value); }}
                                placeholder="e.g. John"
                            />
                        </>
                    )}
                    <label>Collection Name</label>
                    <InputText value={collection} onChange={(e) => setCollection(e.target.value)} />
                </div>
            </div>

            <div className="card">
                <h3>📂 Corpus & Chunking</h3>
                <div className="grid-4col">
                    <div>
                        <label>Corpus directory (server-side path)</label>
                        <div className="row">
                            <InputText
                                value={corpusDir}
                                onChange={(e) => setCorpusDir(e.target.value)}
                                placeholder="e.g. newRAG/corpus or /absolute/path/to/pdfs"
                                style={{ flex: 1 }}
                            />
                            <Button
                                label="Browse…"
                                icon="pi pi-folder-open"
                                size="small"
                                onClick={openBrowser}
                                disabled={!serverOK}
                            />
                        </div>
                        {/* Live PDF-count feedback so the user knows the path
                            is valid before clicking Start (no need to open
                            the browse dialog). */}
                        <div style={{ marginTop: 4, fontSize: 12, minHeight: 16 }}>
                            {!serverOK
                                ? <span style={{ color: '#888' }}>worker offline — can't probe path</span>
                                : !corpusDir
                                    ? <span style={{ color: '#888' }}>enter or browse to a directory</span>
                                    : corpusProbe.loading
                                        ? <span style={{ color: '#888' }}>checking…</span>
                                        : corpusProbe.error
                                            ? <span style={{ color: '#d73a49' }}>⚠ {corpusProbe.error}</span>
                                            : corpusProbe.pdfCount === 0
                                                ? <span style={{ color: '#d4a017' }}>⚠ no PDFs in this directory</span>
                                                : corpusProbe.pdfCount !== null
                                                    ? <span style={{ color: '#22863a' }}>✓ {corpusProbe.pdfCount} PDF{corpusProbe.pdfCount === 1 ? '' : 's'} found</span>
                                                    : null}
                        </div>
                    </div>
                    <div>
                        <label>Chunk size</label>
                        <InputText value={chunkSize} onChange={(e) => setChunkSize(e.target.value)} />
                    </div>
                    <div>
                        <label>Overlap</label>
                        <InputText value={overlap} onChange={(e) => setOverlap(e.target.value)} />
                    </div>
                    <div>
                        <label>Limit (optional, for testing)</label>
                        <InputText value={limit} onChange={(e) => setLimit(e.target.value)} placeholder="e.g. 10" />
                    </div>
                </div>

                <div className="row" style={{ marginTop: 12 }}>
                    <Button
                        label={busy || status.running ? 'Running…' : 'Start Ingestion'}
                        icon="pi pi-play"
                        onClick={startIngest}
                        disabled={busy || status.running || !serverOK}
                    />
                    <Button
                        label="Stop"
                        icon="pi pi-stop"
                        severity="danger"
                        onClick={stopIngest}
                        disabled={!status.running}
                    />
                    <Button
                        label="List indexed papers"
                        icon="pi pi-list"
                        severity="secondary"
                        onClick={listPapers}
                    />
                    <Button
                        label="Drop collection"
                        icon="pi pi-trash"
                        severity="warning"
                        outlined
                        onClick={dropCollection}
                        disabled={!collInfo || !collInfo.exists}
                        tooltip="Recovery for chromadb KeyError('_type')"
                    />
                </div>
            </div>

            {collInfo && collInfo.exists && (() => {
                const collProfile = collInfo.metadata?.profile;
                const embedMismatch   = collInfo.embed_model && collInfo.embed_model !== embedModel;
                const profileMismatch = collProfile          && collProfile          !== profile;
                const anyMismatch = embedMismatch || profileMismatch;
                return (
                    <div className={`card embedGuard ${anyMismatch ? 'mismatch' : 'match'}`}>
                        <strong>Collection check:</strong> "{collection}" exists with{' '}
                        <code>embed_model = {collInfo.embed_model || '(untagged)'}</code>,{' '}
                        <code>profile = {collProfile || '(untagged)'}</code>,{' '}
                        <code>{collInfo.count} chunks</code>.
                        {embedMismatch && (
                            <div className="err" style={{ marginTop: 8 }}>
                                ⚠ Embedding Model is <code>{embedModel}</code>. Ingestion will be
                                refused — vectors would live in a different space.
                                Either set Embedding Model to <code>{collInfo.embed_model}</code> or use
                                a new collection name (e.g. <code>{collection}__{embedModel.replace(/:/g,'_')}</code>).
                            </div>
                        )}
                        {profileMismatch && (
                            <div className="err" style={{ marginTop: 8 }}>
                                ⚠ Profile is <code>{profile}</code> but the collection was built with{' '}
                                <code>{collProfile}</code>. Section semantics and card recipes differ
                                across profiles, so ingestion will be refused. Either switch the
                                profile to <code>{collProfile}</code> or use a new collection name
                                (e.g. <code>{collection}__{profile}</code>).
                            </div>
                        )}
                    </div>
                );
            })()}

            <div className={`card ${(!status.running && status.done > 0) ? 'progressDone' : ''}`}>
                <h3>📈 Progress {!status.running && status.done > 0 && pct === 100 && <Tag severity="success" value="✓ Done" style={{ marginLeft: 8 }} />}</h3>
                <div className="row" style={{ alignItems: 'center' }}>
                    <div style={{ flex: 1 }}>
                        {/* PrimeReact's <ProgressBar> animates a shimmer
                            even at value=100 — visually it looks like the
                            run is still ongoing. While running we keep
                            the animated bar (it's the "alive" cue); once
                            the run completes we swap to a flat coloured
                            div so the UI quiets down. */}
                        {status.running
                            ? <ProgressBar value={pct} />
                            : <StaticBar pct={pct} color={pct === 100 ? '#22863a' : '#2196f3'} height={20} />}
                    </div>
                    <span style={{ marginLeft: 12 }}>{status.done}/{status.total} files ({pct}%)</span>
                </div>
                {/* Chunk-grain progress for the file currently being embedded.
                    Without this, a single-PDF ingestion sits at 0/1 for the
                    entire run with no feedback. */}
                {status.running && status.chunk_total > 0 && (() => {
                    const cpct = Math.floor((status.chunk_done / status.chunk_total) * 100);
                    return (
                        <div className="row" style={{ alignItems: 'center', marginTop: 8 }}>
                            <div style={{ flex: 1 }}>
                                <ProgressBar value={cpct} showValue={false} style={{ height: 12 }} />
                            </div>
                            <span style={{ marginLeft: 12, fontSize: 13 }}>
                                chunk {status.chunk_done}/{status.chunk_total} of current file ({cpct}%)
                            </span>
                        </div>
                    );
                })()}
                {/* Phase 5k: synopsis pass is its own sub-progress. Each
                    matching chapter triggers one Ollama generate call,
                    which is much slower than embed_text — surfacing it
                    separately keeps users from thinking the run hung. */}
                {status.running && status.synopsis_total > 0 && (() => {
                    const spct = Math.floor((status.synopsis_done / status.synopsis_total) * 100);
                    return (
                        <div className="row" style={{ alignItems: 'center', marginTop: 6 }}>
                            <div style={{ flex: 1 }}>
                                <ProgressBar value={spct} showValue={false} style={{ height: 10 }} />
                            </div>
                            <span style={{ marginLeft: 12, fontSize: 13 }}>
                                synopsis {status.synopsis_done}/{status.synopsis_total} ({spct}%)
                            </span>
                        </div>
                    );
                })()}
                {/* Phase 5h: coref is one LLM call PER CHUNK (typically
                    thousands per novel), so its bar moves slowest of
                    all and dominates wall-clock. Distinct colour cue
                    so users can see at a glance "we're in the long
                    phase". */}
                {status.running && status.coref_total > 0 && (() => {
                    const cppct = Math.floor((status.coref_done / status.coref_total) * 100);
                    return (
                        <div className="row" style={{ alignItems: 'center', marginTop: 6 }}>
                            <div style={{ flex: 1 }}>
                                <ProgressBar value={cppct} showValue={false}
                                             color="#b65c00"
                                             style={{ height: 10 }} />
                            </div>
                            <span style={{ marginLeft: 12, fontSize: 13 }}>
                                coref {status.coref_done}/{status.coref_total} ({cppct}%) — per-chunk LLM
                            </span>
                        </div>
                    );
                })()}
                <div style={{ marginTop: 6, fontSize: 13 }}>
                    Errors: {status.errors || 0} &nbsp;|&nbsp; Last: <code>{status.last_file || '—'}</code>
                </div>
                {status.error && <div className="err">⚠ {status.error}</div>}
            </div>

            {finishSummary && (
                <div className="card finishCard">
                    <h3>✅ Ingestion finished at {finishSummary.finishedAt}</h3>
                    <div className="finishGrid">
                        <div><b>Papers processed:</b> {finishSummary.done} / {finishSummary.total}</div>
                        <div><b>Errors:</b> {finishSummary.errors || 0}</div>
                        <div><b>Duration:</b> {formatDuration(finishSummary.duration)}</div>
                    </div>
                    {finishSummary.error && <div className="err">⚠ {finishSummary.error}</div>}
                    {/* Phase 5n: chain logimport status. Surfaced from the
                        worker-side audit call. */}
                    {finishSummary.chain_logimport === 'ok' && (
                        <div style={{ marginTop: 8, fontSize: 13, color: '#22863a' }}>
                            ⛓ Chain logimport: <b>ok</b>
                            {finishSummary.chain_logimport_tx && (
                                <> · tx <code>{finishSummary.chain_logimport_tx}</code></>
                            )}
                        </div>
                    )}
                    {finishSummary.chain_logimport === 'error' && (
                        <div style={{ marginTop: 8, fontSize: 13, color: '#d73a49' }}>
                            ⛓ Chain logimport failed: {finishSummary.chain_logimport_msg || '(no detail)'}
                        </div>
                    )}
                    {finishSummary.chain_logimport === 'skipped' && (
                        <div style={{ marginTop: 8, fontSize: 13, color: '#888' }}>
                            ⛓ Chain logimport skipped (no papers ingested or run errored).
                        </div>
                    )}
                    <div className="row" style={{ marginTop: 8 }}>
                        <Button label="Dismiss" size="small" severity="secondary"
                                onClick={() => setFinishSummary(null)} />
                        <Button label="List indexed papers" size="small" icon="pi pi-list"
                                onClick={listPapers} />
                    </div>
                </div>
            )}

            <div className="card">
                <h3>📝 Recent files</h3>
                <DataTable value={recent} size="small" scrollable scrollHeight="320px" emptyMessage="No activity yet">
                    <Column field="paper_id" header="ID" style={{ width: 70 }} />
                    <Column field="filename" header="File" />
                    <Column field="title" header="Title" />
                    <Column field="status" header="Status" body={(r) => (
                        <Tag value={r.status}
                             severity={r.status === 'ok' ? 'success'
                                     : r.status === 'already_indexed' ? 'info'
                                     : r.status === 'partial' ? 'warning'
                                     : 'danger'} />
                    )} style={{ width: 110 }} />
                    <Column header="Chunks" style={{ width: 110 }}
                            body={(r) => r.failed_chunks
                                ? <span>{r.chunks} <span style={{ color: '#b00020' }}>(✗{r.failed_chunks})</span></span>
                                : (r.chunks ?? '')} />
                    <Column header="Detail" body={(r) => r.error
                        ? <span title={r.error} className="errCell">{String(r.error).slice(0, 100)}{String(r.error).length > 100 ? '…' : ''}</span>
                        : ''} />
                </DataTable>
            </div>

            <Dialog header="📁 Pick corpus directory" visible={browseOpen} style={{ width: '900px' }}
                    onHide={() => setBrowseOpen(false)} dismissableMask>
                {/* PDF count is the key signal — make it the headline so users
                    can see at a glance whether the current directory is the
                    right one to ingest. */}
                <div style={{
                    marginBottom: 12, padding: 12,
                    background: browseInfo.pdf_count > 0 ? '#e6f4ea' : '#fafafa',
                    borderRadius: 6, border: '1px solid #e1e4e8',
                    display: 'flex', alignItems: 'baseline', gap: 12,
                }}>
                    <span style={{
                        fontSize: 32, fontWeight: 700, lineHeight: 1,
                        color: browseInfo.pdf_count > 0 ? '#22863a' : '#999',
                    }}>
                        {browseInfo.pdf_count || 0}
                    </span>
                    <span style={{ fontSize: 14, color: '#444' }}>
                        PDF{browseInfo.pdf_count === 1 ? '' : 's'} found in this directory
                        {browseInfo.pdf_count === 0 && (
                            <span style={{ color: '#888', marginLeft: 6 }}>
                                — pick a subfolder below
                            </span>
                        )}
                    </span>
                </div>
                <div style={{ marginBottom: 10 }}>
                    <span style={{ fontSize: 12, color: '#666' }}>Path:</span>
                    <code style={{ marginLeft: 6, wordBreak: 'break-all' }}>{browseInfo.path || '—'}</code>
                    {browseInfo.cwd && (
                        <span style={{ marginLeft: 12, fontSize: 11, color: '#888' }}>
                            (worker cwd: <code>{browseInfo.cwd}</code>)
                        </span>
                    )}
                </div>
                <div className="row" style={{ marginBottom: 8 }}>
                    <Button icon="pi pi-arrow-up" label="Parent" size="small" disabled={!browseInfo.parent}
                            onClick={() => browseAt(browseInfo.parent)} />
                    <Button icon="pi pi-home" label="Worker cwd" size="small"
                            onClick={() => browseAt(browseInfo.cwd || '')} />
                    <Button icon="pi pi-refresh" label="Reload" size="small"
                            onClick={() => browseAt(browseInfo.path)} loading={browseLoading} />
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                    {/* Subdirectories (left column) — for navigation */}
                    <div>
                        <div style={{ fontSize: 12, color: '#666', marginBottom: 4 }}>
                            Subdirectories ({(browseInfo.subdirs || []).length})
                            {(browseInfo.subdirs || []).length > 0 && ' — click to enter:'}
                        </div>
                        <div style={{ maxHeight: 320, overflow: 'auto', border: '1px solid #e1e4e8', borderRadius: 6 }}>
                            {(browseInfo.subdirs || []).length === 0 && (
                                <div style={{ padding: 12, color: '#888', fontSize: 13, fontStyle: 'italic' }}>
                                    None.
                                </div>
                            )}
                            {(browseInfo.subdirs || []).map((d) => (
                                <div key={d} className="browseRow"
                                     onClick={() => browseAt(browseInfo.path + (browseInfo.path.endsWith('/') ? '' : '/') + d)}>
                                    <i className="pi pi-folder" style={{ marginRight: 8, color: '#fab005' }} />{d}
                                </div>
                            ))}
                        </div>
                    </div>
                    {/* PDF list (right column) — preview of what will be ingested */}
                    <div>
                        <div style={{ fontSize: 12, color: '#666', marginBottom: 4 }}>
                            PDFs in this directory ({browseInfo.pdf_count || 0})
                            {browseInfo.pdfs_truncated && (
                                <span style={{ color: '#d4a017' }}> — showing first 500</span>
                            )}
                        </div>
                        <div style={{ maxHeight: 320, overflow: 'auto', border: '1px solid #e1e4e8', borderRadius: 6 }}>
                            {(browseInfo.pdfs || []).length === 0 && (
                                <div style={{ padding: 12, color: '#888', fontSize: 13, fontStyle: 'italic' }}>
                                    No PDFs here. Navigate into a subdirectory or up via Parent.
                                </div>
                            )}
                            {(browseInfo.pdfs || []).map((f) => (
                                <div key={f.name} className="browseRow"
                                     style={{ cursor: 'default',
                                              display: 'flex', alignItems: 'center', gap: 6 }}
                                     title={f.name}>
                                    <i className="pi pi-file-pdf" style={{ color: '#d73a49', flexShrink: 0 }} />
                                    <span style={{ flex: 1, overflow: 'hidden',
                                                   textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                                   fontSize: 13 }}>
                                        {f.name}
                                    </span>
                                    {f.size != null && (
                                        <span style={{ fontSize: 11, color: '#888', flexShrink: 0 }}>
                                            {f.size >= 1024 * 1024
                                                ? `${(f.size / 1024 / 1024).toFixed(1)} MB`
                                                : `${Math.round(f.size / 1024)} KB`}
                                        </span>
                                    )}
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
                <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}>
                    <Button label="Cancel" severity="secondary" onClick={() => setBrowseOpen(false)} />
                    <Button label={browseInfo.pdf_count > 0
                                    ? `Use this directory (${browseInfo.pdf_count} PDFs)`
                                    : 'Use this directory'}
                            icon="pi pi-check"
                            disabled={!browseInfo.path || (browseInfo.pdf_count === 0)}
                            tooltip={browseInfo.pdf_count === 0
                                    ? 'No PDFs in this directory — can\'t ingest from here'
                                    : undefined}
                            tooltipOptions={{ position: 'top' }}
                            onClick={() => {
                                setCorpusDir(browseInfo.path);
                                localStorage.setItem('corpusDir', browseInfo.path);
                                setBrowseOpen(false);
                            }} />
                </div>
            </Dialog>

            {papers.length > 0 && (
                <div className="card">
                    <h3>📦 Papers in collection ({papers.length})</h3>
                    <DataTable value={papers} size="small" scrollable scrollHeight="300px" paginator rows={20}>
                        <Column field="paper_id" header="ID" sortable style={{ width: 90 }} />
                        <Column field="title" header="Title" />
                        <Column field="filename" header="File" />
                        <Column field="chunks" header="Chunks" sortable style={{ width: 90 }} />
                        <Column field="pages" header="Pages" sortable style={{ width: 80 }} />
                    </DataTable>
                </div>
            )}
        </div>
    );
};

export default DBFromCorpusPapers;
