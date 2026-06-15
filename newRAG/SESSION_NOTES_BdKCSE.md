---
name: passer-rag-bdkcse-conference-paper-draft
description: "short IEEE-conference companion to the PaSSER-RAG journal paper — covers only the new approaches, with one numerical-example experiment scheduled"
metadata: 
  node_type: memory
  type: project
  originSessionId: e4547dea-e05f-403d-bf67-8edeb14a1410
---

A short IEEE-conference paper extending the PaSSER-SR Electronics 2026 paper [21] / [2 in this paper] from title-abstract screening to retrieval-augmented question answering. **Sibling to the longer journal paper** ([[project_passer_rag_paper]]). The conference paper covers *only* the new approaches and aspects of PaSSER-RAG; nothing about per-paper isolation, IMRaD detection, or other infrastructure that the journal paper covers. Currently two parallel versions are maintained: a baseline and an extended one with a planned numerical experiment.

**Why:** User is moving between computers and needs to resume the conference-paper work without re-deriving its scope, structure, or open work items. The conference paper has a specific 5-contribution novelty list that should be preserved verbatim across sessions; weakening any of them is a regression.

**How to apply:** When the user resumes on the other machine, treat this memory as the index for the conference paper. The *journal* paper has its own separate memory ([[project_passer_rag_paper]]); read both if the user is unclear which one they're working on. The conference paper's Table III has 20 `[TBD]` cells with a stable column structure — populate cells in place rather than restructuring the table.

## File locations

All under `/Users/boniradev/Downloads/testReact/passer/newRAG/`:

**Two parallel versions of the conference paper** (the *_ext* version is the most recent and is what the user is actively working on):

- **`paper_PaSSER_RAG_BdKCSE.md`** / **`.docx`** — *baseline* (~3 100 words, 6 sections, 2 tables, ~5 pages). Original short version focusing on the two core contributions only (`CorpusProfile` abstraction + embed/read-time coreference). §V is a brief proof-of-concept plan with no numerical experiment.
- **`paper_PaSSER_RAG_BdKCSE_ext.md`** / **`.docx`** — *extended* (~4 300 words, 6 sections, 3 tables, ~6–7 pages). Adds three things on top of baseline: (a) an explicit numbered list of **five principal contributions** in §I (was previously framed as "twofold"); (b) elevation of the *embed-text and document-text divergence* as a named design pattern with worked examples in §IV; (c) §V.B *Numerical Example: Hierarchy-Aware Synopsis Spans on the Python Tutorial* with Table III containing 20 `[TBD]` placeholder cells.

