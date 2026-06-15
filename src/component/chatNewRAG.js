import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import axios from 'axios';
import { Dropdown } from 'primereact/dropdown';
import { InputText } from 'primereact/inputtext';
import { InputTextarea } from 'primereact/inputtextarea';
import { Button } from 'primereact/button';
import { Tag } from 'primereact/tag';
import { Panel } from 'primereact/panel';
import { ProgressSpinner } from 'primereact/progressspinner';
import { Checkbox } from 'primereact/checkbox';
import { Ollama, OllamaEmbeddings } from '@langchain/ollama';
import { Chroma } from '@langchain/community/vectorstores/chroma';
import { ChromaClient } from 'chromadb';
import { BufferMemory } from 'langchain/memory';
import { ConversationalRetrievalQAChain } from 'langchain/chains';
import configuration from './configuration.json';
import './chatNewRAG.css';

/**
 * ChatNewRAG — conversational Q&A over a paper-corpus collection built
 * by newRAG/ingest_corpus.py. Distinguishing features vs. the old
 * chatFromDB component:
 *
 *  - Optional **paper filter**: scope retrieval to a single paper_id via
 *    Chroma's metadata where-clause (the new ingestion tags every chunk
 *    with paper_id, section, page_from, …). Lets the user ask the corpus
 *    as a whole OR drill into one paper.
 *  - **Section filter**: defaults to excluding `references` and `card`
 *    chunks (which are summaries / noise for free-form Q&A). Toggleable.
 *  - **Retrieved-chunks panel** per message — transparency about which
 *    chunks of which paper(s)/section(s)/page(s) the LLM saw.
 *  - **Multi-turn memory** via langchain's BufferMemory (so follow-up
 *    questions resolve pronouns correctly).
 *  - Standard configuration reuses the same localStorage keys as the rest
 *    of the app (selectedOllama, selectedChromaDB, selectedLLMModel,
 *    papersEmbedModel, papersCollection).
 */

const CFG = (configuration && configuration.passer) || {};
// Pick the local (developer-machine) ingestion API when running on
// localhost, otherwise fall back to the remote production deployment.
// Keep this block byte-identical to the matching block in
// dbFromCorpusPapers.js so both components agree on which API to use.
const IS_LOCAL_HOST = typeof window !== 'undefined'
    && ['localhost', '127.0.0.1', '0.0.0.0'].includes(window.location.hostname);
const DEFAULT_INGEST_API = IS_LOCAL_HOST
    ? (CFG.IngestAPI       || 'http://127.0.0.1:8010')
    : (CFG.IngestAPIRemote || 'http://195.230.127.227:8010');
const DEFAULT_CHROMA = (CFG.Chroma && CFG.Chroma[0] && CFG.Chroma[0].url) || 'http://127.0.0.1:8000';
const DEFAULT_OLLAMA = (CFG.Ollama && CFG.Ollama[0] && CFG.Ollama[0].url) || 'http://127.0.0.1:11434';

const SYSTEM_PROMPT_TEMPLATE = `You are an expert assistant answering questions about academic papers using the provided context.

Rules:
- Answer ONLY from the context excerpts below. If the context is insufficient, say so plainly — do not invent facts.
- When you cite, include the chunk reference in brackets (e.g. [P:001 §method p.4]).
- Be concise. Quote the paper verbatim where it strengthens the answer.
{coref_hint}
Conversation so far:
{chat_history}

Question: {question}

Context excerpts:
----
{context}
----

Answer:`;

// When a first-person novel's coref ingest set a protagonist, retrieved
// chunks carry that name in `coref_protagonist` metadata. The chunk
// documents still show the natural prose ("my slave Anu") because that's
// what readers want to see. To get a comparable benefit at *read* time,
// we tell the LLM how to interpret the first-person pronouns it'll
// encounter. Returns the empty string when no protagonist is in scope,
// so the prompt stays clean for academic papers / manuals / third-person
// novels.
const buildCorefHint = (retrieved) => {
    const protagonists = new Set(
        (retrieved || [])
            .map(d => d?.metadata?.coref_protagonist)
            .filter(Boolean)
    );
    if (protagonists.size !== 1) return '';      // mixed or absent: no hint
    const name = [...protagonists][0];
    return (
        `\nFirst-person interpretation: when a passage uses "I", "me", "my", "mine", or "myself" ` +
        `OUTSIDE direct quoted dialogue, treat those pronouns as referring to ${name} ` +
        `(the protagonist whose narration this is). INSIDE quoted speech (text inside quotation marks), ` +
        `the "I" refers to whoever is speaking, not necessarily ${name}.\n`
    );
};

