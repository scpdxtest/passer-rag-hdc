# Blockchain-anchored audit trail for multi-LLM paper scoring

**Design note for adding Antelope (Spring) on-chain traceability to the
paper-scoring component**, mapped onto the existing PaSSER blockchain
patterns and the `Crypto-Primitives-Upgrade-Plan.md` BLS upgrade.

---

## 1. Why anchor scoring results on chain

The paper-scoring pipeline already produces immutable artefacts —
per-row CSV with `paper_id`, `llm_model`, `embed_model`, `collection`,
`score`, `justification`, `evidence`, `run_started_at` — but these
artefacts live in a CSV file on the analyst's laptop. For a paper that
*claims* "we scored corpus C with judges {gemma3, qwen3, llama3.3}
against criteria Φ on date D and obtained ranking R", a reviewer has
no way to verify that the artefacts weren't edited after the fact.

The same gap that `sraudit` closes for screening exists for scoring,
plus one extra: where screening produces a *binary include/exclude* per
paper, scoring produces a *real-valued score per (paper, criterion,
judge)*. Slight tweaks to the score are very hard to spot
post-hoc — the perfect surface for unconscious result-shopping.

What we want from the chain:

1. **Existence & ordering proof.** "This (paper, criterion, judge) score
   existed at this exact value on this date." Same shape as
   `sraudit::logdecision`.
2. **Run-level commit before reveal.** "These judges, these criteria,
   this corpus hash were fixed *before* the scoring started." Closes
   the door on retroactively swapping criteria or judges to flatter a
   chosen LLM. Uses BLS commit-reveal (Track 3 in the upgrade plan).
3. **Aggregated multi-judge signatures.** "All N judges in the
   reported set actually produced these scores; the analyst didn't
   silently drop one." Uses BLS aggregated signatures (Track 5).
4. **VRF-verifiable corpus sampling.** "The paper subset shown to the
   judges was selected by a verifiable random function of the corpus
   manifest, not cherry-picked." Same construction as Track 4 for
   screening gold-standard sampling.
5. **On-chain Merkle inclusion proof.** Per-row receipts an analyst (or
   reviewer) can verify against the chain *without* trusting the CSV.
   Track 2 from the upgrade plan.

Each of these maps cleanly onto a primitive already covered by the
upgrade plan; none requires invention of new chain features.

---

## 2. How blockchain is used today in this app (recap)

So you can see where the new component plugs in.

| Component | Role | Chain primitive used |
|---|---|---|
| `BCEndpoints.js` `checkBCEndpoint()` | Picks the first reachable Antelope node from `configuration.passer.BCEndPoints`. | `@wharfkit/antelope` v1 `chain.get_info`. |
| `TestWharf.js` | Anchor-wallet login. Stores actor name in `localStorage.wharf_user_name`. | `@wharfkit/session` + `WalletPluginAnchor`. |
| `backEnd.py` `/metrics` | After computing BERT-RT etc., pushes an `addtest` action to contract `llmtest` with `creator`, `testid`, `description`, `results[float64]`. | `pyntelope` direct push, signed with the worker's key. |
| `Screening.js`, `LLMScreening.js`, `AdminDashboard.js`, `UserActionLog.js` | Show per-row `transaction_id`, fetch decoded payload from `configuration.passer.MementoAPI` (`http://blockchain2.uni-plovdiv.net:9909/wax/get_transaction?trx_id=...`). | Read-only HTTP from "Memento" indexer. |
| `Crypto-Primitives-Upgrade-Plan.md` | Forward design for `sraudit` v3.x using `k1_recover`, `sha3`, BLS pairing/mul/add, hash-to-curve. | Tracks 1-5 in that document. |

**Three reusable building blocks** the new scoring audit can sit on:

- `configuration.passer.BCEndPoints` — already the source of truth for
  the chain URL.
- The `pyntelope`-based Python push from `backEnd.py` — same template
  works for a new contract account.