**User-managed manual snapshots** (do NOT regenerate or overwrite — these are the user's own backups):

- **`paper_PaSSER_RAG_BdKCSE_V1.docx`** (37 KB, Jun 6 13:00) — user backup of an earlier conference draft.
- **`paper_PaSSER_RAG_BdKCSE_V2.docx`** (47 KB, Jun 6 12:53) — user backup, likely close to the baseline output. Case distinction `_V1`/`_V2` vs `_ext` avoids case-insensitive-filesystem collisions on macOS.

**Style template + converter**:

- **`BdKCSE2025.pdf`** — the reference paper (Noncheva and Radeva, *Blockchain Framework for E-Voting in Bulgaria*, BdKCSE 2025, IEEE). Style template for both versions. IEEE conference format: Times New Roman 10 pt, single-column header + 2-column body (continuous section break), Roman-numeral sections (I, II, III, ...), lettered subsections (A, B, C), `TABLE I` Roman-numeral table labels, `Abstract—` and `Keywords—` bold-italic prefixes, hanging-indent IEEE-Vancouver references.
- **`tools/md_to_docx_ieee.py`** — the IEEE converter (distinct from the MDPI converter used by the journal paper). Resolves paths relative to its own location. Uses python-docx 1.2.0 — same setup as the journal paper: `pip3 install --user --break-system-packages python-docx`.

**Shared source documents** (same as the journal paper):

- **`RAG_EVOLUTION.md`**, **`RAG_EVOLUTION_V1.md`**, **`RAG_GENERALIZATION.md`** — internal documents distilled for the methods sections of both papers.

## Section structure (already complete, both versions)

| Section | State |
|---|---|
| Title + authors (2-author: Noncheva, Radeva) | Complete |
| Abstract (italic, `Abstract—` prefix) | Complete |
| Keywords (italic, `Keywords—` prefix) | Complete |
| I. Introduction | Complete; ends with 5 enumerated contributions (in `_ext` version) |
| II. Related Work | Complete; 4 paragraphs |
| III. The Profile-Driven Architecture | Complete; includes Table I (profile defaults) |
| IV. Embed-Time and Read-Time Coreference | Complete; subsections A First-Person Attribution, B Read-Time Interpretation Hint; includes the named *embed-text and document-text divergence* design pattern (in `_ext`) |
| V. Proof of Concept and Evaluation Plan | A: corpus validation + Table II costs (complete); B: numerical example with Table III (`[TBD]` cells); C: full evaluation plan (complete) |
| VI. Conclusion | Complete; lists all 5 contributions (in `_ext`) |
| References | 18 entries, all cited at least once, IEEE format with DOIs where available |

## Pending experimental work — the single-day numerical example (§V.B)

Total time budget: **~7 hours of focused work, single day**. Output: populate the 20 `[TBD]` cells of Table III.

| Step | Time | Deliverable |
|---|:-:|---|
| 1. Question generation | 1 h | 25 questions: 15 chapter-summarisation + 10 granular control on Python Tutorial 3.7.0 |
| 2. Gold-relevance labelling | 1.5 h | Union of top-100 retrievals across both configurations annotated as RELEVANT/PARTIAL/IRRELEVANT |
| 3. Two ingestion runs | ~30 min GPU | Collections `python_tut_N` (naïve span) and `python_tut_H` (hierarchy-aware span); chunk-ID suffixes differ so they coexist |
| 4. Retrieval runs | 15 min | For each (question × config), record top-10 chunk IDs and section types |
| 5. Answer generation | ~30 min LLM | End-to-end RAG answers under the default system prompt |
| 6. Metric computation | 1 h | Recall@10, Synopsis-Recall@10, Hedging Rate (regex), Mistral-judge Faithfulness |
| 7. Statistical analysis | 1 h | McNemar's exact test for paired binary metrics; Wilcoxon signed-rank for Faithfulness; Wilson 95 % CIs |

Falsifiable predictions (the experiment is designed to test these crisply):

- *Summarisation subset*: H produces **Synopsis-Recall@10 gain** AND **Hedging Rate drop** vs N. The smoking-gun sanity check is the *synopsis_input_chars* row in Table III: N must measure ≈ 40 chars/chapter and H must measure 3 000–14 000 chars/chapter, otherwise the ablation has not isolated the §III variable.
- *Control subset*: N and H are statistically indistinguishable. A non-null result here means the ablation isolates something other than the synopsis pathway.

Supplementary CSV to release alongside the manuscript (schema captured in the proposal):
`q_id, subset, q_text, gold_chunk_ids, gold_synopsis_id, config, topk_chunk_ids, topk_sections, synopsis_in_topk, recall_at_10, answer_text, hedged, faithfulness, synopsis_input_chars`.

## Critical design decisions to preserve

- **Stay short**: the conference paper covers *only* the new approaches. Do NOT expand it to cover server-side ingestion, blockchain audit, per-paper isolation, content_type classification, or the broader 8-week experimental plan — those belong to the journal paper. If the user asks for "more detail", offer to expand the journal paper instead.
- **Five contributions in §I** (in `_ext` version, the canonical list):
  1. `CorpusProfile` abstraction with collection-level enforcement (§III)
  2. Embed-text and document-text divergence design pattern (§IV)
  3. Two-stage coref treatment: embed-time first-person attribution with dialogue qualifier + read-time interpretation hint conditional on chunk metadata (§§IV.A, IV.B)
  4. Hierarchy-aware synopsis-span computation (§§III, V.B)
  5. Cost-control primitives: pronoun-density-thresholded pre-pass (default *k* = 3) + 50%-length sanity check on coref rewrites
- **Dialogue qualifier in §IV.A** is the specific empirical detail that makes first-person attribution actually work. *"Anu said, 'I cannot.'"* must NOT be rewritten to *"Anu said, '[Protagonist] cannot.'"*. Preserve this example or an equivalent.
- **Embed-text and document-text divergence** is the most distinctive *generalisable* contribution. The paper explicitly claims it is "to our knowledge not standard practice in production RAG pipelines". Do not soften that claim without checking literature.
- **Numerical example is on the MANUAL profile, not the novel profile.** This was an explicit user instruction. Do not switch the experiment to *Jane Eyre* or any other narrative corpus.
- **Two-author convention** (Noncheva + Radeva) matching BdKCSE 2025 — do not silently expand to the 4-author Electronics convention.
- **`---COLUMN-BREAK---` marker** between the author block and the abstract is consumed by the IEEE converter. Do not remove it; do not add another one elsewhere.

## Known caveats to verify before submission

- The "~80 LOC per profile" claim should be verified against the actual `corpus_profiles.py` diff (cf. §III) — count the LOC of the `novel` profile entry, plus its helper functions.
- The 30–50 % wall-clock cut attributed to the pronoun-density pre-pass + length sanity check should be measured during the actual experimental run, not estimated.
- Reference [2] (PaSSER-SR Electronics 2026) needs verification that the journal publication is confirmed and the DOI is correct. The conference paper depends heavily on this citation.
- The Mistral-7B LLM-judge is a single-judge limitation — acknowledged in the paper but worth flagging to a reviewer who might ask for a stronger judge.
- The IEEE copyright footer (`979-8-XXXX-XXXX-X/26/$31.00 ©2026 IEEE` or similar for BdKCSE 2026) is NOT generated by the converter; it must be added via the IEEE Word template at submission time.
- If the venue caps strictly at 6 pages: the easiest trim from the `_ext` version is contribution (5) in §I (the cost-control primitives paragraph) — it's the least surprising and most easily condensed.

## How to regenerate the .docx after editing the .md

For the *_ext* version (the active draft):

```bash
python3 newRAG/tools/md_to_docx_ieee.py \
    --src newRAG/paper_PaSSER_RAG_BdKCSE_ext.md \
    --dst newRAG/paper_PaSSER_RAG_BdKCSE_ext.docx
```

For the baseline version (use only if you need to update it specifically):

```bash
python3 newRAG/tools/md_to_docx_ieee.py
```

(With no flags the converter defaults to `paper_PaSSER_RAG_BdKCSE.md` → `paper_PaSSER_RAG_BdKCSE.docx`.)

If python-docx is missing on the new machine: `pip3 install --user --break-system-packages python-docx`.

## Related memories

[[passer-rag-paper-draft]] / [[project_passer_rag_paper]] — the longer journal paper, which the conference paper is a sibling to. Both papers cite each other (the conference paper cites the journal as a companion work; the journal can cite this conference paper as a focused preliminary report). When updating either paper, check the other for consistency on terminology and contribution framing.
