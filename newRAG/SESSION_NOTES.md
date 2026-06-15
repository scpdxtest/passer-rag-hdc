---
name: passer-rag-paper-draft
description: "in-progress journal paper extending PaSSER-SR — current state, file locations, pending experimental work"
metadata: 
  node_type: memory
  type: project
  originSessionId: e4547dea-e05f-403d-bf67-8edeb14a1410
---

A draft scientific paper extending the PaSSER-SR Electronics 2026 paper [21] from title-abstract screening to retrieval-augmented question answering with a profile-driven architecture (`academic_paper`, `novel`, `manual`). Currently a complete architecture and experimental-plan draft with placeholder result tables; awaiting the experimental phase to populate ~140 `[TBD]` cells.

**Why:** User is moving between computers and needs to resume work without re-deriving the paper's state, structure, or open work items. This is a multi-week project that will produce real experimental results to fill the placeholders.

**How to apply:** When the user resumes on the other machine, treat this memory as the index. Read the files at the paths below for content; do NOT rewrite from scratch. The result tables (5–10) have stable column structures with `[TBD]` cells — populate cells in place rather than restructuring tables.

## File locations

All under `/Users/boniradev/Downloads/testReact/passer/newRAG/`:

**Journal paper (MDPI Electronics-style, long format, with experimental plan + placeholder result tables)**

- **`paper_PaSSER_RAG.md`** — the editable source (~10 200 words, 7 sections, 11 tables; 139 `[TBD]` cells + 13 prose placeholders of the form *"to be added upon Week-N completion"*).
- **`paper_PaSSER_RAG.docx`** — generated from the .md by `tools/md_to_docx.py`.
- **`electronics-15-01661-v2.pdf`** — the reference paper (Radeva, Noncheva, Doukovska, Popchev. *Comparing Single-Agent and Multi-Agent Strategies in LLM-Based Title-Abstract Screening*. Electronics 2026, 15, 1661). Journal paper's structure mirrors this one. Cite as [21].

**Conference paper (IEEE BdKCSE-style, short format, covers only the new approaches)**

Two parallel versions are maintained:

- **`paper_PaSSER_RAG_BdKCSE.md`** / **`.docx`** — the *baseline* short paper (~3 100 words, 6 sections, 2 tables, ~5 pages). Focuses on the two genuinely new contributions: the `CorpusProfile` abstraction (§III) and the embed-time + read-time coreference treatment with first-person attribution (§IV). §V is a brief proof-of-concept + cross-profile evaluation plan.
- **`paper_PaSSER_RAG_BdKCSE_ext.md`** / **`.docx`** — the *extended* short paper (~3 870 words, 6 sections, 3 tables, ~6 pages). Adds a planned §V.B *Numerical Example: Hierarchy-Aware Synopsis Spans on the Python Tutorial* — a focused single-variable ablation (N naïve span vs H hierarchy-aware span) on 25 questions split into 15 chapter-summarisation + 10 granular control. Table III has 20 `[TBD]` cells to be populated upon the ~7-hour experimental run described in §V.B.1. The §VI Conclusion forward-references this experiment.
- Both versions contain a `---COLUMN-BREAK---` marker between the author block and the abstract that the IEEE converter consumes to transition from 1-column header to 2-column body.
- **`paper_PaSSER_RAG_BdKCSE_V1.docx`** and **`paper_PaSSER_RAG_BdKCSE_V2.docx`** are *user-managed* manual backup snapshots (created outside the converter). The case difference (`_V1`/`_V2` vs `_ext`) avoids case-insensitive-filesystem collisions on macOS.
- **`BdKCSE2025.pdf`** — the reference paper from the same conference series (Noncheva and Radeva, *Blockchain Framework for E-Voting in Bulgaria*, BdKCSE 2025). Style template for both conference-paper variants.

**Shared source documents**

- **`RAG_EVOLUTION.md`**, **`RAG_EVOLUTION_V1.md`**, **`RAG_GENERALIZATION.md`** — internal documents that became both papers' body content. See `RAG_EVOLUTION.md` §6 + §8 for the corpus-generalisation framing and evaluation plan that became §4 of the journal paper and §III–§V of the conference paper.

**Converter scripts**

- **`newRAG/tools/md_to_docx.py`** — MDPI-style (Palatino, single-column, justified body). Drives the journal paper.
- **`newRAG/tools/md_to_docx_ieee.py`** — IEEE-style (Times New Roman, 1-col header + 2-col body via continuous section break on the `---COLUMN-BREAK---` marker, centered all-caps section headings, hanging-indent references). Drives the conference paper.

Both scripts resolve paths relative to their own location, so they work anywhere the repo is checked out. First-time setup on a fresh machine: `pip3 install --user --break-system-packages python-docx`.

## Paper structure (already complete)

