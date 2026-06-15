"""
Corpus profiles — pluggable corpus-type configuration for ingestion.

A profile encapsulates the four corpus-specific assumptions identified
in newRAG/RAG_GENERALIZATION.md §1:

  1. Document-id extraction (filename → id)
  2. Section detection (regex list)
  3. Card-chunk composition (which sections weave into the per-doc
     card chunk, in what proportion)
  4. References-as-noise — which detected sections to exclude from
     default retrieval

The registry currently ships three profiles:

  - academic_paper  (the original NewRAG behaviour, default)
  - novel           (chapter / part / scene detection for long-form fiction)
  - manual          (hierarchical section detection for technical docs)

Add a fourth by constructing a CorpusProfile and calling `register()`.

This module is imported by ingest_corpus.py. It has zero non-stdlib
dependencies so it stays available in every Python environment where
the rest of NewRAG runs.
"""

from __future__ import annotations
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional


def _normalize_for_title(text: str) -> str:
    """Repair the two PDF-extraction artefacts that corrupt title detection:

    1. **Drop-cap line splits.** Stylised initial letters get extracted
       as a one-character line followed by the rest of the word
       lowercased on the next line (e.g. 'J\\nohn'). We merge them
       so the title-picker sees 'John', not 'ohn'.
    2. **Decorative-letter unicode.** Display fonts replace letters
       with look-alikes — 'Å' (U+00C5, A with ring) instead of plain
       'A'. NFKD decomposition splits these into base + combining mark;
       stripping the marks yields the ASCII letter.

    Only the FIRST few thousand characters need this treatment, and
    only the *title extraction* sees the normalized form — the source
    `full_text` is untouched (chunk content stays verbatim)."""
    # 1. Drop-cap merge
    lines = text.split("\n")
    merged_lines = []
    i = 0
    while i < len(lines):
        cur = lines[i].strip()
        if (len(cur) == 1 and cur.isalpha() and i + 1 < len(lines)):
            nxt = lines[i + 1].lstrip()
            if nxt and nxt[0].islower():
                merged_lines.append(cur + nxt)
                i += 2
                continue
        merged_lines.append(lines[i])
        i += 1
    merged = "\n".join(merged_lines)
    # 2. NFKD + strip combining marks
    nfkd = unicodedata.normalize("NFKD", merged)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


