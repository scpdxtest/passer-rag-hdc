# NewRAG beyond scientific papers — amendments for long-form corpora

A companion to `RAG_EVOLUTION.md` that answers a different question:
*if I point NewRAG at a 500-page fantasy novel or a 1 200-page
technical manual instead of a 565-paper corpus, what works, what
breaks, and what needs to change?*

---

## 0. The short answer

The **architectural backbone** of NewRAG transfers to any long-form
corpus without modification:

- Server-side ingestion in a Python worker (browser-close-survivable).
- Metadata-rich chunks → filtered retrieval via Chroma's `where`.
- Content-hashed chunk IDs → idempotent re-ingestion.
- Embedding-model collection guard → no silent retrieval garbage.
- Per-chunk failure isolation, sanitisation, context-overflow auto-truncation.
- Optional on-chain audit trail (overkill for casual chat but useful
  for editorial / continuity-verification use cases).
- Server-side jobs with pause/resume/cancel (still relevant for
  bulk-summarisation, multi-LLM annotation, character extraction).

What does **not** transfer is the *corpus-specific knowledge* baked
into four places:

1. **Document-id extraction** — `filename = "NNN - …pdf"`
   convention.
2. **Section detection** — nine IMRaD regexes (abstract, intro,
   method, results, etc.).
3. **Card-chunk composition** — `Title + Abstract + Keywords`,
   ≤ 1500 chars.
4. **References-as-noise assumption** — section tagged `references`
   is excluded from default retrieval.

For a novel, none of those four assumptions hold (no IDed filename,
no abstract, no "references" — but possibly a glossary or
appendices that are *primary* sources, not noise). For a technical
manual, the structure is hierarchical (chapter → section →
subsection) and the "card" should summarise a chapter, not an
abstract.

The amendment is therefore not a rewrite but a **plug-in point**:
turn those four assumptions into a *corpus profile* and ship two or
three additional profiles (novel, manual) alongside the existing
academic-paper one. Sections 2 and 3 of this document specify what
those profiles need to contain.

---

## 1. What in the current design is paper-specific

### 1.1 Filename → `paper_id`

`ingest_corpus.py:paper_id_from_filename` extracts `paper_id` from
the leading `\d{2,4}\s*-\s*` of the filename. For a single novel
named `the_lord_of_the_rings.pdf`, this fails over to a SHA-1 hash
of the filename — usable but uninformative. For a single document
the per-paper retrieval filter (`{paper_id: "X"}`) becomes a
no-op: it always selects the entire corpus.

What you'd want instead for a single long document: filters on
`chapter`, `part`, `pov_character`, or `entities`.

### 1.2 IMRaD section regexes

`SECTION_PATTERNS` recognises nine canonical academic-paper
headings. None of them match `Chapter I`, `PART THREE`, `§ 4.2.1`,
or the unmarked scene breaks (`* * *`, `‡`, blank-line) typical of
narrative or hierarchical-manual structure.

### 1.3 Card-chunk composition

`process_paper` builds the card as:

```
Title + DOI + arXiv + Abstract[:75 % of budget] + Keywords[:25 %]
```

For a novel the natural per-chapter "card" is something like the
first paragraph of the chapter, or a one-sentence synopsis. For a
manual it's the chapter's "What you'll learn" / "Overview" block.

### 1.4 References-as-noise

`section: references` is excluded from default retrieval. For a
novel, an *appendix* containing a family lineage (cf. Tolkien's
appendices on the Heirs of Elendil) is what you actually want
queried first. For a manual, an *index* is a high-precision
retrieval source, not noise.

---

## 2. The proposed generalisation: corpus profiles

Make the four points above configurable through a small Python
class (or dict, or JSON) consumed by `ingest_corpus.py`. A
**corpus profile** specifies:

```python
class CorpusProfile:
    name: str                          # "academic_paper", "novel", "manual", …
    document_id_rule: Callable[[str], str]
                                       # filename → document_id
    section_patterns: list[tuple[str, str]]
                                       # [(section_name, regex), …]
    card_recipe: dict
                                       # which detected sections to weave
                                       # into the card, and in what proportion
    exclude_from_default_retrieval: set[str]
                                       # section names to tag-but-exclude
    chunk_size: int = 800
    chunk_overlap: int = 120
    card_budget_chars: int = 1500
    extract_entities: bool = False
                                       # run lightweight NER per chunk
    entity_kinds: list[str] = []
                                       # e.g. ["PERSON", "LOC", "ORG"]
    extra_metadata_extractors: list[Callable[[str, dict], dict]] = []
                                       # functions that read a chunk's text
                                       # + base metadata and return extra
                                       # metadata keys/values
```

At ingestion time the worker selects a profile from a registry by
name (`profile="academic_paper"` by default). The
`get_collection_for_embed` guard learns one more field —
`profile` — and refuses to re-ingest into a collection built with
a different profile.

Concretely, three profiles cover ~95 % of long-form-corpus use
cases beyond papers:

- **`academic_paper`** — the existing behaviour, unchanged.
- **`novel`** — § 3 below.
- **`manual`** — § 4 below.

Adding a fourth profile (legal, medical, transcripts, etc.) is then
a ~50-line code change per profile, not a re-architecture.

---

## 3. Case study: a 500-page novel

### 3.1 What "good retrieval" looks like

The questions readers ask of a long novel are not "what's the
conclusion of the abstract" but:

- **Character query.** *"List every scene featuring Tyrion Lannister in
  the second half of the book."* Needs `entities contains "Tyrion"`
  AND `chapter > N/2`.
- **Lineage query.** *"Who are the descendants of Aragorn son of
  Arathorn?"* Needs the appendices (where lineages are catalogued)
  AND maybe forward references in body chapters.
- **Sub-plot query.** *"Summarise the romance arc between A and B
  across the novel."* Multi-hop: find scenes with both A and B
  present, order by chapter, summarise.
- **First-appearance query.** *"When does the One Ring first appear
  in the text?"* Order chunks by chapter index, filter
  `entities contains "ring"`, return earliest.
- **POV query.** *"What chapters are narrated from Jon Snow's
  point of view?"* Filter `pov_character == "Jon Snow"`.

None of these are well-served by flat top-$k$ similarity. All are
well-served by metadata-filtered retrieval (the NewRAG
backbone), *provided the right metadata exists*.

### 3.2 `novel` profile — what changes

| profile field | value for `novel` |
|---|---|
| `document_id_rule` | filename → static `"book"` (single-doc corpus); or `book_id` from a sidecar manifest for multi-novel libraries |
| `section_patterns` | chapter (`(?im)^\s*(chapter\s+(\d+|[ivxlcdm]+|\w+)\b)`); part (`(?im)^\s*(part\s+(\d+|[ivxlcdm]+|[a-z]+)\b)`); prologue / epilogue / interlude; appendix (`(?im)^\s*appendix\b`); scene break (`^\s*[\*‡§•◆]\s*$` between chunks) |
| `card_recipe` | per-chapter: first 1–2 paragraphs of the chapter → tagged `section: "chapter_card"` |
| `exclude_from_default_retrieval` | empty — appendices, glossaries, dramatis personae are *primary* sources for some queries |
| `extract_entities` | `True` |
| `entity_kinds` | `["PERSON", "LOC", "ORG"]` (spaCy `en_core_web_sm` or LLM-based) |
| `extra_metadata_extractors` | `[pov_detector, dialog_density, scene_index]` |

The detected hierarchy becomes the metadata graph:

```
book_id ─┬─ part: "Part Two"
         │
         ├─ chapter: 14
         │  chapter_title: "The Council of Elrond"
         │
         ├─ scene_index: 3        ← scene-break within the chapter
         │
         ├─ pov_character: "Frodo"
         │
         ├─ entities: ["Frodo", "Gandalf", "Elrond", "Boromir", "the Ring"]
         │
         ├─ section: "body" | "chapter_card" | "appendix" | "glossary"
         │
         └─ page_from / page_to (preserved from pdfplumber)
```

