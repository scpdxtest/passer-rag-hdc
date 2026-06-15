# BLS primitive roadmap for the `sscore` audit trail

**Forward-looking design notes for v0.3 and beyond.** Drafted
2026-05-21 against the v0.2 Track 1 implementation. Decision pending.

## 0. Where we are

| Version | Status | Crypto in use | Audit-trail claim level |
|---|---|---|---|
| v0.1 | shipped | SHA-256, `eosio::pack` | "every score on chain, Merkle-rooted, time-anchored" |
| v0.2 (Track 1) | shipped | + secp256k1 (`recover_key`) | + "every run-boundary signed by the named human analyst" |
| **v0.3+** | **this doc** | + BLS12-381 (`bls_*`) | depends on which track is chosen |

BLS host functions in `BLS_PRIMITIVES2` are already activated on the
chain. The `sscore` contract has never invoked one. This document
lays out three candidate v0.3 paths plus two longer-horizon options,
each scoped enough to estimate effort.

## 1. What BLS gives us

The BLS12-381 pairing-friendly curve over Antelope's `BLS_PRIMITIVES2`
host functions provides:

- **Aggregate signatures.** Multiple parties' signatures over the same
  message compose into one 96-byte signature, verifiable in one
  pairing check. Adds *signer-count-independent* group attestations.
- **Pedersen commitments.** Commit to a value $a$ with a blinding
  scalar $r$ as $c = a G + r H$ on $G_1$; reveal by providing $a, r$;
  contract checks $c$ from the reveal. Used to enforce *commit before
  reveal* between independent parties.
- **Verifiable Random Functions (VRF).** A holder of secret scalar
  $x$ can publicly prove that the output of a deterministic hash
  function on input $m$ is $x \cdot H(m) \in G_1$ — anyone with the
  matching pubkey can verify, but only the holder of $x$ could have
  produced the output. Used for *verifiable random sampling*.
- **Threshold signatures.** $k$-of-$n$ aggregation where $k$ of $n$
  named parties suffice to validate.
- **Algebraic building blocks** for zk-SNARK-friendly constructions
  (KZG commitments, polynomial IOPs, etc.) — research horizon.

The host-function surface relevant to us (signatures may differ
slightly across CDT versions):

```cpp
void bls_g1_add (const bls_g1& a, const bls_g1& b, bls_g1& res);
void bls_g2_add (const bls_g2& a, const bls_g2& b, bls_g2& res);
void bls_g1_mul (const bls_g1& p, const bls_scalar& s, bls_g1& res);
void bls_g2_mul (const bls_g2& p, const bls_scalar& s, bls_g2& res);
void bls_g1_weighted_sum (const bls_g1* pts, const bls_scalar* ss,
                          uint32_t n, bls_g1& res);
void bls_g2_weighted_sum (const bls_g2* pts, const bls_scalar* ss,
                          uint32_t n, bls_g2& res);
void bls_pairing (const bls_g1* g1, const bls_g2* g2,
                  uint32_t n, bls_gt& res);
void bls_g1_map  (const bls_fp& msg, bls_g1& res);   // hash-to-curve G1
void bls_g2_map  (const bls_fp2& msg, bls_g2& res);  // hash-to-curve G2
void bls_fp_mod  (...);   // scalar field reductions
void bls_fr_mod  (...);
```

All values are byte arrays of fixed size: $G_1$ = 96 bytes
(uncompressed), $G_2$ = 192 bytes (uncompressed), scalar = 32 bytes.

## 2. Track 4 — VRF-verifiable paper-subset sampling

The highest-leverage BLS feature for *this* methodology. Closes the
"how do we know the subset wasn't cherry-picked" gap that affects
every benchmark paper that scores a sample rather than the full corpus.

### 2.1 Goal

Given a corpus of $N$ papers committed via `logimport`
(`corpus_hash`), produce a deterministic, publicly-verifiable random
subset of size $K$. A third party with the corpus manifest, the
analyst's BLS pubkey, and the VRF proof can recompute the same
$K$ indices and confirm they follow uniquely from the seed.

### 2.2 Construction (VRF on BLS12-381)

Following Boneh, Lynn, and Shacham (BLS) + Boneh and Boyen:

- **Setup.** Analyst holds a BLS secret scalar $x \in \mathbb{F}_r$.
  Publishes $X = x \cdot G_2 \in G_2$ (their VRF public key,
  registered on chain via an `analysts`-style table or a new
  `vrfkeys` table).
- **Seed.** Once `corpus_hash` is committed via `logimport`, the
  analyst picks $\text{seed} \in \mathbb{F}_r$ uniformly (or
  derives it deterministically from `corpus_hash || ts`).
- **Evaluation.** Hash the seed to $G_1$:
  $H = \text{hash\_to\_G}_1(\text{corpus\_hash} \,\|\, \text{seed})$
  (via `bls_g1_map`). Then $Y = x \cdot H \in G_1$. $Y$ is the VRF
  output and also the proof; $X$ is the (already-published) pubkey.
- **Verification (on chain).** Anyone checks
  $e(Y, G_2) = e(H, X)$ via one `bls_pairing`. If true, $Y$ was
  produced by the holder of $x$ over input
  $\text{corpus\_hash} \,\|\, \text{seed}$.
- **Sampling.** Treat $Y$ as a 96-byte seed for a deterministic
  RNG; XOF-expand to $K$ uniformly-random integers in $[0, N)$ with
  rejection-sampling for unbiased uniform distribution. Equivalently,
  use $\text{sha256}(Y \,\|\, i)$ for $i \in 0, \dots, K-1$ and take
  $\text{int}(\cdot) \mod N$ with rejection-sampling for collisions.

### 2.3 Contract surface

```cpp
struct [[eosio::table]] vrfkey_row {
    name        account;
    bls_g2      pubkey_g2;        // X = x*G2
    bool        active;
    uint64_t primary_key() const { return account.value; }
};
typedef multi_index<"vrfkeys"_n, vrfkey_row> vrfkeys_table;

struct [[eosio::table]] sample_row {
    uint64_t        id;
    name            analyst;
    checksum256     corpus_hash;
    checksum256     seed;
    bls_g1          proof_y;          // Y = x*H(corpus_hash||seed)
    uint32_t        n_total;          // |corpus|
    uint32_t        n_sample;         // |subset|
    std::vector<uint32_t> indices;    // the actual subset (deterministic from proof_y)
    time_point_sec  ts;
    uint64_t primary_key() const { return id; }
};
typedef multi_index<"samples"_n, sample_row> samples_table;

[[eosio::action]]
void setvrfkey(name analyst, bls_g2 pubkey_g2, bool active) {
    require_auth(get_self());
    /* upsert into vrfkeys_table */
}

[[eosio::action]]
void vrfsample(name analyst, checksum256 corpus_hash, checksum256 seed,
               bls_g1 proof_y, uint32_t n_total, uint32_t n_sample,
               std::vector<uint32_t> indices) {
    require_auth(get_self());

    // 1. Look up analyst's VRF pubkey.
    vrfkeys_table k(get_self(), get_self().value);
    auto vk = k.require_find(analyst.value, "no vrf key registered");
    check(vk->active, "vrf key deactivated");

    // 2. Hash-to-curve: H = hash_to_G1(corpus_hash || seed).
    bls_fp msg;
    auto preimage = pack(std::make_tuple(corpus_hash, seed));
    auto h = sha256(preimage.data(), preimage.size());
    bls_fp_mod(reinterpret_cast<const char*>(&h), 32, msg);
    bls_g1 H_g1;
    bls_g1_map(msg, H_g1);

    // 3. Pairing check: e(Y, G2) == e(H, X).
    //    Equivalent: e(Y, G2) * e(-H, X) == 1, single pairing call.
    bls_g1 negH;
    bls_g1_neg(H_g1, negH);            // pseudo — use g1_mul by -1
    bls_g1 pairing_g1[2]   = { proof_y, negH };
    bls_g2 pairing_g2[2]   = { G2_GENERATOR, vk->pubkey_g2 };
    bls_gt result;
    bls_pairing(pairing_g1, pairing_g2, 2, result);
    check(bls_gt_is_one(result), "VRF proof failed");

    // 4. Deterministically derive indices from proof_y and verify
    //    they match what the worker submitted (rejection sampling
    //    over sha256(proof_y || i) is reproducible).
    auto derived = derive_indices(proof_y, n_total, n_sample);
    check(derived == indices, "indices do not match VRF output");

    // 5. Store.
    samples_table s(get_self(), get_self().value);
    s.emplace(get_self(), [&](auto& r) {
        r.id          = s.available_primary_key();
        r.analyst     = analyst;
        r.corpus_hash = corpus_hash;
        r.seed        = seed;
        r.proof_y     = proof_y;
        r.n_total     = n_total;
        r.n_sample    = n_sample;
        r.indices     = indices;
        r.ts          = time_point_sec(current_time_point());
    });
    print("VRFSAMPLE|analyst=", analyst, "|n=", n_sample);
}
```