@dataclass
class CorpusProfile:
    """A pluggable configuration that turns generic-pipeline NewRAG into
    a corpus-type-specific ingestion. See module docstring."""

    # Identity ---------------------------------------------------------
    name: str

    # Structural rules -------------------------------------------------
    section_patterns: list                       # [(section_name, regex_string), ...]
    """List of (section_name, regex) pairs. Each regex is compiled once
    on first access with re.IGNORECASE | re.MULTILINE. The first match
    of each pattern in the document's canonical text becomes a section
    start offset; consecutive matches define non-overlapping spans
    tagged with section_name."""

    document_id_from_filename: Callable[[str], str]
    """fname (str) -> document_id (str). For corpora where the filename
    doesn't carry an obvious id, fall back to a hash of the filename."""

    extract_doc_metadata: Callable[[str], dict]
    """full_text (str) -> {title, doi?, arxiv_id?, version?, ...} plus
    any profile-specific fields (e.g. {chapter_title}, {pov_character})
    that should land in chunk metadata."""

    build_card: Callable[[dict, dict, int], str]
    """sections    (dict mapping section_name to its concatenated text),
       doc_meta    (the dict from extract_doc_metadata),
       budget      (max characters)
       -> card_body (str). Returns "" if no card-worthy content exists
       for this profile + document."""

    # Retrieval defaults -----------------------------------------------
    exclude_from_default_retrieval: set = field(default_factory=set)
    """Section names that are tagged at ingestion time but excluded by
    default at retrieval time (e.g. {"references"} for academic_paper).
    Empty for profiles where every section is a primary source."""

    # Chunking defaults ------------------------------------------------
    chunk_size: int = 800
    chunk_overlap: int = 120
    card_budget_chars: int = 1500

    # Enrichment flags -------------------------------------------------
    classify_content_type: bool = True
    """When True, every chunk's metadata gains a `content_type` field
    classified by a lightweight heuristic — one of
    {prose, dialog, code, procedure, list, table_text}. Always on by
    default because the classifier is cheap and the field is broadly
    useful (e.g. 'find code blocks in this paper'). Disable on a
    per-profile basis only if the corpus is fully uniform."""

    extract_entities: bool = False
    """When True, run a spaCy NER pass during ingestion and add an
    `entities` array (deduplicated, lowercased) to each chunk's
    metadata. Requires the `spacy` package + an English model (e.g.
    `python -m spacy download en_core_web_sm`); if either is missing
    the ingestion logs a one-line warning and proceeds without
    entities (no failure). Off by default; the novel profile turns
    it on because it's the keystone for character-based retrieval."""

    entity_kinds: list = field(default_factory=lambda: ["PERSON", "LOC", "ORG"])
    """spaCy entity types to keep. Defaults to the three most useful
    for narrative corpora."""

    atomic_block_patterns: list = field(default_factory=list)
    """List of (name, regex) pairs describing 'atomic' regions —
    contiguous text spans that must be emitted as a single chunk
    regardless of chunk_size (within a per-profile hard ceiling).
    Used by the manual profile to keep procedures and code blocks
    coherent."""

    atomic_block_ceiling: int = 4000
    """Hard upper bound (chars) for any single atomic chunk. Beyond
    this we fall back to the normal sliding splitter — better to
    chop a 12-page procedure than to send a single 50 KB blob to
    Ollama and crash on context overflow."""

    # Synopsis flags (Phase 5k) ----------------------------------------
    synopsize: bool = False
    """When True, for each section whose name is in `synopsize_sections`,
    we ask the local LLM (model passed via opts.llm_model) to generate
    a 4-6 sentence synopsis and emit it as an extra `section: 'synopsis'`
    chunk. The synopsis is embedded with the same model as body chunks,
    so retrieval surfaces it naturally. Idempotent: the synthetic chunk
    id is derived from a hash of the SOURCE text, so a re-ingest skips
    LLM calls whose synopses already exist in the collection. Off by
    default; `novel` and `manual` turn it on."""

    synopsize_sections: list = field(default_factory=list)
    """Section names eligible for synopsis generation. Empty list means
    'no synopsis even if synopsize=True'. Profile-specific so e.g. the
    novel summarises chapters but not the glossary."""

    synopsis_prompt: str = ""
    """Profile-tuned prompt template. The literal token {text} is
    replaced with the source-section text at runtime; no other
    substitutions are performed (so curly braces in prose pass through
    safely). Empty string means the generic template in
    ingest_corpus.run_synopsis_pass is used."""

    synopsis_max_input_chars: int = 12000
    """Hard cap on how much section text is fed to the LLM. For
    chapters longer than this we take head+tail to keep wall-clock
    bounded without losing the opening/closing scenes."""

    synopsis_max_output_tokens: int = 320
    """num_predict passed to Ollama. Tuned for ~4-6 sentences;
    profiles with longer ideal synopses can raise it."""

    # Coreference resolution flags (Phase 5h) --------------------------
    coref: bool = False
    """When True, for each body chunk the local LLM is asked to rewrite
    pronouns (he/she/it/they/...) to their named-entity antecedents.
    The REWRITTEN text is what gets embedded (so a search for 'Aragorn'
    surfaces chunks that originally only said 'he'), while the
    ORIGINAL text is what gets stored as the chunk document (preserving
    the natural prose for display and LLM-reader citation).

    Cost is dramatically higher than synopses — one LLM call per chunk,
    NOT one per chapter. A 2000-chunk novel = ~3-6 hours on a single
    consumer GPU. Off by default; only the `novel` profile enables it,
    because pronoun-heavy narrative is the only corpus type where the
    embedding quality jump justifies the cost (technical writing avoids
    pronoun ambiguity by convention)."""

    coref_pronoun_threshold: int = 3
    """Skip the LLM call when a chunk contains fewer than this many
    third-person pronouns. Saves ~30-50% of calls on a typical novel
    (dialog-heavy chunks naturally drop; descriptive chunks pass through).
    The minimum-3 default targets prose-quality wins, not perfection."""

    coref_context_chars: int = 1500
    """How much trailing text from the PREVIOUS body chunk gets included
    as context for resolving antecedents that aren't in the current
    chunk. Larger values help character continuity across paragraph
    breaks; smaller values reduce prompt tokens. 1500 is roughly the
    last ~250 words, which catches most pronoun chains in fiction."""

    coref_max_input_chars: int = 8000
    """Hard cap on (context + chunk) sent to the LLM. Chunks exceeding
    this skip the coref pass and embed the original text — better an
    un-resolved chunk than a truncated rewrite the model can't trust."""

    coref_max_output_tokens: int = 2200
    """num_predict for the rewrite. Must be larger than the chunk
    itself since the resolved version is typically a bit longer
    (substituting names for short pronouns)."""

    coref_prompt: str = ""
    """Optional profile-tuned prompt template. The literal tokens
    {context} and {passage} are replaced at runtime. Empty string
    falls back to the generic template in
    ingest_corpus._GENERIC_COREF_PROMPT."""

    # Lazily compiled regex cache
    _compiled_cache: Optional[list] = field(default=None, repr=False, compare=False)
    _compiled_atomic_cache: Optional[list] = field(default=None, repr=False, compare=False)

    def compiled_section_patterns(self) -> list:
        """Returns [(section_name, compiled_regex), ...]. Compiled once
        per profile instance on first access."""
        if self._compiled_cache is None:
            self._compiled_cache = [
                (name, re.compile(pat, re.IGNORECASE | re.MULTILINE))
                for name, pat in self.section_patterns
            ]
        return self._compiled_cache

    def compiled_atomic_patterns(self) -> list:
        """Returns [(name, compiled_regex), ...] for atomic-block
        detection. Compiled once per profile instance."""
        if self._compiled_atomic_cache is None:
            self._compiled_atomic_cache = [
                (name, re.compile(pat, re.IGNORECASE | re.MULTILINE | re.DOTALL))
                for name, pat in self.atomic_block_patterns
            ]
        return self._compiled_atomic_cache