- The `TestWharf.js` Anchor-wallet flow — same login can sign scoring
  receipts when Track 1 is active.

---

## 3. Proposed on-chain contract: `sscore` ("scoring-audit")

Companion contract to `sraudit`. Three tables, eight actions. Same
account-level deployment model.

### 3.1 Tables

```cpp
// One row per scoring "run" (one click of "Run scoring" in the UI,
// scoped to a single judge LLM over a chosen paper subset & criteria
// set).
struct [[eosio::table]] run_row {
    uint64_t        run_id;            // primary key, auto-assigned
    name            analyst;           // wharf_user_name
    name            llm_model;         // e.g. "gemma3.12b" (Antelope-name encoded)
    name            embed_model;       // e.g. "mxbai.large"
    std::string     collection;        // Chroma collection name
    std::string     corpus_hash;       // sha3 of ingestion manifest
    std::string     criteria_hash;     // sha3 of criteria JSON
    std::string     params_hash;       // sha3(json of {k, theta, gamma, temperature, prompt_template_version})
    std::string     sample_seed;       // for VRF-derived paper subsets (optional)
    uint32_t        n_papers;
    uint32_t        n_criteria;
    uint64_t        started_at;        // seconds since epoch
    uint64_t        finished_at;       // seconds since epoch
    checksum256     rows_root;         // Merkle root over per-row receipts (Track 2)
    checksum256     rows_root_bitcoin; // OpenTimestamps anchor (filled async)
    bool            sealed;
    uint64_t primary_key() const { return run_id; }
    uint64_t by_analyst()  const { return analyst.value; }
};

// One row per (run, paper, criterion). Heavy table — kept summarised:
// the chain stores the digest, not the prose.
struct [[eosio::table]] cell_row {
    uint64_t        cell_id;
    uint64_t        run_id;
    std::string     paper_id;
    name            criterion_id;
    uint8_t         score;             // 0..255 (>= any practical scale)
    uint8_t         scale_max;         // upper bound of the criterion's scale
    checksum256     payload_hash;      // sha3 of canonical JSON {paper, criterion, score, justification, evidence, source}
    std::string     mongo_oid;         // optional: MongoDB ObjectId of the full payload (no IPFS)
    uint64_t        ts;
    uint64_t primary_key()    const { return cell_id; }
    uint64_t by_run()         const { return run_id; }
    fixed_bytes<32> by_hash() const { return payload_hash.extract_as_byte_array(); }
};

// One row per judge participating in a "study" (a set of runs over the
// same corpus_hash + criteria_hash). Used for aggregate BLS signatures
// at study seal time (Track 5).
struct [[eosio::table]] judge_row {
    uint64_t        judge_id;
    checksum256     study_id;          // sha3(corpus_hash || criteria_hash || analyst)
    name            llm_model;
    checksum256     pubkey_g1_x;
    checksum256     pubkey_g1_y;
    bool            signed_off;
    uint64_t primary_key() const { return judge_id; }
    fixed_bytes<32> by_study() const { return study_id.extract_as_byte_array(); }
};
```

### 3.2 Actions

| Action | Auth | Purpose | Crypto |
|---|---|---|---|
| `startrun(analyst, llm, embed, collection, corpus_hash, criteria_hash, params_hash, sample_seed?, n_papers, n_criteria)` | `analyst@active` (via Anchor) | Opens a `run_row`. Commits the experimental setup *before* any score is computed. | k1_recover for analyst sig (Track 1). |
| `logcell(run_id, paper_id, criterion_id, score, scale_max, payload_hash, mongo_oid?, sig)` | `worker@active` (Track 1 also lets analyst sign) | One per scored cell. Streams as the run progresses. | k1_recover for analyst-signed variant. |
| `sealrun(run_id, rows_root)` | `analyst@active` | Closes the run, commits the Merkle root of all `payload_hash` values. Run is then immutable. | sha3 / k1_recover. |
| `verifycell(run_id, payload_hash, merkle_path, path_bits)` | anyone (read-only) | On-chain Merkle inclusion proof for a single cell against `rows_root`. | sha3 (Track 2). |
| `commitvote(run_id, judge_id, commit_g1)` | judge owner | Pedersen commit to the judge's score *vector* before reveal (multi-judge studies). | bls_g1_mul + bls_g1_add (Track 3). |
| `revealvote(run_id, judge_id, scores[], randomness)` | judge owner | Opens the commit; chain verifies. | bls_g1_mul + bls_g1_add. |
| `aggsign(study_id, aggregated_sig_g2)` | analyst | Posts one 96-byte BLS aggregate signature collected from all `judge_row` entries. | bls_pairing (Track 5). |
| `vrfsample(corpus_hash, seed_commitment, sample_proof, indices)` | analyst | Logs a VRF-verified random subset of the corpus (optional). | bls_g1_mul + bls_pairing (Track 4). |