The index-derivation must be deterministic and identical on both
sides. The cheapest approach: $\text{idx}_i = (\text{sha256}(Y \,\|\, i) \bmod N')$
for an $N'$-bit window large enough to do rejection-sampling
without bias.

### 2.4 Worker side

Needs a Python BLS12-381 library. Two options:

- **`py_ecc`** (Ethereum Foundation, pure-Python) — ~5 ms per
  pairing on M1, easy install, mature. **Recommended.**
- **`blspy`** (Chia, libsecp256k1-style C bindings) — ~0.5 ms per
  pairing, requires compilation, less portable.

```python
# newRAG/vrf.py (new module)
from py_ecc.bls12_381 import G2, multiply, add, neg, pairing
from py_ecc.bls.hash_to_curve import hash_to_G1
import hashlib, struct

def vrf_eval(x_scalar: int, corpus_hash: bytes, seed: bytes):
    H = hash_to_G1(corpus_hash + seed, dst=b"sscore-vrf-v1")
    Y = multiply(H, x_scalar)
    return Y, H

def derive_indices(proof_Y, n_total: int, n_sample: int) -> list[int]:
    """Same algorithm the contract uses. Rejection-sampling over
    sha256(serialised(Y) || i32-counter) % nearest-power-of-two."""
    serialised = serialize_g1(proof_Y)
    out, seen, counter = [], set(), 0
    upper = 1 << (n_total.bit_length() + 32)   # comfortable margin
    while len(out) < n_sample and counter < 10 * n_sample:
        h = hashlib.sha256(serialised + struct.pack(">I", counter)).digest()
        idx = int.from_bytes(h, "big") % upper
        counter += 1
        if idx >= n_total:
            continue
        if idx in seen:
            continue
        seen.add(idx)
        out.append(idx)
    return sorted(out)
```

New endpoint `POST /chain/vrfsample` that takes
`{ corpus_hash, n_sample }`, generates the seed, evaluates the VRF,
derives indices, pushes `vrfsample`, and returns
`{ trx_id, seed, proof_y, indices }`.

### 2.5 UI

Add a *Sampling* card on the *Paper Scoring* page:

```
☐ Use full corpus
●  Sample N papers via VRF
       ┌─────┐
   N = │ 50  │
       └─────┘
   [ Generate sample ]   → pushes vrfsample on chain

   Last sample: 50 indices, proof trx 8af3c0...  [view]
```

When *Run scoring* is clicked, the `selectedPapers` list is the
indices from the last VRF sample (instead of the user's manual
selection).

### 2.6 Effort + paper impact

- Contract: ~150 LOC C++ + ~50 LOC for `derive_indices` (must match
  Python bit-for-bit). 3 days.
- Worker: `pip install py_ecc`, ~120 LOC new `vrf.py`, new endpoint.
  2 days.
- UI: small *Sampling* card + Dialog viewer for proof. 1 day.
- Integration testing (deterministic indices must agree between
  Python and the contract): 1 day.
- **Total: ~1 week.**

Paper claim it unlocks:
> *"The 50 papers we scored were selected by a verifiable random
> function over the sealed corpus manifest; the proof is recorded on
> chain at transaction X, allowing any reviewer to recompute the
> identical subset."*

This is the difference between *"random sample"* in §5 of the
methodology becoming a methodological contribution rather than a
footnote. Particularly powerful in combination with the rest of the
audit trail: the corpus itself was already hashed-and-anchored at
ingestion time (`logimport`); the sample is a verifiable function of
that anchor.

### 2.7 Risks

- **Hash-to-curve domain separation.** The DST string (`"sscore-vrf-v1"`)
  must be identical between Python and the contract. Versioning of
  the DST is mandatory for forward compatibility.
- **Index-derivation reproducibility.** The trickiest engineering
  detail. The contract and Python must agree byte-for-byte. We can
  short-circuit risk by having the contract just verify the indices
  the worker provides, rather than re-deriving — but that weakens the
  on-chain guarantee.
- **Off-chain BLS library quality.** `py_ecc` is well-maintained; if
  it goes stale, swapping to `blspy` is feasible.

## 3. Track 5 — BLS aggregate signatures for multi-party seals

Lower paper impact for single-analyst studies but a natural fit when
a paper has multiple institutional stakeholders (analyst + supervisor
+ external reviewer).

### 3.1 Goal

After `sealrun` is called, allow $n$ named parties to co-sign the
sealed root with a *single* 96-byte aggregated signature, verified
in one pairing on chain.

### 3.2 Construction

Standard BLS aggregate signatures:

- Each signer $i$ holds secret $x_i$, registered pubkey
  $X_i = x_i \cdot G_2 \in G_2$.
- For message $m$ = `sha256(run_id || rows_root)`, each $i$ computes
  $\sigma_i = x_i \cdot H_{G_1}(m)$ on $G_1$.
- Aggregated signature: $\sigma = \sum_i \sigma_i$ (point-add).
- Aggregated pubkey: $X = \sum_i X_i$ on $G_2$ (or verify against
  each $X_i$ separately for stronger non-repudiation).
- Verify: $e(\sigma, G_2) = e(H(m), X)$.

A standard rogue-key attack countermeasure is to use **proof-of-possession**
(each signer's pubkey is registered after they sign a known
challenge with the same key); we can lift the existing `analysts`
table to require this at registration time.

### 3.3 Contract surface

```cpp
struct [[eosio::table]] cosigner_row {
    name      account;
    bls_g2    pubkey_g2;        // X_i
    bls_g1    pop;              // proof-of-possession: signature on "POP|account"
    bool      active;
    uint64_t primary_key() const { return account.value; }
};

struct [[eosio::table]] aggregate_row {
    uint64_t       run_id;
    bls_g1         agg_sig;        // sigma
    std::vector<name> signers;     // ordered list of co-signers
    time_point_sec ts;
    uint64_t primary_key() const { return run_id; }
};

[[eosio::action]]
void setcosigner(name account, bls_g2 pubkey_g2, bls_g1 pop) { ... }

[[eosio::action]]
void aggsign(uint64_t run_id, bls_g1 agg_sig,
             std::vector<name> signers) {
    require_auth(get_self());
    runs_table runs(get_self(), get_self().value);
    auto run = runs.require_find(run_id, "unknown run_id");
    check(run->sealed, "run not yet sealed");

    // Compute m = sha256(run_id || rows_root) and H_G1(m).
    auto m = sha256(pack(std::make_tuple(run_id, run->rows_root)));
    bls_g1 H; bls_g1_map(/* fp from m */, H);

    // Aggregate pubkey: X = sum X_i for i in signers.
    cosigners_table c(get_self(), get_self().value);
    bls_g2 X = BLS_G2_ZERO;
    for (auto& signer : signers) {
        auto co = c.require_find(signer.value, "unknown cosigner");
        check(co->active, "cosigner deactivated");
        bls_g2 tmp;
        bls_g2_add(X, co->pubkey_g2, tmp);
        X = tmp;
    }

    // Verify: e(sigma, G2) == e(H, X).
    bls_g1 neg_sigma; /* g1_mul by -1 */;
    bls_g1 g1s[2] = { neg_sigma, H };
    bls_g2 g2s[2] = { G2_GENERATOR, X };
    bls_gt r; bls_pairing(g1s, g2s, 2, r);
    check(bls_gt_is_one(r), "aggregate signature invalid");

    // Persist.
    aggregates_table a(get_self(), get_self().value);
    a.emplace(get_self(), [&](auto& row) {
        row.run_id = run_id;
        row.agg_sig = agg_sig;
        row.signers = signers;
        row.ts = time_point_sec(current_time_point());
    });
}
```

### 3.4 Worker side

```python
# newRAG/bls_agg.py
from py_ecc.bls12_381 import G1, G2, multiply, add, neg, pairing
from py_ecc.bls.hash_to_curve import hash_to_G1

def cosigner_sign(x_i: int, run_id: int, rows_root: bytes) -> tuple:
    m = sha256(pack_uint64(run_id) + rows_root)
    H = hash_to_G1(m, dst=b"sscore-agg-v1")
    return multiply(H, x_i)   # sigma_i

def aggregate(sigmas: list) -> tuple:
    agg = sigmas[0]
    for s in sigmas[1:]:
        agg = add(agg, s)
    return agg
```

New endpoint `POST /chain/aggsign` that takes the list of cosigner
sigs (collected out-of-band — each cosigner runs their own `sign`),
aggregates them, and pushes `aggsign`.

### 3.5 UI

A *Co-signers* tab in the run-detail dialog where each cosigner can
upload their $\sigma_i$ (paste a base58 blob, or sign via Anchor if
Anchor exposes BLS — it doesn't today, so this is paste-only in v1).
Once all expected $\sigma_i$ are collected, a *Submit aggregate*
button pushes `aggsign`.

### 3.6 Effort + paper impact

- Contract: ~120 LOC. 2 days.
- Worker: `py_ecc`-based aggregation, new endpoint. 1 day.
- UI: cosigner collection dialog. 1 day.
- **Total: ~1 week.**

Paper claim it unlocks:
> *"Each reported study was sealed by an aggregated BLS signature
> from all participating analysts; the 96-byte aggregate is recorded
> at transaction X and can be verified with a single pairing check."*

Useful for cross-institutional collaborations. For single-analyst
studies, marginal value — Track 1 already gives single-analyst
attestation.

### 3.7 Risks

- **Rogue-key attack** unless proof-of-possession is enforced at
  cosigner registration. Standard countermeasure; needs explicit
  verification of POP at `setcosigner` time.
- **Cosigner key custody.** Same trust model as the analyst's key
  (mode B), but now for $n$ parties.
- **Anchor wallet doesn't currently support BLS sigs.** Cosigners
  sign with a hot key (paste / WIF) until wallet support lands.

## 4. Track 3 — Pedersen commit-reveal between judges

The original "paper-grade contribution" from
`Crypto-Primitives-Upgrade-Plan.md` §5.3, but in the scoring context
it only matters when *multiple human analysts* coordinate to run
different judge LLMs and you need to prove they didn't see each
other's scores before committing.

### 4.1 Goal

When $J \geq 2$ analysts run independent judge LLMs over the same
$(papers, criteria)$, each analyst commits cryptographically to
their entire score vector *before* any reveal. Once all commits are
on chain, none can change their mind based on the others' results.

### 4.2 Construction

Pedersen commitments on $G_1$ with two independent generators
$G, H$ (generated nothing-up-my-sleeve from `sscore-pedersen-v1`).

- **Commit phase.** Analyst $j$ has score vector
  $\mathbf{s}_j \in \{0, \dots, 5\}^{NM}$. Encode as a scalar
  $a_j = \text{int}(\mathbf{s}_j)$ or as a hash
  $a_j = \text{sha256}(\mathbf{s}_j) \bmod r$. Pick random
  $r_j \in \mathbb{F}_r$. Compute
  $c_j = a_j \cdot G + r_j \cdot H \in G_1$. Push
  `commitvote(study_id, analyst, c_j)` on chain.
- **Reveal phase.** Once all $J$ commits are recorded, each $j$
  pushes `revealvote(study_id, analyst, scores, r_j)`. The contract
  recomputes $a_j$ from the revealed scores, then
  $\hat{c}_j = a_j \cdot G + r_j \cdot H$, and refuses unless
  $\hat{c}_j = c_j$.

### 4.3 Contract surface

```cpp
struct [[eosio::table]] commit_row {
    checksum256  study_id;        // sha256(corpus_hash || criteria_hash || ...)
    name         analyst;
    bls_g1       commitment;
    time_point_sec ts;
    bool         revealed;
};

[[eosio::action]]
void commitvote(checksum256 study_id, name analyst, bls_g1 commitment) {
    require_auth(get_self());
    /* refuse if same (study_id, analyst) already committed */
    /* refuse if reveal phase has started — enforced by a study-phase flag */
}

[[eosio::action]]
void revealvote(checksum256 study_id, name analyst,
                std::vector<uint8_t> scores, bls_scalar r) {
    require_auth(get_self());
    auto cmt = /* find commit_row */;
    check(!cmt->revealed, "already revealed");

    // a = sha256(scores) mod r
    auto a_hash = sha256(scores.data(), scores.size());
    bls_scalar a; bls_fr_mod(/* from a_hash */, a);

    // Recompute commitment.
    bls_g1 aG, rH, sum;
    bls_g1_mul(G_GEN, a, aG);
    bls_g1_mul(H_GEN, r, rH);
    bls_g1_add(aG, rH, sum);

    check(sum == cmt->commitment, "reveal does not open the commitment");

    /* mark revealed; persist scores in a `revealed` table */
}
```

### 4.4 Worker side

```python
def commit(scores: list[int], r: int) -> tuple:
    a = int.from_bytes(sha256(bytes(scores)).digest(), "big") % BLS_R
    return add(multiply(G_GEN, a), multiply(H_GEN, r))
```

New endpoints `/chain/commitvote` and `/chain/revealvote`.

### 4.5 UI

Substantial UI work — needs a *Study* concept above individual runs,
where analyst $j$ uploads their per-cell scores from a CSV (or runs
scoring with `--commit-only`), submits the commit, then later submits
the reveal.

### 4.6 Effort + paper impact

- Contract: ~200 LOC including study phase management. 5 days.
- Worker: BLS scalar/point arithmetic, two endpoints, study-phase
  tracking. 3 days.
- UI: per-analyst commit/reveal workflow, study coordination
  dashboard. 5 days.
- **Total: ~3 weeks.**

Paper claim it unlocks:
> *"Inter-judge agreement metrics (§7.2 of the methodology) were
> computed under cryptographically-enforced independence between
> analysts; each analyst committed to their full score vector before
> any reveal."*

This is the *novel* methodological contribution — it converts
inter-rater independence from a procedural assumption into a
verifiable property. **But only if your study actually has multiple
independent human analysts.** For single-analyst experiments with
multiple LLMs (the dominant case in our work), Track 3 is
semantically vacuous.

### 4.7 Risks

- **Vacuity in single-analyst studies.** The commit-reveal scheme
  only enforces independence between parties holding *distinct*
  secret keys. One analyst running 5 LLMs is one party — Track 3
  gives nothing there.
- **Reveal-refusal griefing.** If one analyst commits but never
  reveals, the study cannot complete. Need a deadline / slash
  mechanism, or accept that incomplete studies are unpublishable.
- **Encoding choice for $a$.** Hashing the score vector to a scalar
  is robust but loses the per-cell breakdown; encoding directly
  loses information beyond 256 bits. We'd commit *per cell* (one
  Pedersen per cell, all under the same study_id) — increases on-chain
  cost from 1 to $NM$ commits per analyst.

## 5. Out-of-roadmap explorations

### 5.1 Threshold signatures (k-of-n analyst pool)

Variant of Track 5 where any $k$ of $n$ registered analysts suffice
to seal a run. Useful for institutional pools where a single
analyst's availability shouldn't block publication. Implementation:
Shamir-secret-share the BLS secret key across the pool, reconstruct
on-demand for signing. Out of scope for v0.3–v0.5 unless a specific
use case appears.

### 5.2 KZG / Plonk polynomial commitments

The same BLS pairing machinery underlies KZG (Kate-Zaverucha-Goldberg)
polynomial commitments. Useful for batch-proving membership of many
cells in a much more compact form than a Merkle tree — a single
KZG commitment can witness all $NM$ cells with an inclusion proof
of ~96 bytes (vs Merkle's $32 \log NM$).

Worth considering only if on-chain storage of `cells` becomes
costly. At $\sim$ 200 cells per run, Merkle is fine. At $10^4$+ it
becomes interesting.

### 5.3 Zero-knowledge proof of correct LLM execution

True end-to-end: prove that the score $s$ for $(P, c)$ was produced
by feeding the canonical prompt and retrieved chunks into the
declared LLM model at the declared temperature. **Not feasible at
current model scales.** zk-LLM research (zkML, EZKL, RISC-Zero
zkVM-of-PyTorch) targets sub-billion-parameter models for now; a 12 B
gemma is ~100× too large for proof generation in any reasonable time.

Watch for: improvements in lookup-argument-based zk circuits, MPC-in-the-head
proofs, or trusted-hardware attestations (SGX, SEV) as cheaper
alternatives.

## 6. Recommended ordering

If the goal is **paper impact**, in order of decreasing return:

1. **Track 4 (VRF)** — directly strengthens §5 *Experimental
   protocol*; benefits every benchmark paper that samples; ~1 week.
2. **Track 5 (Aggregate)** — strengthens institutional credibility
   for multi-stakeholder publications; ~1 week.
3. **Track 3 (Commit-reveal)** — only valuable if the study uses
   multiple independent human analysts; ~3 weeks; the most
   "novel-sounding" claim but vacuous in single-analyst settings.

If the goal is **minimum engineering disruption** before generating
experimental data, in order:

1. **Skip BLS for v0.3**; ship the v0.2 audit-trail-only paper now;
   add BLS as a v2 manuscript or supplemental work.
2. **Track 5 first** (smallest contract delta, no new off-chain
   dependency beyond `py_ecc`).
3. **Track 4** (needs the most-careful spec because of
   index-derivation determinism).
4. **Track 3** (heaviest, both contract and UI).

If the goal is **maximum cryptographic novelty for a methodology
paper**, in order:

1. **Track 3** (Pedersen commit-reveal) — but only if your
   experimental design actually uses multiple human analysts.
   Otherwise it's smoke.
2. **Track 4** (VRF sampling).
3. **Track 5** (aggregate signature).

## 7. Dependencies summary

| Track | New on-chain | New Python deps | New JS deps | Anchor-wallet involvement |
|---|---|---|---|---|
| 4 (VRF) | `vrfkeys`, `samples` tables; `setvrfkey`, `vrfsample` actions | `py_ecc` | none | not required (worker signs) |
| 5 (Aggregate) | `cosigners`, `aggregates` tables; `setcosigner`, `aggsign` actions | `py_ecc` | none | not required (each cosigner uses worker bridge) |
| 3 (Commit-reveal) | `commits`, `revealed` tables; `commitvote`, `revealvote` actions | `py_ecc` | maybe `@wharfkit/contract` for direct contract reads | not required (worker signs each phase) |

All three can share one Python BLS module (`newRAG/bls.py`) and one
set of generator constants $G, H, G_2$ derived nothing-up-my-sleeve
from a versioned domain separator (`"sscore-bls-v1"`).

## 8. Cross-references

- Existing higher-level plan and adoption sequence:
  [`BLOCKCHAIN_INTEGRATION_PLAN.md`](./BLOCKCHAIN_INTEGRATION_PLAN.md)
  (§5.3 Pedersen / §5.4 VRF / §5.5 Aggregate).
- Existing primitive inventory and sraudit-side plan:
  [`Crypto-Primitives-Upgrade-Plan.md`](./Crypto-Primitives-Upgrade-Plan.md)
  (Tracks 3 / 4 / 5).
- Where each would slot into the methodology paper:
  [`PAPER_METHODOLOGY.md`](./PAPER_METHODOLOGY.md) §5 *Experimental
  protocol* (Track 4), §7.2 *Inter-judge agreement* (Track 3),
  §8 *Threats to validity* (any of the three).
- Live contract this builds on:
  [`blockchain/sscore.cpp`](../blockchain/sscore.cpp) v0.2.
- Bridge that will gain endpoints:
  [`newRAG/chain_bridge.py`](./chain_bridge.py).

---

*Decision pending — choose one of the seven orderings in §6 or
defer BLS entirely and ship the audit-trail-only paper on v0.2.*