# ---------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------

_REGISTRY: dict = {}


def register(profile: CorpusProfile) -> None:
    _REGISTRY[profile.name] = profile


def get_profile(name: Optional[str] = None) -> CorpusProfile:
    """Look up a profile by name. None / "" → default "academic_paper".
    Raises ValueError on an unknown name (with the available list)."""
    if not name:
        name = "academic_paper"
    if name not in _REGISTRY:
        raise ValueError(
            f"unknown corpus profile '{name}'. "
            f"Registered profiles: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]


def list_profiles() -> list:
    return sorted(_REGISTRY.keys())


# ---------------------------------------------------------------------
# Profile #1 — academic_paper (default; reproduces v1 NewRAG behaviour)
# ---------------------------------------------------------------------

_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_ARXIV_RE = re.compile(r"\barXiv[:\s]*([0-9]{4}\.[0-9]{4,5})(v\d+)?", re.IGNORECASE)


def _academic_paper_document_id(fname: str) -> str:
    m = re.match(r"^\s*(\d{2,4})\s*-\s*", fname)
    if m:
        return m.group(1).lstrip("0") or "0"
    return hashlib.sha1(fname.encode("utf-8")).hexdigest()[:8]


def _academic_paper_extract_meta(full_text: str) -> dict:
    title = ""
    head = _normalize_for_title(full_text[:2000])
    for line in head.splitlines():
        s = line.strip()
        if 8 < len(s) < 200 and not s.isupper() and not re.match(r"^[A-Z]+/[A-Z]+", s):
            title = s
            break
    if not title:
        title = head[:120].strip()
    doi = ""
    m = _DOI_RE.search(full_text[:5000])
    if m:
        doi = m.group(1).rstrip(".,;)")
    arxiv = ""
    m = _ARXIV_RE.search(full_text[:5000])
    if m:
        arxiv = m.group(1) + (m.group(2) or "")
    return {"title": title, "doi": doi, "arxiv_id": arxiv}


def _academic_paper_build_card(sections: dict, meta: dict, budget: int) -> str:
    pieces = []
    if meta.get("title"):    pieces.append(f"Title: {meta['title']}")
    if meta.get("doi"):      pieces.append(f"DOI: {meta['doi']}")
    if meta.get("arxiv_id"): pieces.append(f"arXiv: {meta['arxiv_id']}")
    used = sum(len(p) for p in pieces) + len(pieces)
    remaining = max(200, budget - used)
    abstract_share = int(remaining * 0.75)
    keywords_share = remaining - abstract_share
    abstract_text = sections.get("abstract", "")
    keywords_text = sections.get("keywords", "")
    if abstract_text:
        pieces.append(abstract_text[:abstract_share])
    if keywords_text:
        pieces.append(keywords_text[:keywords_share])
    return "\n".join(pieces).strip()


register(CorpusProfile(
    name="academic_paper",
    section_patterns=[
        ("abstract",   r"^\s*(abstract)\b"),
        ("keywords",   r"^\s*(keywords?|index\s+terms)\b"),
        ("intro",      r"^\s*(?:\d+\.?\s*)?(introduction|background)\b"),
        ("related",    r"^\s*(?:\d+\.?\s*)?(related\s+work|literature\s+review|prior\s+work)\b"),
        ("method",     r"^\s*(?:\d+\.?\s*)?(methodology|method[s]?|approach|"
                       r"proposed\s+(?:system|method|approach|model)|"
                       r"system\s+(?:architecture|design)|design|implementation)\b"),
        ("results",    r"^\s*(?:\d+\.?\s*)?(results?|evaluation|experiments?|findings)\b"),
        ("discussion", r"^\s*(?:\d+\.?\s*)?(discussion|analysis)\b"),
        ("conclusion", r"^\s*(?:\d+\.?\s*)?(conclusion[s]?|future\s+work)\b"),
        ("references", r"^\s*(?:\d+\.?\s*)?(references?|bibliography)\b"),
        ("appendix",   r"^\s*(?:appendix|acknowledg(?:e?ments?))\b"),
    ],
    document_id_from_filename=_academic_paper_document_id,
    extract_doc_metadata=_academic_paper_extract_meta,
    build_card=_academic_paper_build_card,
    exclude_from_default_retrieval={"references"},
    chunk_size=800,
    chunk_overlap=120,
    card_budget_chars=1500,
))


# ---------------------------------------------------------------------
# Profile #2 — novel (long-form fiction)
#
# See newRAG/RAG_GENERALIZATION.md §3 for the design rationale.
# This profile ships the structural detection layer; per-chapter
# LLM-generated synopses (§3.3) and entity extraction (§5g) are
# *enrichment passes* layered on top in later work — they are not
# required for the profile to be useful.
# ---------------------------------------------------------------------

def _novel_document_id(fname: str) -> str:
    base = re.sub(r"\.pdf$", "", fname, flags=re.IGNORECASE)
    base = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    return base[:30] if base else "book"


def _novel_extract_meta(full_text: str) -> dict:
    title = ""
    head = _normalize_for_title(full_text[:3000])
    for line in head.splitlines():
        s = line.strip()
        if 4 < len(s) < 120:
            title = s
            break
    return {"title": title}


def _novel_build_card(sections: dict, meta: dict, budget: int) -> str:
    """Book-level card. For a single novel ingestion, the card is the
    opening of the prologue (or the first chapter if no prologue), so
    the corpus has a coarse 'what is this book about' anchor that the
    scoring / chat layers can use as a cheap relevance pre-check.

    Per-CHAPTER cards (one per chapter) are a separate enrichment pass
    documented in RAG_GENERALIZATION.md §3.3 — they require an LLM call
    per chapter and are deferred to the follow-on profile-aware
    ingestion phase."""
    pieces = []
    if meta.get("title"):
        pieces.append(f"Title: {meta['title']}")
    opener = (sections.get("prologue")
              or sections.get("chapter")        # first chapter span
              or sections.get("body", ""))
    if opener:
        used = sum(len(p) for p in pieces) + len(pieces)
        remaining = max(200, budget - used)
        pieces.append(opener[:remaining])
    return "\n".join(pieces).strip()


register(CorpusProfile(
    name="novel",
    section_patterns=[
        ("prologue",  r"^\s*(prologue)\b"),
        ("epilogue",  r"^\s*(epilogue)\b"),
        ("part",      r"^\s*(part\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten))\b"),
        ("chapter",   r"^\s*(chapter\s+(?:\d+|[ivxlcdm]+|"
                      r"one|two|three|four|five|six|seven|eight|nine|ten|"
                      r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty))\b"),
        # Drop-cap convention: a single uppercase letter on its own
        # line followed by a lowercase line. Catches literary fiction
        # whose PDFs preserve typeset drop caps but have no textual
        # "Chapter N" header (common in literary novels that use a
        # decorative initial cap and small-caps subtitle instead).
        # `(?-i:...)` keeps the case test strict inside the
        # profile's outer re.IGNORECASE flag.
        ("chapter",   r"(?-i:^[A-Z]\n[a-z])"),
        ("interlude", r"^\s*(interlude)\b"),
        ("appendix",  r"^\s*(appendix(?:\s+[a-z])?)\b"),
        ("glossary",  r"^\s*(glossary|index|dramatis\s+personae)\b"),
        ("acks",      r"^\s*(acknowledg(?:e)?ments?|about\s+the\s+author)\b"),
    ],
    document_id_from_filename=_novel_document_id,
    extract_doc_metadata=_novel_extract_meta,
    build_card=_novel_build_card,
    exclude_from_default_retrieval=set(),    # appendix + glossary are PRIMARY sources here
    chunk_size=900,                           # slightly larger for prose continuity
    chunk_overlap=200,                        # larger overlap mitigates pronoun anaphora across chunks
    card_budget_chars=1500,
    classify_content_type=True,
    extract_entities=True,                    # the keystone for character / location queries
    entity_kinds=["PERSON", "LOC", "ORG", "GPE"],
    synopsize=True,
    synopsize_sections=["chapter", "part", "interlude", "prologue", "epilogue"],
    synopsis_prompt=(
        "You are creating a per-chapter synopsis for a novel that will be used "
        "as a retrieval card during semantic search. Read the passage and produce "
        "a single self-contained paragraph (4-6 sentences) that names the central "
        "characters, the principal location(s), the chapter's key events in order, "
        "and any objects, decisions, or revelations that matter for the rest of the "
        "story. Be specific and concrete; do not editorialise; do not preface; "
        "output only the synopsis text.\n\nPASSAGE:\n{text}\n\nSYNOPSIS:"
    ),
    synopsis_max_input_chars=14000,
    synopsis_max_output_tokens=380,
    coref=True,
    coref_prompt=(
        "You are doing coreference resolution on a passage from a novel. "
        "Rewrite the PASSAGE so that every third-person pronoun is replaced "
        "by the named entity it refers to. Pronouns to resolve: he, she, it, "
        "they, him, her, them, his, hers, their, theirs, its, himself, "
        "herself, itself, themselves. Use the CONTEXT to find antecedents "
        "that are not named in the passage itself.{protagonist_clause}\n"
        "Critical rules:\n"
        "- Preserve all other words, punctuation, paragraph breaks, dialogue "
        "marks, and line breaks EXACTLY as in the original.\n"
        "- Do NOT summarise, paraphrase, or shorten.\n"
        "- Do NOT add explanations or footnotes.\n"
        "- If a pronoun has no clear antecedent, leave it unchanged.\n"
        "- Output only the rewritten passage. No preamble, no quotation marks "
        "around the output.\n\n"
        "CONTEXT (the preceding passage, for antecedent lookup):\n{context}\n\n"
        "PASSAGE:\n{passage}\n\n"
        "REWRITTEN PASSAGE:"
    ),
))