### 3.3 Canonical payload (what `payload_hash` covers)

The hash is computed *off-chain* over a strictly-ordered JSON, so
reviewers can recompute it. Schema:

```json
{
  "v": 1,
  "run_id": 17,
  "paper_id": "001",
  "criterion_id": "topical_fit",
  "score": 4,
  "scale": "0-5",
  "source": "body",
  "justification": "<verbatim>",
  "evidence": "<verbatim>",
  "llm_model": "gemma3:12b",
  "embed_model": "mxbai-embed-large",
  "collection": "papers_corpus__mxbai",
  "run_started_at": "2026-05-21T10:42:00Z"
}
```

Hash = `sha3(canonical_json_bytes)`. The chain stores only the 32-byte
hash + an optional MongoDB ObjectId pointing to the document persisted
in the existing MongoDB instance (no IPFS).

---

## 4. Client-side flow

Mirrors the existing PaSSER pattern: the React UI computes locally, then
calls a Python worker that pushes the transaction.

### 4.1 Where to add hooks in the existing code

| File | Hook | Action |
|---|---|---|
| `scorePapersBat.js` `runScoring` (start) | After capturing `runLlm`, `runEmbed`, `runStartedAt` | `POST {IngestAPI}/chain/startrun` with the canonical setup. Store the returned `run_id`. |
| `scorePapersBat.js` `runScoring` (per row) | Inside the for-loop, after `setRows(...)` | `POST {IngestAPI}/chain/logcell` with `{run_id, paper_id, criterion_id, score, scale_max, payload}`. Worker computes the hash and pushes. |
| `scorePapersBat.js` `runScoring` (end) | After loop, before `setRunning(false)` | Compute Merkle root locally from row hashes. `POST {IngestAPI}/chain/sealrun` with `{run_id, rows_root}`. |
| `scorePapersBat.js` UI table | Per-row | Show a small Tag with `payload_hash[:8]` + click-through to `MementoAPI?trx_id=...`, identical to `LLMScreening.js`. |
| `dbFromCorpusPapers.js` (finish handler) | When ingestion finishes | `POST {IngestAPI}/chain/logimport` with `{collection, embed_model, corpus_hash, n_papers, manifest_cid}`. Anchors the *vector store* itself, not just the scores. |

### 4.2 The Python worker side

`newRAG/ingest_corpus.py` already runs Flask. Add a `/chain/*` blueprint
that wraps `pyntelope` exactly the way `backEnd.py:446-488` does today,
except pointing at the new `sscore` account:

```python
# newRAG/chain_bridge.py  (new file, imported by ingest_corpus.py)
import json, hashlib, pyntelope
from flask import Blueprint, jsonify, request
import configuration  # reads passer.BCEndPoints[0].url

bp = Blueprint("chain", __name__)
CONTRACT = "sscore"
SIGNING_KEY = os.environ["SSCORE_SIGNING_KEY"]   # admin key (Track-1 work moves this client-side)


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sha3_hex(b: bytes) -> str:
    return hashlib.sha3_256(b).hexdigest()


def push(action_name: str, data: list[pyntelope.Data], actor: str = "sscore"):
    auth = pyntelope.Authorization(actor=actor, permission="active")
    action = pyntelope.Action(account=CONTRACT, name=action_name,
                              data=data, authorization=[auth])
    tx = pyntelope.Transaction(actions=[action])
    net = pyntelope.Net(host=configuration.passer.BCEndPoints[0]["url"])
    signed = tx.link(net=net).sign(key=SIGNING_KEY)
    resp = signed.send()
    return resp.get("transaction_id"), resp


@bp.route("/chain/startrun", methods=["POST"])
def startrun():
    j = request.get_json()
    data = [
        pyntelope.Data(name="analyst",       value=pyntelope.types.Name(j["analyst"])),
        pyntelope.Data(name="llm_model",     value=pyntelope.types.Name(name_safe(j["llm_model"]))),
        pyntelope.Data(name="embed_model",   value=pyntelope.types.Name(name_safe(j["embed_model"]))),
        pyntelope.Data(name="collection",    value=pyntelope.types.String(j["collection"])),
        pyntelope.Data(name="corpus_hash",   value=pyntelope.types.String(j["corpus_hash"])),
        pyntelope.Data(name="criteria_hash", value=pyntelope.types.String(j["criteria_hash"])),
        pyntelope.Data(name="params_hash",   value=pyntelope.types.String(j["params_hash"])),
        pyntelope.Data(name="n_papers",      value=pyntelope.types.Uint32(j["n_papers"])),
        pyntelope.Data(name="n_criteria",    value=pyntelope.types.Uint32(j["n_criteria"])),
    ]
    trx_id, resp = push("startrun", data)
    return jsonify({"trx_id": trx_id, "raw": resp})


@bp.route("/chain/logcell", methods=["POST"])
def logcell():
    j = request.get_json()
    payload = j["payload"]
    payload_hash = sha3_hex(canonical(payload))
    # Persist the full payload in MongoDB (same instance as backEnd.py)
    mongo_oid = mongo_db["sscore_cells"].insert_one({
        **payload, "payload_hash": payload_hash, "run_id": j["run_id"],
    }).inserted_id
    data = [
        pyntelope.Data(name="run_id",       value=pyntelope.types.Uint64(j["run_id"])),
        pyntelope.Data(name="paper_id",     value=pyntelope.types.String(payload["paper_id"])),
        pyntelope.Data(name="criterion_id", value=pyntelope.types.Name(name_safe(payload["criterion_id"]))),
        pyntelope.Data(name="score",        value=pyntelope.types.Uint8(int(payload["score"]))),
        pyntelope.Data(name="scale_max",    value=pyntelope.types.Uint8(int(payload["scale"].split("-")[1]))),
        pyntelope.Data(name="payload_hash", value=pyntelope.types.Checksum256(payload_hash)),
        pyntelope.Data(name="mongo_oid",    value=pyntelope.types.String(str(mongo_oid))),
    ]
    trx_id, _ = push("logcell", data)
    return jsonify({"trx_id": trx_id, "payload_hash": payload_hash, "mongo_oid": str(mongo_oid)})


@bp.route("/chain/sealrun", methods=["POST"])
def sealrun():
    j = request.get_json()
    rows_root = merkle_root(j["payload_hashes"])
    data = [
        pyntelope.Data(name="run_id",    value=pyntelope.types.Uint64(j["run_id"])),
        pyntelope.Data(name="rows_root", value=pyntelope.types.Checksum256(rows_root)),
    ]
    trx_id, _ = push("sealrun", data)
    return jsonify({"trx_id": trx_id, "rows_root": rows_root})
```

`name_safe(s)` lowercases and replaces invalid characters so `gemma3:12b`
becomes the valid Antelope `name` `gemma3.12b` (Antelope `name` allows
only `a-z 1-5 .`, max 12 chars — `criterion_id` must be designed within
that constraint; this is why §3 makes them `name` rather than
`string`).