### 3.3 The per-chapter card

The chapter card is the novel-corpus equivalent of the abstract:
- ~500 chars per card,
- Tagged `section: "chapter_card"`,
- Used as the *cheap relevance gate*: for *"is this chapter
  relevant to the question?"* the LLM scores the card before
  triggering full retrieval of the chapter's body chunks.

For a 80-chapter novel this means the first pass over a query
makes 80 cheap LLM calls instead of $80 \cdot k$ expensive ones,
and the second pass only retrieves from chapters that passed the
gate. Same economics as in the paper case.

For literary chapters without a natural opening summary, the card
can alternatively be **LLM-generated at ingestion time** — one
sentence per chapter produced by a cheap model (e.g.
`qwen3:8b`) — and stored as a chunk like any other. This is
materially better than just embedding the first paragraph for
chapters that open in medias res.

### 3.4 The "synopsis" chunk

For sub-plot / arc queries (*"how does the romance between A and
B develop?"*), one card per chapter is still too granular. A
second tier on top:

- **Book synopsis** — 1 paragraph summary of the whole novel,
  `section: "book_synopsis"`.
- **Arc synopses** — 1 paragraph per major sub-plot,
  `section: "arc_synopsis"`, with the involved characters in
  `entities`.

These can be authored manually for a high-value corpus or
LLM-generated at ingestion time as a one-off batch (cost: ~$N_{chapters}$
LLM calls). Tagged so the retriever can pull them on demand:

```python
where = {"$and": [{"book_id": "lotr"}, {"section": "arc_synopsis"},
                  {"entities": {"$in": ["Aragorn", "Arwen"]}}]}
```

### 3.5 Lineage queries — multi-hop retrieval

*"Who are the descendants of Aragorn?"* cannot be answered from
one paragraph. The chain of reasoning is:

1. Retrieve from `section: "appendix"` AND `entities contains "Aragorn"`.
2. Identify named descendants in the retrieved text (LLM extraction).
3. For each descendant, retrieve their first body appearance + any
   appendix entry.
4. Compose the lineage from accumulated chunks.

NewRAG today supports single-hop filtered retrieval. Multi-hop is
a thin wrapper:

```python
# pseudo-code
def trace_lineage(root_name: str):
    found = {root_name}
    queue = [root_name]
    output = []
    while queue:
        name = queue.pop(0)
        chunks = retrieve(filter={"entities": {"$in": [name]},
                                  "section": "appendix"})
        for c in chunks:
            output.append(c)
            for entity in c.metadata["entities"]:
                if entity not in found:
                    found.add(entity); queue.append(entity)
    return summarise(output)
```

This is ~30 lines of code and lives in a new
`newRAG/narrative_queries.py` module that the new chat UI calls
when the user explicitly asks for a lineage/genealogy query.

### 3.6 Anaphora and character aliasing

A real difficulty: *"He turned to her and smiled"* has no
embed-able entity. Three mitigations, in increasing cost:

1. **Larger overlap.** Setting overlap = 200 (instead of 120)
   keeps the prior 1–2 sentences in the next chunk, so most
   pronoun antecedents stay visible. Cheap; partially effective.
2. **Pre-processing pass with coreference resolution.** Use
   `spaCy` + `coreferee` (or LLM-based resolution at ingestion
   time) to rewrite pronouns to their antecedents:
   *"He turned to her and smiled"* →
   *"[Aragorn] turned to [Arwen] and smiled."* This is computed
   *once* at ingestion and stored in a separate field
   `text_resolved` alongside the original `pageContent` (which is
   what's shown to the user). The resolved text is what gets
   embedded. Cost: one pass with a small LLM at ingest time;
   substantial retrieval quality boost.
3. **Character-alias index.** Maintain a side-table mapping
   aliases to canonical character names (*"the boy king" →
   "Tommen Baratheon"*) and rewrite or augment the
   `entities` metadata at chunk time. Manual or LLM-curated;
   one-time effort per novel.

A practical recommendation: ship (1) and (3) by default; gate (2)
behind an `--coref` flag because it adds ~2-3 LLM calls per
chapter at ingestion time.

### 3.7 Quantitative sketch for a 500-page novel

Assumptions: ~3 500 chars per page → 1.75 M chars; 80 chapters;
~25 characters detected.

| | value |
|---|---:|
| Body chunks | ~2 200 (800-char window, 120 overlap, per chapter) |
| Chapter cards | 80 |
| Arc synopses | ~10 (one per major arc) |
| Book synopsis | 1 |
| Appendix / glossary chunks | ~50 |
| Total Chroma vectors | ~2 340 |
| Ingestion wall-clock (`mxbai-embed-large` on M-series, local) | ~8 minutes |
| Re-ingestion (idempotent, unchanged corpus) | ~30 s |
| Cost of card-gate over the whole book for one criterion | 80 LLM calls |
| Cost of full-corpus retrieval for one criterion | 80 cards + ~k retrieved chunks per kept chapter (typical 30-50 chunks total) |

All numbers fit comfortably on a developer laptop.

---

## 4. Case study: a 1 200-page technical manual

### 4.1 What "good retrieval" looks like

Manual users ask:

- **Procedure query.** *"How do I configure SAML SSO?"* Needs the
  step-by-step procedure section, with all numbered steps in
  order, not split mid-step.
- **Reference query.** *"What does error code E-2104 mean?"* Needs
  the error-code table, ideally filtered to that exact code.
- **Concept query.** *"What is the difference between feature A
  and feature B?"* Needs the concept-overview sections of both
  features, plus possibly a comparison table.
- **Cross-reference query.** *"Section 4.3 says to see Appendix C
  — what does Appendix C say?"* Needs the ability to *follow*
  in-document references.

Again: all answerable with metadata filtering on the right
metadata.

### 4.2 `manual` profile — what changes

| profile field | value for `manual` |
|---|---|
| `document_id_rule` | `manual_id` from filename (e.g. `redhat_admin_8.7`) plus a `version` metadata field |
| `section_patterns` | numbered heading hierarchy: `(?m)^(\d+(\.\d+)*)\s+(.+)$` matched at multiple levels; chapter / section / subsection / procedure detection |
| `card_recipe` | per-chapter introduction (the "Overview" or "What you'll learn" block at the start of each chapter) |
| `exclude_from_default_retrieval` | empty — the index is high-precision and useful, the glossary defines terms |
| `chunk_size` | 1200 (larger; procedures need to stay together) |
| `extra_metadata_extractors` | `[content_type_classifier, procedure_grouper, cross_reference_extractor]` |

### 4.3 The `content_type` field

The most useful single addition for manual-style content. Add a
metadata field `content_type` with values:

| value | matches | retrieval implication |
|---|---|---|
| `prose` | regular paragraphs | default |
| `procedure` | numbered-step blocks | keep the block atomic (don't split mid-procedure) |
| `code` | indented / fenced code | preserve verbatim, atomic chunk |
| `table` | detected by pdfplumber's table extractor | preserve cell structure as TSV inside the chunk |
| `figure_caption` | `^Figure \d+:` lines | retrievable separately for "where is figure 12?" |
| `warning` | call-out boxes ("Warning", "Caution", "Note") | retrievable separately for safety queries |
| `error_code_row` | rows of error-code tables | filter by exact code |

A criterion like *"Does the manual provide a configuration
example?"* can then filter `content_type IN ("procedure", "code")`
and skip prose.

### 4.4 Procedure-atomic chunking

A chunked procedure is a broken procedure. The amendment: a
**pre-pass** identifies procedure blocks (regex on `^\d+\.\s+` at
the start of consecutive lines, or a "Procedure" header) and emits
the whole block as one chunk regardless of size, up to a hard
ceiling (say 3 000 chars). The character-window splitter is
applied only to prose content.

Implementation: change `split_chunks` to take a list of
*pre-segmented* regions, each with an `atomic: bool` flag.
Atomic regions become single chunks; non-atomic regions get the
800-char sliding window as today.

### 4.5 Cross-reference graph

Manuals are riddled with *"see Section 5.4"*, *"refer to Appendix
B"*. Extract those at ingestion time and store as a
metadata array `cross_refs: ["§5.4", "Appendix B"]`. Then a
follow-up retrieval pass (similar to the lineage walk in §3.5)
can pull the referenced sections automatically when the user asks
*"what does the referenced section say?"*.

### 4.6 Quantitative sketch for a 1 200-page manual

| | value |
|---|---:|
| Body chunks (mixed prose + procedure) | ~6 000 |
| Chapter cards (~15 chapters × 1 card each) | 15 |
| Procedure-atomic chunks | ~400 |
| Table-atomic chunks | ~250 |
| Code-atomic chunks | ~300 |
| Total Chroma vectors | ~7 000 |
| Ingestion wall-clock | ~25 minutes |
| Re-ingestion | ~1 min |

Still server-side, still idempotent, still pause/resume-able.

---

## 5. Amendment matrix — what code changes are needed

| amendment | files changed | LOC | risk |
|---|---|---|---|
| 5a. Corpus profile abstraction | new `newRAG/corpus_profiles.py`; minor edits in `ingest_corpus.py` | ~150 + 50 | low |
| 5b. `academic_paper` profile (factor out current code) | move regex + card recipe into the profile registry | ~100 (moves, not adds) | low |
| 5c. `novel` profile | new entry in registry | ~80 | low |
| 5d. `manual` profile | new entry in registry | ~120 | low |
| 5e. Atomic-region pre-pass for procedures + tables + code | extend `split_chunks` | ~60 | medium |
| 5f. `content_type` metadata field | add to per-chunk metadata + Chroma schema; one classifier function per profile | ~80 | low |
| 5g. NER pass (spaCy) for `entities` metadata | new module `newRAG/ner.py` (optional, profile-flagged) | ~120 | low |
| 5h. Coreference resolution at ingest (optional) | new module `newRAG/coref.py`; gated by `--coref` flag | ~150 + costs LLM calls | medium |
| 5i. Alias index for characters (manual + LLM hybrid) | a per-corpus JSON sidecar consumed by the NER step | ~40 | low |
| 5j. Multi-hop retrieval (lineage / cross-ref walk) | new module `newRAG/narrative_queries.py` for the chat UI to call | ~100 | low |
| 5k. LLM-generated synopses / chapter cards at ingestion | new optional step in the ingest pipeline | ~80 | low |
| 5l. UI: profile picker in `dbFromCorpusPapers.js` | extend the existing form | ~30 | low |
| 5m. UI: profile-aware retrieval defaults in `chatNewRAG.js` and `scorePapersBat.js` | parameterise the filters that are currently hardcoded against IMRaD sections | ~40 each | low |
| **Total** | ~1 200 LOC across ~12 files | — | mostly low |

For comparison, the current NewRAG codebase is ~3 500 LOC. The
generalisation is a ~30 % addition, not a rewrite.

---

## 6. What stays the same

Worth being explicit about this so a paper reviewer doesn't
suspect over-engineering:

- The contract (`sscore.cpp`) is **corpus-agnostic**. It logs
  `(paper_id, criterion_id)` cells, but neither value is
  validated — they could just as well be `(chapter, character)` or
  `(procedure_id, audit_question)`. No change.
- The chain bridge (`chain_bridge.py`) needs **no change**. The
  canonical payload schema is generic.
- The scoring-job orchestrator (`scoring_jobs.py`) needs **no
  change**. It already works against any Chroma collection with
  any metadata schema.
- The Chroma direct-HTTP wrapper (`ChromaHTTP`) is **independent
  of corpus type**.
- The MongoDB schemas (`sscore_jobs`, `sscore_cells`,
  `sscore_imports`, `sscore_runs`, `sscore_analysts`) are
  generic.
- The job lifecycle (pause / resume / cancel /
  zombie-reconciliation / 503-degraded) is generic.

The amendment surface is narrow: ingestion-time metadata
construction + retrieval-time filter defaults, with optional NER
/ coref / synopsis modules layered on for narrative corpora.

---

## 7. Recommended adoption sequence

If you want to ship this in stages:

1. **Foundation (1–2 days):** §5a + §5b. Factor the current
   IMRaD logic into the `academic_paper` profile, register it,
   wire the worker to look up profiles by name. No behaviour
   change; the test suite continues to pass.
2. **Novel profile (~3 days):** §5c + §5f + §5g. Chapter / part
   detection, `content_type` field, basic NER for characters.
   Get the lineage / character-scene queries working end-to-end
   on one demo novel.
3. **Manual profile (~3 days):** §5d + §5e. Hierarchical
   section detection + atomic procedure/code chunks. Demo a
   procedure-extraction Q&A on one technical manual.
4. **Optional enrichment (~1 week):** §5h (coref) + §5j
   (multi-hop walks) + §5k (LLM-generated synopses). These
   markedly improve narrative-corpus quality but are not
   required for v1.
5. **UI polish (~1 day):** §5l + §5m. Profile picker in the
   ingestion UI and profile-aware filter defaults in the chat
   and scoring UIs.

Total to a usable "NewRAG for arbitrary long-form corpora":
**~2 weeks of focused work**, mostly in new files, with the
existing paper-corpus pipeline preserved and re-shipped as the
default profile.

---

## 8. Limitations even after amendment

To stay honest about what NewRAG-with-profiles still cannot do:

- **Multimodal queries** (figure content, illustrated diagrams):
  the current pipeline embeds *text only*. Figures and
  illustrations are referenced by caption but their pixel
  content is not searchable. A vision-language embedding
  (CLIP / SigLIP) is a separate architectural addition.
- **Audio / video content** (audiobook transcripts, lecture
  recordings): requires upstream ASR; semantically the
  resulting transcript is just another text corpus that NewRAG
  can ingest with a `transcript` profile.
- **Mathematical reasoning across the corpus** (manuals with
  equations, papers with proofs): equations survive as inline
  text but the LLM-as-judge / LLM-as-reader doesn't do symbolic
  math. Out of scope.
- **Hard quantitative reasoning over tables**: a per-table
  chunk preserves cell structure as TSV, but precise
  aggregations (*"sum the third column"*) need the LLM to do
  arithmetic, which is unreliable on large numbers. A
  structured-table store (DuckDB sidecar) is a future option.
- **Cross-corpus reasoning**: NewRAG is single-collection at
  retrieval time. Joining a "novel" collection and a
  "criticism" collection requires either UNIONing them at
  ingestion (lose provenance) or implementing federated
  retrieval (out of scope).
- **Plot graph / character-arc analytics**: today's NewRAG
  returns *passages*, not extracted *entities and relations*.
  Building a knowledge graph from the corpus is a separate
  enrichment step that complements rather than replaces NewRAG.

These limitations apply equally to the current
`academic_paper`-only NewRAG; the proposed amendments don't
introduce new limitations, they just reveal which ones are
already there.

---

## 9. Net conclusion

NewRAG **as currently designed** is paper-specific in roughly four
explicit places. Replacing those with a small *corpus profile*
abstraction (one profile per corpus type) gives you:

- A general-purpose long-form-corpus RAG with the same
  metadata-filtered retrieval semantics.
- The same idempotent ingestion / robust failure handling /
  pause-resume jobs / blockchain audit trail (the latter being
  optional but unchanged).
- ~2 weeks of focused work to ship `novel` and `manual`
  profiles alongside the existing `academic_paper` one.

Two profile-specific extras (NER for narrative entities,
atomic-chunking for procedures) deliver disproportionate
retrieval quality for their target corpora; coreference
resolution and LLM-generated synopses are advanced options that
materially close the gap to "feels like reading the book itself"
queries.

For a 500-page novel asking *"trace the lineage of House X"* or a
1 200-page manual asking *"how do I configure feature Y"*, the
*architecture* is ready; the *configuration* needs the changes
specified in §2–§5.