# ---------------------------------------------------------------------
# Profile #3 — manual (technical documentation, workbooks)
#
# See newRAG/RAG_GENERALIZATION.md §4 for the design rationale.
# Atomic-procedure / atomic-code chunking (§5e) is a follow-on; this
# profile contributes the structural layer.
# ---------------------------------------------------------------------

def _manual_document_id(fname: str) -> str:
    base = re.sub(r"\.pdf$", "", fname, flags=re.IGNORECASE)
    base = re.sub(r"[^a-z0-9_.\-]+", "_", base.lower()).strip("_")
    return base[:40] if base else "manual"


def _manual_extract_meta(full_text: str) -> dict:
    title = ""
    head = _normalize_for_title(full_text[:3000])
    for line in head.splitlines():
        s = line.strip()
        if 5 < len(s) < 200:
            title = s
            break
    version = ""
    m = re.search(r"\b(?:version|v|release)[\s.:]*([\d.]+)\b", full_text[:5000], re.IGNORECASE)
    if m:
        version = m.group(1)
    return {"title": title, "version": version}


def _manual_build_card(sections: dict, meta: dict, budget: int) -> str:
    pieces = []
    if meta.get("title"):    pieces.append(f"Title: {meta['title']}")
    if meta.get("version"):  pieces.append(f"Version: {meta['version']}")
    overview = (sections.get("overview")
                or sections.get("introduction")
                or sections.get("preface")
                or sections.get("body", ""))
    if overview:
        used = sum(len(p) for p in pieces) + len(pieces)
        remaining = max(200, budget - used)
        pieces.append(overview[:remaining])
    return "\n".join(pieces).strip()