### 4.3 CSV export — chain receipts

The existing CSV export already has the right shape; we just add four
columns:

| column | meaning |
|---|---|
| `chain_trx_id` | Antelope tx id of the `logcell` push |
| `payload_hash` | sha3 hex (same as on chain) |
| `chain_run_id` | the `sscore::run_row.run_id` this cell belongs to |
| `mongo_oid`    | MongoDB ObjectId of the full payload (for the faithfulness audit) |

A reviewer can then take any CSV row, recompute the canonical payload
hash locally, fetch the on-chain `cell_row` by `payload_hash`, and call
`verifycell` with the Merkle path to prove the row was in the sealed
run. No trust in the analyst or the CSV file.

---

## 5. How each BLS primitive in the upgrade plan maps here

This is the part that goes straight into the methodology paper.

### 5.1 Track 1 (k1_recover) — analyst signatures on cells

**Use:** every `logcell` is signed by the analyst's Anchor wallet, not
the worker's hot key. The contract calls `k1_recover` on the signature
plus the canonical hash, derives the public key, looks up
`analysts` table, refuses the action if it doesn't match.

**Effect on the paper:** the audit trail names the human analyst, not a
service account. "Analyst Boni signed run #17 on 2026-05-21" becomes
a cryptographic statement, not just metadata.

**Status:** doesn't require Phase D. Can ship today.

### 5.2 Track 2 (sha3 Merkle) — verifiable per-cell receipts

**Use:** at `sealrun`, the worker hashes the ordered list of
`payload_hash` values into a binary Merkle tree, posts the root.
`verifycell(run_id, payload_hash, merkle_path, path_bits)` re-derives
the root on chain and compares.

**Effect on the paper:** "the CSV row at index $i$ for paper $P$ under
criterion $c$ scored by judge $L$ is provably part of the sealed run
$R$" becomes an on-chain function call. Combine with the existing
OpenTimestamps Bitcoin anchor over `rows_root` (already used by
`sraudit`) and you have **chain says: was in the tree at this block;
Bitcoin says: tree existed at this time**. Same construction the
upgrade plan recommends for screening.

### 5.3 Track 3 (Pedersen commit-reveal) — multi-judge independence

**Use:** when comparing $J \geq 2$ judges, each judge's score *vector*
(one integer per criterion per paper) is committed before any score
is revealed.

Mechanism:

1. After `startrun`, each judge $j$ runs locally over the agreed paper
   set. Let $\mathbf s_j \in \{0,\dots,5\}^{N \cdot M}$ be the
   judge's score vector (flattened).
2. Judge computes $h_j = \text{sha3}(\mathbf s_j)$ interpreted as a
   scalar. Picks random $r_j$.
3. Posts `commitvote(run_id, judge_id, commit_g1)` where
   $\text{commit\_g1} = h_j \cdot G + r_j \cdot H$.
4. **No judge can see another judge's score vector before all commits
   are on chain** — `commitvote` rejects after the run's commit
   window closes.
5. After all commits, each judge calls `revealvote(run_id, judge_id,
   scores[], randomness)`. Contract reconstructs the commitment and
   verifies.