const ChatNewRAG = () => {
    // ---------- Config ----------
    const [ingestAPI, setIngestAPI] = useState(localStorage.getItem('ingestAPI') || DEFAULT_INGEST_API);
    const [chromaUrl, setChromaUrl] = useState(localStorage.getItem('selectedChromaDB') || DEFAULT_CHROMA);
    const [ollamaUrl, setOllamaUrl] = useState(localStorage.getItem('selectedOllama') || DEFAULT_OLLAMA);
    const [llmModel,  setLlmModel ] = useState(localStorage.getItem('selectedLLMModel') || 'mistral');
    const [embedModel, setEmbedModel] = useState(
        localStorage.getItem('papersEmbedModel') || localStorage.getItem('selectedLLMModel') || 'mistral'
    );
    const [temperature, setTemperature] = useState(parseFloat(localStorage.getItem('chatTempreture') || '0.2'));
    const [topK, setTopK] = useState(6);

    // ---------- Collections + papers ----------
    const [collection, setCollection] = useState(localStorage.getItem('papersCollection') || '');
    const [collections, setCollections] = useState([]);
    const [collInfo, setCollInfo] = useState(null);
    const [papers, setPapers] = useState([]);
    const [selectedPaperId, setSelectedPaperId] = useState(null);   // null = whole corpus
    const [includeRefs, setIncludeRefs] = useState(false);
    const [includeCards, setIncludeCards] = useState(false);

    // ---------- Conversation ----------
    // Each message: { role: 'user' | 'assistant', text, chunks?: [...] }
    const [messages, setMessages] = useState([]);
    const [input, setInput] = useState('');
    const [sending, setSending] = useState(false);
    const memoryRef = useRef(new BufferMemory({ memoryKey: 'chat_history', returnMessages: true }));
    const endRef = useRef(null);

    // ---------- Memoised Ollama instances ----------
    const embeddings = useMemo(() => {
        if (!embedModel || !ollamaUrl) return null;
        return new OllamaEmbeddings({ model: embedModel, baseUrl: ollamaUrl });
    }, [embedModel, ollamaUrl]);

    const llm = useMemo(() => {
        if (!llmModel || !ollamaUrl) return null;
        return new Ollama({
            baseUrl: ollamaUrl,
            model: llmModel,
            temperature: parseFloat(temperature) || 0.2,
        });
    }, [llmModel, ollamaUrl, temperature]);

    // ---------- Collection discovery ----------

    const listCollections = useCallback(async () => {
        try {
            const client = new ChromaClient({ path: chromaUrl });
            const cs = await client.listCollections();
            setCollections(cs.map((c, i) => ({ name: c, id: i })));
        } catch (_) {
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
        } catch (_) {
            setCollInfo(null);
        }
    }, [ingestAPI, chromaUrl, collection]);

    const loadPapers = useCallback(async () => {
        if (!collection) { setPapers([]); return; }
        try {
            const r = await axios.get(ingestAPI.replace(/\/$/, '') + '/papers', {
                params: { chroma: chromaUrl, collection },
                timeout: 60000,
            });
            setPapers(r.data?.papers || []);
        } catch (e) {
            setPapers([]);
        }
    }, [ingestAPI, chromaUrl, collection]);

    useEffect(() => { listCollections(); }, [listCollections]);
    useEffect(() => { probeCollection(); loadPapers(); }, [probeCollection, loadPapers]);

    // ---------- Auto-scroll on new message ----------
    useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, sending]);

    // ---------- Send message ----------

    const buildFilter = useCallback(() => {
        // Build the Chroma metadata filter using the collection's
        // profile-declared exclude list. This is what makes the chat
        // do the right thing on a `novel` collection (nothing excluded
        // by default — appendices and glossaries are primary sources)
        // vs. an `academic_paper` collection (references excluded).
        const clauses = [];
        if (selectedPaperId) clauses.push({ paper_id: String(selectedPaperId) });

        // Profile-driven base exclude list. Falls back to the v1 default
        // (only "references") when the collection has no profile tag yet.
        const profileExcludes = Array.isArray(collInfo?.metadata?.exclude_from_default_retrieval)
            ? collInfo.metadata.exclude_from_default_retrieval
            : ['references'];

        const excluded = new Set();
        for (const s of profileExcludes) excluded.add(s);
        if (includeRefs) excluded.delete('references');     // user override
        if (!includeCards) excluded.add('card');             // cards are never primary evidence by default
        if (excluded.size) clauses.push({ section: { $nin: Array.from(excluded) } });

        if (clauses.length === 0) return undefined;
        if (clauses.length === 1) return clauses[0];
        return { $and: clauses };
    }, [selectedPaperId, includeRefs, includeCards, collInfo]);

    // ---------- Phase 5j: entity-walk plumbing ----------
    //
    // We store entities at ingest time as comma-joined strings per kind
    // (ent_person, ent_loc, ent_org, ent_gpe — see corpus_profiles.py and
    // ingest_corpus.extract_entities). Chroma's metadata filter only does
    // whole-scalar equality, so we cannot natively query "any chunk whose
    // ent_person LIST contains 'Aragorn'". The walk hop therefore uses
    // Chroma's whereDocument $contains substring filter on the raw chunk
    // text, which matches what the LLM would actually read.

    const buildEntityFrequencies = useCallback((chunks) => {
        // chunks is an array of langchain Documents OR the {metadata,...}
        // shape we store on a message — accept either.
        const freq = new Map();   // `${kind}|${name}` -> count
        for (const c of chunks || []) {
            const m = c.metadata || c;
            for (const k of Object.keys(m)) {
                if (!k.startsWith('ent_')) continue;
                const v = m[k];
                if (typeof v !== 'string' || !v) continue;
                const kind = k.slice(4);
                for (const raw of v.split(',')) {
                    const name = raw.trim();
                    if (!name) continue;
                    const key = `${kind}|${name}`;
                    freq.set(key, (freq.get(key) || 0) + 1);
                }
            }
        }
        return Array.from(freq.entries())
            .map(([key, count]) => {
                const [kind, name] = key.split('|');
                return { kind, name, count };
            })
            // Drop very short tokens — too noisy as substring filters.
            .filter(e => e.name.length >= 3)
            .sort((a, b) => b.count - a.count)
            .slice(0, 12);
    }, []);

    const askLLM = useCallback(async (question, retrieved, options = {}) => {
        // Build context the same way as the main path; centralised so the
        // entity-walk pipeline produces an identical bracketed format.
        const context = (retrieved || []).map((d, i) => {
            const m = d.metadata || d;
            const pid = m.paper_id ?? '?';
            const sec = m.section ?? '?';
            const pf  = m.page_from;
            const pt  = m.page_to;
            const pg  = pf && pt && pf !== pt ? `${pf}-${pt}` : (pf ?? '?');
            const body = d.pageContent ?? d.content ?? '';
            return `[chunk ${i + 1} | P:${pid} §${sec} p.${pg}]\n${body}`;
        }).join('\n\n');

        const history = await memoryRef.current.loadMemoryVariables({});
        const chatHistory = Array.isArray(history.chat_history)
            ? history.chat_history.map(m => `${m._getType?.() || 'msg'}: ${m.content}`).join('\n')
            : (history.chat_history || '');

        const prompt = SYSTEM_PROMPT_TEMPLATE
            .replace('{coref_hint}', buildCorefHint(retrieved))
            .replace('{chat_history}', chatHistory || '(none)')
            .replace('{question}', question)
            .replace('{context}', context || '(no chunks retrieved — answer cautiously)');

        const answer = await llm.invoke(prompt);
        if (options.persist !== false) {
            await memoryRef.current.saveContext({ input: question }, { output: answer });
        }
        return answer;
    }, [llm]);

    const sendMessage = async () => {
        const question = input.trim();
        if (!question || !embeddings || !llm || !collection) return;
        if (collInfo?.embed_model && collInfo.embed_model !== embedModel) {
            const ok = window.confirm(
                `Collection "${collection}" was built with embed_model="${collInfo.embed_model}", ` +
                `but you're querying with "${embedModel}". Retrieval will silently return garbage. Continue?`
            );
            if (!ok) return;
        }

        setInput('');
        setSending(true);
        setMessages(prev => [...prev, { role: 'user', text: question }]);

        try {
            const vectorStore = await Chroma.fromExistingCollection(embeddings, {
                collectionName: collection, url: chromaUrl,
            });

            const filter = buildFilter();
            const k = Number(topK) || 6;
            const retrieved = await vectorStore.similaritySearch(question, k, filter);

            const answer = await askLLM(question, retrieved);

            const chunks = retrieved.map(d => ({
                paper_id: d.metadata?.paper_id,
                title:    d.metadata?.title,
                section:  d.metadata?.section,
                page_from: d.metadata?.page_from,
                page_to:   d.metadata?.page_to,
                content:  d.pageContent,
                metadata: d.metadata,    // kept so entity chips can read ent_*
            }));

            setMessages(prev => [...prev, {
                role: 'assistant',
                text: answer,
                chunks,
                entities: buildEntityFrequencies(chunks),
                seedQuestion: question,    // anchors the entity walk
            }]);
        } catch (e) {
            setMessages(prev => [...prev, {
                role: 'assistant',
                text: `❌ Error: ${e.message || e}`,
                chunks: [],
            }]);
        } finally {
            setSending(false);
        }
    };

    const walkOnEntity = async (seedQuestion, entity) => {
        // The walk reuses the seed question's embedding (so semantic relevance
        // is preserved) and adds a substring constraint requiring the chunk
        // text to mention `entity.name`. Renders as a synthetic conversation
        // turn so the user can keep walking.
        if (!seedQuestion || !embeddings || !llm || !collection) return;
        const walkLabel = `[walk → ${entity.kind}:${entity.name}] ${seedQuestion}`;
        setSending(true);
        setMessages(prev => [...prev, { role: 'user', text: walkLabel }]);
        try {
            const chromaClient = new ChromaClient({ path: chromaUrl });
            const coll = await chromaClient.getCollection({ name: collection });
            const qEmb = await embeddings.embedQuery(seedQuestion);
            const k = Number(topK) || 6;
            const filter = buildFilter();

            const r = await coll.query({
                queryEmbeddings: [qEmb],
                nResults: k,
                where: filter,
                whereDocument: { $contains: entity.name },
            });

            const ids   = r.ids?.[0]       || [];
            const docs  = r.documents?.[0] || [];
            const metas = r.metadatas?.[0] || [];
            const retrieved = ids.map((_id, i) => ({
                pageContent: docs[i] || '',
                metadata: metas[i] || {},
            }));

            if (retrieved.length === 0) {
                setMessages(prev => [...prev, {
                    role: 'assistant',
                    text: `(no chunks found whose text contains "${entity.name}" under the current filters)`,
                    chunks: [],
                }]);
                return;
            }

            const answer = await askLLM(walkLabel, retrieved);

            const chunks = retrieved.map(d => ({
                paper_id: d.metadata?.paper_id,
                title:    d.metadata?.title,
                section:  d.metadata?.section,
                page_from: d.metadata?.page_from,
                page_to:   d.metadata?.page_to,
                content:  d.pageContent,
                metadata: d.metadata,
                _walkedOn: entity.name,
            }));

            setMessages(prev => [...prev, {
                role: 'assistant',
                text: answer,
                chunks,
                entities: buildEntityFrequencies(chunks),
                seedQuestion,    // keep the same seed so further walks compose
            }]);
        } catch (e) {
            setMessages(prev => [...prev, {
                role: 'assistant',
                text: `❌ Walk failed: ${e.message || e}`,
                chunks: [],
            }]);
        } finally {
            setSending(false);
        }
    };

    const clearChat = () => {
        setMessages([]);
        memoryRef.current = new BufferMemory({ memoryKey: 'chat_history', returnMessages: true });
    };

    const onKey = (e) => {
        // Enter to send, Shift+Enter for newline
        if (e.key === 'Enter' && !e.shiftKey && !sending) {
            e.preventDefault();
            sendMessage();
        }
    };

    const copyToClipboard = (text) => {
        try { navigator.clipboard.writeText(text); } catch (_) { /* noop */ }
    };

    // ---------- Render ----------

    const paperOptions = useMemo(() => {
        const opts = [{ value: null, label: 'All papers (corpus-wide retrieval)' }];
        for (const p of papers) {
            const t = (p.title || '').slice(0, 60);
            opts.push({
                value: String(p.paper_id),
                label: `${p.paper_id} — ${t || p.filename || '(untitled)'}`,
            });
        }
        return opts;
    }, [papers]);

    const mismatch = collInfo?.embed_model && collInfo.embed_model !== embedModel;

    return (
        <div className="chatNewRAG">
            <h2>💬 Chat with Paper Corpus (NewRAG)</h2>

            <div className="grid-2col">
                <div className="card">
                    <h3>🗄 Source</h3>
                    <label>ChromaDB URL</label>
                    <div className="row">
                        <InputText value={chromaUrl}
                                   onChange={(e) => { setChromaUrl(e.target.value); localStorage.setItem('selectedChromaDB', e.target.value); }}
                                   style={{ flex: 1 }} />
                        <Button label="↻" size="small" onClick={listCollections} tooltip="Refresh collections" />
                    </div>

                    <label>Collection ({collections.length} available — type to filter)</label>
                    <Dropdown
                        value={collection}
                        options={collections}
                        optionLabel="name"
                        optionValue="name"
                        onChange={(e) => { setCollection(e.value); localStorage.setItem('papersCollection', e.value); }}
                        placeholder={collections.length ? 'Pick a Chroma collection' : 'Click ↻ to load collections'}
                        filter
                        filterPlaceholder="Filter collections…"
                        filterBy="name"
                        showClear
                        editable
                        style={{ width: '100%' }}
                    />

                    <label>Paper (filters retrieval to one paper, or whole corpus)</label>
                    <Dropdown
                        value={selectedPaperId}
                        options={paperOptions}
                        onChange={(e) => setSelectedPaperId(e.value)}
                        placeholder={papers.length ? 'All papers' : '(load papers via collection)'}
                        filter
                        filterBy="label"
                        filterPlaceholder="Filter by id or title…"
                        showClear
                        style={{ width: '100%' }}
                    />

                    <div className="row" style={{ marginTop: 8, gap: 16, flexWrap: 'wrap' }}>
                        <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                            <Checkbox inputId="incRefs" checked={includeRefs} onChange={(e) => setIncludeRefs(e.checked)} />
                            include references
                        </label>
                        <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                            <Checkbox inputId="incCards" checked={includeCards} onChange={(e) => setIncludeCards(e.checked)} />
                            include card (title+abstract) chunks
                        </label>
                    </div>

                    {mismatch && (
                        <div className="err" style={{ marginTop: 8 }}>
                            ⚠ Collection was built with embed_model = <code>{collInfo.embed_model}</code>.
                            Retrieval with <code>{embedModel}</code> will return garbage.
                        </div>
                    )}
                </div>

                <div className="card">
                    <h3>🤖 LLM</h3>
                    <label>Ollama URL</label>
                    <InputText value={ollamaUrl}
                               onChange={(e) => { setOllamaUrl(e.target.value); localStorage.setItem('selectedOllama', e.target.value); }} />
                    <label>LLM model</label>
                    <InputText value={llmModel}
                               onChange={(e) => { setLlmModel(e.target.value); localStorage.setItem('selectedLLMModel', e.target.value); }} />
                    <label>Embedding model (must match ingestion)</label>
                    <InputText value={embedModel}
                               onChange={(e) => { setEmbedModel(e.target.value); localStorage.setItem('papersEmbedModel', e.target.value); }} />
                    <div className="grid-2col">
                        <div>
                            <label>Top-K chunks</label>
                            <InputText value={topK} onChange={(e) => setTopK(e.target.value)} />
                        </div>
                        <div>
                            <label>Temperature</label>
                            <InputText value={temperature}
                                       onChange={(e) => { setTemperature(e.target.value); localStorage.setItem('chatTempreture', e.target.value); }} />
                        </div>
                    </div>
                </div>
            </div>

            {/* Chat panel */}
            <div className="card chatPanel">
                <div className="row" style={{ justifyContent: 'space-between' }}>
                    <h3 style={{ margin: 0 }}>
                        💭 Conversation
                        {selectedPaperId &&
                            <Tag severity="info"
                                 style={{ marginLeft: 10, fontSize: 12 }}
                                 value={`scoped to paper ${selectedPaperId}`} />}
                    </h3>
                    <Button label="Clear chat" icon="pi pi-trash" size="small" severity="secondary"
                            outlined onClick={clearChat} disabled={!messages.length} />
                </div>

                <div className="messages">
                    {messages.length === 0 && (
                        <div className="emptyHint">
                            {!collection
                                ? <>Pick a collection above, then ask a question.</>
                                : <>Ready. Ask anything about <code>{collection}</code>
                                  {selectedPaperId && <> (paper {selectedPaperId})</>}.</>}
                        </div>
                    )}

                    {messages.map((m, i) => (
                        <div key={i} className={`msg msg-${m.role}`}>
                            <div className="msg-header">
                                <strong>{m.role === 'user' ? '👤 You' : `🤖 ${llmModel}`}</strong>
                                {m.role === 'assistant' && (
                                    <Button icon="pi pi-copy" size="small" text
                                            style={{ padding: 2, marginLeft: 6 }}
                                            onClick={() => copyToClipboard(m.text)}
                                            tooltip="Copy answer" />
                                )}
                            </div>
                            <pre className="msg-body">{m.text}</pre>
                            {m.chunks && m.chunks.length > 0 && (
                                <Panel header={`📎 Retrieved chunks (${m.chunks.length})`}
                                       toggleable collapsed className="chunkPanel">
                                    {m.chunks.map((c, j) => (
                                        <div key={j} className="chunk">
                                            <div className="chunk-head">
                                                <Tag value={`P:${c.paper_id || '?'}`} severity="info" />
                                                <Tag value={`§${c.section || '?'}`} severity="secondary" />
                                                <Tag value={`p.${c.page_from}${c.page_to && c.page_from !== c.page_to ? `-${c.page_to}` : ''}`} />
                                                {c._walkedOn && <Tag severity="warning" value={`walked: ${c._walkedOn}`} />}
                                                {c.title && <span className="chunk-title">{c.title}</span>}
                                            </div>
                                            <pre className="chunk-body">{c.content}</pre>
                                        </div>
                                    ))}
                                </Panel>
                            )}
                            {m.role === 'assistant' && m.entities && m.entities.length > 0 && (
                                <div className="entityWalkBar" style={{
                                    marginTop: 6, padding: '6px 8px',
                                    background: '#fafafa',
                                    border: '1px solid #eee', borderRadius: 4,
                                    display: 'flex', flexWrap: 'wrap',
                                    alignItems: 'center', gap: 6, fontSize: 12,
                                }}>
                                    <span style={{ color: '#666', marginRight: 4 }}>
                                        ↪ walk further:
                                    </span>
                                    {m.entities.map((e, k) => (
                                        <Button
                                            key={k}
                                            label={`${e.name} (${e.count})`}
                                            size="small"
                                            text
                                            severity={
                                                e.kind === 'person' ? 'info'
                                                    : e.kind === 'loc' || e.kind === 'gpe' ? 'success'
                                                    : 'secondary'
                                            }
                                            icon={
                                                e.kind === 'person' ? 'pi pi-user'
                                                    : e.kind === 'loc' || e.kind === 'gpe' ? 'pi pi-map-marker'
                                                    : 'pi pi-tag'
                                            }
                                            style={{ padding: '2px 6px', fontSize: 12 }}
                                            disabled={sending}
                                            tooltip={`Re-query with text containing "${e.name}"`}
                                            tooltipOptions={{ position: 'top' }}
                                            onClick={() => walkOnEntity(m.seedQuestion, e)}
                                        />
                                    ))}
                                </div>
                            )}
                        </div>
                    ))}
                    {sending && (
                        <div className="msg msg-assistant">
                            <div className="msg-header"><strong>🤖 {llmModel}</strong></div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                                <ProgressSpinner style={{ width: 24, height: 24 }} strokeWidth="6" />
                                <span style={{ color: '#666' }}>thinking…</span>
                            </div>
                        </div>
                    )}
                    <div ref={endRef} />
                </div>

                <div className="composer">
                    <InputTextarea
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        onKeyDown={onKey}
                        rows={2}
                        autoResize
                        placeholder={collection ? "Ask a question… (Enter to send · Shift+Enter for newline)" : "Pick a collection first"}
                        disabled={!collection || sending}
                        style={{ width: '100%' }}
                    />
                    <Button label={sending ? "Sending…" : "Send"}
                            icon="pi pi-send"
                            onClick={sendMessage}
                            disabled={!collection || !input.trim() || sending}
                            style={{ minWidth: 100 }} />
                </div>
            </div>
        </div>
    );
};

export default ChatNewRAG;