register(CorpusProfile(
    name="manual",
    section_patterns=[
        ("preface",      r"^\s*(preface)\b"),
        ("overview",     r"^\s*(what\s+you'?ll\s+learn|overview|in\s+this\s+chapter)\b"),
        ("introduction", r"^\s*(?:\d+(?:\.\d+)*\s+)?(introduction)\b"),
        # `\s+` here includes newline, so this also catches the
        # three-line Python-docs style header:
        #     CHAPTER
        #     ONE
        #     WHETTING YOUR APPETITE
        # The number can be digits, roman numerals, or English words —
        # Python Tutorial uses the words form ("CHAPTER\nONE"); other
        # manuals use the digit form ("Chapter 3").
        ("chapter",      r"^\s*(chapter\s+(?:\d+|[ivxlcdm]+|"
                         r"one|two|three|four|five|six|seven|eight|nine|ten|"
                         r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
                         r"twenty-one|twenty-two|twenty-three|twenty-four|twenty-five|"
                         r"twenty-six|twenty-seven|twenty-eight|twenty-nine|thirty))\b"),
        ("section",      r"^\s*(\d+\.\d+)(?:\s+\w)"),
        ("procedure",    r"^\s*(procedure|to\s+(?:configure|install|set\s+up|enable|disable))\b"),
        ("appendix",     r"^\s*(appendix(?:\s+[a-z])?)\b"),
        ("glossary",     r"^\s*(glossary|terminology|definitions?)\b"),
        ("index",        r"^\s*(index)\b"),
        ("references",   r"^\s*(bibliography|references)\b"),
    ],
    document_id_from_filename=_manual_document_id,
    extract_doc_metadata=_manual_extract_meta,
    build_card=_manual_build_card,
    # Index + glossary are *primary* search targets for manuals (keyword
    # lookup, term definition). Only bibliographic noise is excluded.
    exclude_from_default_retrieval={"references"},
    chunk_size=1200,                          # larger; procedures often span multiple steps
    chunk_overlap=120,
    card_budget_chars=1500,
    classify_content_type=True,
    extract_entities=False,                    # NER not useful for technical prose
    # Atomic regions: keep procedures and code blocks coherent. The
    # detection regex captures the WHOLE block (header + steps) so
    # the splitter can preserve it as one chunk.
    atomic_block_patterns=[
        # Numbered-step procedure: "1. Foo\n2. Bar\n3. Baz" with at least 3 steps
        ("procedure_block",
         r"(?:^|\n)(?:\s*\d+\.\s+[^\n]+\n){3,}"),
        # Fenced code block ```...```
        ("code_fence",
         r"```[a-z]*\n[\s\S]*?\n```"),
        # 4-space-indented code: 3+ consecutive indented lines
        ("code_indent",
         r"(?:^|\n)(?:    [^\n]+\n){3,}"),
    ],
    atomic_block_ceiling=4000,
    synopsize=True,
    synopsize_sections=["chapter"],
    synopsis_prompt=(
        "You are creating a per-chapter synopsis for a technical manual that "
        "will be used as a retrieval card during semantic search. Read the "
        "passage and produce a single self-contained paragraph (4-6 sentences) "
        "that names the system/component covered, the principal tasks the "
        "chapter teaches, any prerequisites or warnings, and the procedures or "
        "configuration changes the reader will be able to perform after reading. "
        "Be specific and concrete; do not editorialise; do not preface; output "
        "only the synopsis text.\n\nPASSAGE:\n{text}\n\nSYNOPSIS:"
    ),
    synopsis_max_input_chars=14000,
    synopsis_max_output_tokens=360,
))