**Effect on the paper:** inter-judge agreement metrics (Cohen's $\kappa$,
Krippendorff's $\alpha$ from §7 of `PAPER_METHODOLOGY.md`) computed
under cryptographically-enforced independence carry a substantially
stronger interpretation. Direct parallel to Track 3's screening
contribution.

**Status:** requires Phase D (BLS_PRIMITIVES2 active). Aligned with the
upgrade plan's "paper-grade contribution" track.

### 5.4 Track 4 (VRF) — verifiable paper subset selection

**Use:** when scoring a subset of the corpus (e.g. for a pilot run or
to keep cost tractable), the subset is selected by a VRF:

```
seed_commit  = sha3(study_id || timestamp || external_randomness)
indices      = VRF_sample(seed_commit, n_papers, sample_size)
proof        = VRF_prove(secret_key, seed_commit)
```

`vrfsample(corpus_hash, seed_commit, proof, indices)` records the
selection. Anyone with the corpus manifest and the chain transaction
can verify that the indices follow uniquely from the seed.

**Effect on the paper:** "the 50 papers we benchmarked the judges on
were sampled by VRF, not cherry-picked" is a chain-verifiable claim.

**Status:** requires Phase D.

### 5.5 Track 5 (BLS aggregate signatures) — multi-judge study seal

**Use:** at the end of a multi-judge study, every participating judge
account submits a BLS G2 signature over `study_id || final_rows_root`.
The analyst calls `aggsign(study_id, aggregated_sig_g2)` with the
single aggregated 96-byte signature. The contract verifies via
`bls_pairing` against the concatenation of registered G1 keys from
`judge_row`.

**Effect on the paper:** "all reported judges actually signed off on
this study, none was silently dropped after the run" — verifiable
with one pairing check.

**Status:** requires Phase D.

---

## 6. Recommended adoption sequence

Mirrors the upgrade plan's ordering so the two contracts can share the
ramp-up work.

### Phase D-independent (ship now)

1. **`sscore` v0.1**: `startrun`, `logcell`, `sealrun`, `verifycell`,
   `logimport`. No commits, no aggregates. Server-side key only.
   Wire the three React hooks (§4.1) and the three CSV columns
   (§4.3). Reviewers can verify per-cell inclusion against the
   sealed Merkle root.
2. **Add Track 1**: `k1_recover` for analyst signatures. The Anchor
   wallet flow already lives in `TestWharf.js` — the work is in the
   contract and a small signing helper added to `scorePapersBat.js`.
3. **Hash-chained import receipts**. `logimport` includes the previous
   `logimport`'s tx hash for the same `collection`. Trivial — gives
   the audit a tamper-evident sequence.

### Phase D-dependent (after BLS_PRIMITIVES2)

4. **Track 5 first** (BLS aggregate signatures for study seal) — low
   effort, immediate hardening.
5. **Track 4** (VRF sampling) — strengthens the experimental
   protocol's claim that the paper sample was unbiased.
6. **Track 3** (Pedersen commit-reveal across judges) — the
   paper-grade contribution. Pairs naturally with §7.2 (inter-judge
   agreement) in `PAPER_METHODOLOGY.md`.

### Effort estimate per track (contract + worker + UI)

| Track | Contract | Python worker | React UI | Total |
|---|---|---|---|---|
| `sscore` v0.1 (logcell, sealrun, verifycell) | 4 d | 2 d | 2 d | ~1.5 wk |
| Track 1 (analyst k1_recover) | 2 d | 0 d | 2 d | ~1 wk |
| Track 5 (BLS aggregate seal) | 3 d | 1 d | 1 d | ~1 wk |
| Track 4 (VRF sampling) | 5 d | 2 d | 1 d | ~2 wk |
| Track 3 (commit-reveal independence) | 7 d | 3 d | 5 d | ~3 wk |

---

## 7. What this lets the paper claim

Slotting into the table from `Crypto-Primitives-Upgrade-Plan.md` §"Mapping to paper":

| Section of follow-up paper | Without sscore | With sscore (v0.1 + Track 1) | With Phase-D tracks |
|---|---|---|---|
| §Architecture | "Local CSV files of scores" | "Cryptographically-anchored per-cell receipts on an Antelope private chain, with Bitcoin time-anchoring" | (same) |
| §Methodology | "We ran judges $j_1, j_2, j_3$ on papers $\mathcal P$ with criteria $\mathcal C$" | "Run setup committed *before* scoring (corpus hash, criteria hash, judge identity), analyst-signed" | "...with commit-reveal between judges so independence is cryptographically enforced" |
| §Experimental sample | "We scored 50 randomly selected papers" | (same) | "We scored 50 papers selected by VRF; the proof is on chain" |
| §Reproducibility | "We provide the CSV and the criteria JSON" | "Reviewers can verify any CSV row's inclusion in the sealed run via `sscore::verifycell`" | "+ verify the multi-judge study was sealed by all reported judges via one BLS pairing check" |
| §Threats to validity (results-shopping) | "We did not modify scores post-hoc" (claim) | "Scores were committed cell-by-cell as produced; modifications would be detectable on chain" (proof) | "+ judges could not see each other's scores during scoring" |

The "with Phase-D tracks" column is what differentiates a system that
produces verifiable scores from one that just produces *signed* scores
— and is exactly the gap the upgrade plan was written to close for
`sraudit`. The work needed here is incremental on top of that plan,
not a parallel effort.

---

## 8. Settled design decisions

The five open questions were closed in conversation on 2026-05-21:

1. **Contract name: `sscore`.** Companion to `sraudit` rather than an
   extension; keeps the per-row table small, avoids a `sraudit`
   migration, and the actor `sscore@active` is a natural namespace for
   the analyst signing key. All actions in §3.2 sit on this account.

2. **No IPFS.** The chain stores `payload_hash` only. Full canonical
   payloads (including `justification` and `evidence` strings) are
   persisted server-side to **MongoDB**, in the same pattern that
   `backEnd.py`'s `metrics_collection.insert_one` already uses. The
   `ipfs_cid` field in `cell_row` is therefore removed; the row now
   carries an optional `mongo_oid` string instead, for analysts who
   want a direct database pointer alongside the on-chain hash. The
   faithfulness audit (`PAPER_METHODOLOGY.md` §7.5) operates on the
   MongoDB-resident payloads with the chain-stored `payload_hash` as
   the integrity check — same trust model as the existing screening
   `datahash` flow.

3. **`criterion_id` keeps the current shape (Antelope `name` already
   accommodates the defaults).** `topical_fit`, `method_rigor`,
   `evaluation`, `novelty` are all valid `name`s. A lint step is added
   to `scorePapersBat.js` at criteria-JSON load time that flags any id
   exceeding 12 chars or containing characters outside `[a-z1-5.]`,
   refuses the load, and points the user to the constraint. No
   change to the JSON schema itself.

4. **Streaming `logcell`.** One transaction per scored cell, pushed
   from the Python worker as the score is computed. At $N \cdot M$
   cells per run (typical: $50 \times 4 = 200$, worst observed in
   the corpus: $565 \times 8 \approx 4500$), the chain handles this
   without batching. The visible benefit is per-row transaction IDs
   appearing in the UI table in real time, matching the existing
   `LLMScreening.js` UX pattern.

5. **Hybrid key custody.** Anchor wallet signs `startrun` and
   `sealrun`; the worker's hot key signs the streamed `logcell`s in
   between. The contract enforces this via `require_auth(analyst)` on
   the boundary actions and `require_auth(worker_account)` on
   `logcell`, plus a `cell_row.run_id` foreign-key check against the
   open run. This is the §4.2 recommended pattern.

These five decisions are reflected in the code sketches and the
adoption sequence in §6.

---

## 9. Cross-references

- Crypto primitive details and feature-flag status:
  [`Crypto-Primitives-Upgrade-Plan.md`](./Crypto-Primitives-Upgrade-Plan.md)
- Method spec this audit trail anchors:
  [`PAPER_METHODOLOGY.md`](./PAPER_METHODOLOGY.md)
- User-facing description of the scoring pipeline:
  [`EXPLANATION.md`](./EXPLANATION.md)
- Existing chain integration:
  [`src/component/BCEndpoints.js`](../src/component/BCEndpoints.js),
  [`src/component/TestWharf.js`](../src/component/TestWharf.js),
  [`backEnd.py`](../backEnd.py) (the `addtest` push at lines 446-488).
- Where Memento indexer reads decoded transactions (mirror this for
  `sscore`): [`src/component/Screening.js`](../src/component/Screening.js)
  around line 772.

---

*End of integration plan.*