| Section | State |
|---|---|
| Abstract | Complete; explicitly frames the paper as an amendment to PaSSER-SR [21] |
| §1 Introduction | Complete; 4 RQs (RQ1–RQ3 architectural, RQ4 empirical) |
| §2 Related Work | Complete; 7 sub-sections ending in §2.7 Research Gap |
| §3 Methods | Complete; system overview + `CorpusProfile` abstraction + 5 enrichments + audit trail |
| §4 Experimental Protocol | Complete plan, no data yet — see "Pending experimental work" below |
| §5 Results | Placeholder tables 5–10, every data cell `[TBD]` |
| §6 Discussion | Architectural arguments populated; per-RQ empirical discussion marked *"to be added upon Week-N completion"* |
| §7 Conclusions | Architectural-claim paragraphs complete; per-RQ summaries pending |

## Pending experimental work — the 8-week schedule (paper §4.7, Table 4)

Each row is a milestone whose completion populates specific Results-section tables.

| Week | Milestone | Tables/sections to populate |
|:-:|---|---|
| 1 | Question generation for Gold Standard | Section 4.2 figures (300 candidate questions) |
| 2 | Pilot annotation + κ ≥ 0.6 calibration | — |
| 3 | Full relevance labelling (dual annotation) | Table 5 (κ, PABAK per corpus) |
| 4 | Reference answers drafted | — |
| 5 | Ingestion under all 21 configurations (7 × 3 corpora) | Table 8 (cost data) |
| 6 | Retrieval-quality evaluation | Table 6 (Recall@10/100, nDCG@10, Bonferroni-adjusted p-values) |
| 7 | Answer-quality evaluation, dual LLM-judge | Table 7 (Faithfulness × 2 judges, Hedging Rate, judge-κ); §6.2 Table 11 sub-hypotheses |
| 8 | Cross-profile leakage + transcript profile | Tables 9, 10; §5.5, §5.6 |

## Critical design decisions to preserve

- **Test corpus for novel** is Charlotte Brontë's *Jane Eyre* (1847, Project Gutenberg eBook #1260, ref [29]). NOT the original test novel (Cthulhu mythos / Necronomicon of Alhazred). Any mention of "Alhazred", "Cthulhu", or "occult" in the paper is a regression — search and remove.
- **Academic corpus** is the same 565-PDF blockchain-voting corpus from PaSSER-SR. This shared corpus is the principal cross-study link and should be preserved verbatim — do not propose a different academic corpus.
- **First-person attribution clause** (§3.6.1) is the central empirical claim of the paper. The `protagonist_name="Jane Eyre"` configuration is what makes Configuration C6 distinct from C5 in Table 3. The expected finding is that C5 alone is insufficient on first-person novels and the read-time hint of C6 is required to close the hedging gap.
- **Configurations C0–C6** in Table 3 are the official ablation labels. Use these labels consistently in every Results-section table.
- **Statistical testing**: Wilcoxon signed-rank with Bonferroni correction, 95% bootstrap CIs (10 000 resamples), Wilson interval for Hedging Rate (binomial proportion). These choices mirror PaSSER-SR — preserve them.
- **References were curated** to be cited at least once in the body. The bibliography runs cleanly 1–29 with no orphans. When adding new content, prefer reusing existing citations; if a new ref is added, ensure it is cited in the body.
- **AI-use disclosure** is in the Acknowledgments and mirrors the PaSSER-SR style: "the authors used Claude (Anthropic, Opus 4.7) for purposes of drafting initial section structure...". Preserve this for honesty.

## Known caveats to verify before submission

- The `[21]` citations make specific claims about PaSSER-SR (three-tier audit, PABAK, McNemar, Wilson intervals, Zenodo DOI namespace). Cross-check each `[21]` mention against the actual Electronics paper text — some methodology attributions may need to be softened or removed if the screening paper does not explicitly claim them.
- The author list (Radeva, Noncheva, Doukovska, Popchev) mirrors PaSSER-SR. Confirm authorship and CRediT roles for the new paper before submission.
- The Antelope chain references should be verified against the actual chain endpoint and contract version used.
- The 8-week schedule is aggressive. Real timeline may push tables 5–10 population to Week 10+ depending on annotation rate.

## How to regenerate the .docx after editing the .md

**Journal paper (MDPI):**

```bash
python3 newRAG/tools/md_to_docx.py
```

**Conference paper (IEEE / BdKCSE):**

```bash
python3 newRAG/tools/md_to_docx_ieee.py
```

Both honour `--src` / `--dst` overrides. If python-docx is missing on the new machine: `pip3 install --user --break-system-packages python-docx` (PEP 668 requires the flag on macOS Homebrew Python).

## Related memories

[[passer_screening_paper]] — would be the screening paper's own memory if one exists. None at present.
