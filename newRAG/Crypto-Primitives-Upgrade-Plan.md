# PaSSER-SR — crypto primitives upgrade plan

**Forward-looking design notes for `sraudit` v3.x.** Drafted 2026-04-27 against `sraudit.cpp` v2.1.

## Context

PaSSER-SR's `sraudit` smart contract is currently a logging contract: every screening event becomes a row with a `datahash` field (SHA-256 of the actual decision data stored in MongoDB). The chain commits to the hash; clients trust the off-chain database for the underlying data. Periodic exports compute a Merkle root and (via OpenTimestamps) anchor on Bitcoin.

This works, but **leaves three significant cryptographic gaps:**

1. **No screener authentication.** Every action does `require_auth(get_self())` — the contract account itself signs. The `name screener` parameter is just data; whoever holds the contract's active key can record any screener's decision. **The audit log can be fabricated by an admin.**
2. **No on-chain Merkle proof verification.** Inclusion proofs against the committed root happen off-chain in the Python/JS clients. Trust still rests on those clients running unmodified code.
3. **No cryptographic guarantee of independence between dual screeners.** The dual-screener workflow assumes blinding via UI, but the database itself can be queried out-of-band. Cohen's Kappa values may be inflated by anchoring bias that the audit can't detect.

The spring 1.2.2 chain (after Phase C–D upgrade — see [`/Users/boniradev/Downloads/testReact/CloakOnAntelope/Progress-Report-za-Irina-2026-04-26.md`](../CloakOnAntelope/Progress-Report-za-Irina-2026-04-26.md)) exposes new host functions that close all three gaps. Some don't even require waiting for Phase D — `k1_recover` and `sha3` are in the older `CRYPTO_PRIMITIVES` feature, which spring includes.

## Available host functions (relevant subset)

| Function | Use here |
|---|---|
| `k1_recover` | Verify secp256k1 signatures (compatible with Anchor wallet, MetaMask) |
| `sha3` / `keccak256` | Merkle proof verification on chain |
| `bls_pairing`, `bls_g1_add`, `bls_g1_mul` | BLS aggregate signatures, Pedersen commitments, VRFs |
| `bls_g1_map` | Hash-to-curve for VRF and signature schemes |

## Upgrade tracks (in priority order)

### Track 1 — Screener signature verification [E, high value]

**The single most impactful upgrade for the existing system.** Closes gap #1.

**Mechanism:**
1. Add a `screeners` table mapping `name → public_key` (each screener registers their key once)
2. Each screener signs decision payloads off-chain with their personal key (Anchor wallet UI is already wired in — `TestWharf.js`)
3. Each `logdecision` / `logllmdec` / `logres` action takes the signature as an additional parameter
4. Contract calls `k1_recover` to derive the public key from the signature and digest, checks it matches the registered key

**Code sketch:**

```cpp
struct [[eosio::table]] screener_row {
    name      account;
    checksum256 pubkey_x;
    checksum256 pubkey_y;
    bool      active;
    uint64_t primary_key() const { return account.value; }
};
using screeners_t = multi_index<"screeners"_n, screener_row>;

[[eosio::action]]
void registerkey(name screener, checksum256 pubkey_x, checksum256 pubkey_y) {
    require_auth(get_self());                  // admin registers
    screeners_t s(get_self(), get_self().value);
    s.emplace(get_self(), [&](auto& r) {
        r.account = screener; r.pubkey_x = pubkey_x; r.pubkey_y = pubkey_y; r.active = true;
    });
}

[[eosio::action]]
void logdecision(name screener, std::string projectid, std::string gsid,
                 std::string decision, std::string confidence, std::string datahash,
                 signature signed_payload) {
    // Build canonical message
    auto msg_hash = sha256(canonical_form(projectid, gsid, decision, confidence, datahash));

    // Recover public key from signature
    public_key recovered = k1_recover(signed_payload, msg_hash);

    // Look up registered key for this screener
    screeners_t s(get_self(), get_self().value);
    auto reg = s.require_find(screener.value, "screener not registered");
    check(reg->active, "screener deactivated");

    // Verify match
    check(matches(recovered, reg->pubkey_x, reg->pubkey_y),
          "signature does not match registered screener key");

    // Store decision (existing logic)
    decisions_table d(get_self(), get_self().value);
    d.emplace(get_self(), [&](auto& row) { /* ... */ });
}
```

**Wallet integration:** the React UI uses Anchor (`TestWharf.js`). Anchor produces secp256k1 signatures that `k1_recover` can verify directly — no additional crypto library on the frontend.

**Effort:** ~1 week including frontend signing flow + screener registration UI. Backwards-compatibility plan: keep the old `logdecision` action in v3.x for migration; deprecate in v4.

**Doesn't require Phase D.** `CRYPTO_PRIMITIVES` is a feature spring 1.2.2 already supports out of the box for new chains and will be one of the first features activated after Phase C completes.

---

### Track 2 — On-chain Merkle proof verification [E-M, high value]

Closes gap #2. Turns the audit-export Merkle root into a fully verifiable on-chain proof system.

**Mechanism:**

1. Add a new action `verifyleaf` that takes (gsid, decision_data, merkle_path, milestone_id) and verifies the path computes to the stored merkle root for that milestone
2. `sha3` is used internally to hash leaf and intermediate nodes

**Code sketch:**

```cpp
[[eosio::action]]
void verifyleaf(std::string projectid, std::string milestone,
                std::string leaf_data, std::vector<checksum256> merkle_path,
                std::vector<uint8_t> path_indices) {
    // Compute leaf hash
    checksum256 current = sha3(leaf_data);

    // Walk up the tree
    for (size_t i = 0; i < merkle_path.size(); i++) {
        auto sibling = merkle_path[i];
        if (path_indices[i] == 0) {
            current = sha3(concat(current, sibling));
        } else {
            current = sha3(concat(sibling, current));
        }
    }

    // Look up the committed root for this milestone
    audits_table a(get_self(), get_self().value);
    auto audit = find_by_milestone(a, projectid, milestone);
    check(audit != a.end(), "no audit for milestone");

    auto stored_root = hex_to_checksum256(audit->merkleroot);
    check(current == stored_root, "leaf is NOT in the committed Merkle tree");

    print("VERIFIED|", projectid, "/", milestone, " leaf authentic");
}
```

**Result:** anyone with a decision JSON + Merkle path can prove its inclusion in the audited export *to the chain itself*. Combined with OpenTimestamps Bitcoin anchoring, this becomes a **two-layer proof**: spring chain says "this leaf is in this Merkle tree at this milestone", Bitcoin says "this Merkle tree existed at time T". Together: cryptographic proof that the decision existed at time T in this exact form.

**Effort:** ~1 week. Doesn't require Phase D.

---

### Track 3 — Commit-reveal for blind double-screening [M, novel for paper]

Addresses gap #3 — and is a **methodological contribution worth a paper extension**, not just an engineering fix.

**Problem:** dual-screening assumes the two screeners decide independently, with their decisions only revealed to each other after both have committed. The current PaSSER-SR UI implements blinding visually, but the underlying MongoDB is queryable. A reviewer with database access could see another reviewer's decision before submitting their own. Cohen's Kappa and PABAK values appear high, but the audit can't *prove* they came from independent decisions.

**Solution:** Pedersen commitments. Each reviewer commits to their decision before any reveal happens. Once both commits are on chain, both reveal. The chain enforces the ordering.

**Mechanism (uses BLS_PRIMITIVES2):**

1. Reviewer A computes `commit_A = a·G + r_A·H` where:
   - `a` is encoded decision (1=INCLUDE, 0=EXCLUDE, etc., or a multi-bit value if confidence included)
   - `G` and `H` are independent generator points on G1
   - `r_A` is private random scalar
2. Reviewer A pushes `commitvote(projectid, gsid, commit_A)` action to chain
3. Reviewer B independently does the same → `commit_B`
4. Once both commits are recorded, *no reviewer can change their mind based on the other's decision* — commitments are binding
5. Each reviewer reveals: `revealvote(projectid, gsid, decision, randomness_r_A)` 
6. Contract verifies `commit == decision·G + r·H` using `bls_g1_mul` and `bls_g1_add`
7. Disagreements proceed to `logres` as normal

**Code sketch (reveal):**

```cpp
[[eosio::action]]
void revealvote(name screener, std::string projectid, std::string gsid,
                uint64_t decision_value, checksum256 randomness) {
    // Look up the prior commit
    commits_table c(get_self(), get_self().value);
    auto cmt = c.require_find_for(screener, projectid, gsid, "no prior commit");

    // Recompute: decision·G + r·H
    bls_g1 a_G; bls_g1_mul(g_generator, decision_value_as_scalar, a_G);
    bls_g1 r_H; bls_g1_mul(h_generator, randomness_as_scalar, r_H);
    bls_g1 expected; bls_g1_add(a_G, r_H, expected);

    // Verify it matches the committed value
    check(serialize(expected) == cmt->commitment,
          "reveal does not open the commitment");

    // Record decision (now provably committed before revealed)
    decisions_table d(get_self(), get_self().value);
    d.emplace(get_self(), [&](auto& row) { /* ... */ });
}
```

**Why this matters for the paper:**

- Current SR methodology literature treats dual-screener independence as a procedural property (bound by IRB, training, UI design). 
- This proposal makes it a **cryptographic property** — independence is verifiable, not just promised.
- Cohen's Kappa computed under cryptographically-enforced independence has a stronger interpretation than under conventional dual-screening.
- This is genuinely novel; I'm not aware of prior systematic-review platforms with this property.

**Requires Phase D** (BLS_PRIMITIVES2 active). Effort: ~3-4 weeks for full implementation including frontend changes. Could be a Section 6 (Architecture) extension or a stand-alone methodology paper.

---

### Track 4 — VRF for verifiable gold-standard sampling [M, medium-high value]

The current `gold_standard_sampling.py` runs in Python — clients have to trust it produced an unbiased sample. With a VRF (Verifiable Random Function) built on `bls_g1_mul` + `bls_pairing`, sample selection becomes a deterministic, verifiable function of a public seed.

**Mechanism:**
1. Project initializes with a `seed_commitment = sha3(project_id || corpus_hash || timestamp)`
2. Sampling becomes: `selected_indices = VRF(seed, corpus_size, sample_size)`
3. The VRF output includes a proof that anyone can verify: "given this seed, the output is uniquely determined"
4. The proof is logged on-chain via a new `logsample` action

**Result:** the gold-standard subset can no longer be cherry-picked by anyone, including the system administrators. Strengthens the central methodological pillar of SR evaluation. **Particularly important** because gold-standard subsets are typically small (~5-10% of corpus) and biased sampling could make a poor screening strategy look effective.

**Effort:** ~2 weeks. Requires Phase D.

---

### Track 5 — Aggregated milestone signatures [E-M, medium value]

For `logaudit` (project milestones / final exports), require BLS-aggregated signatures from all participating screeners + admin. One 48-byte signature regardless of how many parties.

**Effect:** a project milestone is only sealed when all named contributors have cryptographically signed off. Currently `logaudit` requires only admin auth, which is a single-point-of-failure.

**Effort:** ~1-2 weeks. Requires Phase D.

---

## Lower-priority / nice-to-have

- **Hash-chained imports** [E] — each `logimport` includes hash of the previous import in the chain, creating a tamper-evident sequence. Adds ~1 line per import action; uses `sha3`.
- **Proof of LLM job uniqueness** [E] — `sha3(model + corpus_hash + strategy + seed)` becomes a unique identifier; prevents duplicate logged jobs with different metadata.
- **Anonymous reviewer credentials** [H] — for double-blind reviews where reviewer demographics need protection. PaSSER-SR's accountability model probably doesn't want this; skip unless a specific use case emerges.
- **zk-proofs of LLM execution integrity** [R] — research-level (zk-LLMs aren't production-feasible at current model scales).

## Recommended adoption sequence

1. **Phase D-independent (do first):**
   - Track 1: screener signature verification — closes the biggest audit gap, no blockchain upgrade dependency
   - Track 2: on-chain Merkle proof verification — completes the audit story
   - Hash-chained imports (lower-priority but trivial)

2. **Phase D-dependent (after BLS_PRIMITIVES2 active):**
   - Track 5: aggregated milestone signatures — solid hardening, low effort
   - Track 4: VRF gold-standard sampling — methodological strengthening
   - Track 3: commit-reveal blind double-screening — paper-grade contribution

3. **Future / research:**
   - Anonymous credentials, zk-LLM verification — cross these bridges if specific needs arise

## Mapping to paper

If a paper extension or v2 follows from this:

| Section | Current claim | Stronger claim with primitives |
|---|---|---|
| §Architecture | "Blockchain audit trail" | "Cryptographically-authenticated audit trail with screener-signed decisions" |
| §Methodology | "Dual-screener independent decisions" | "Provably-independent decisions via cryptographic commit-reveal" |
| §Evaluation | "Cohen's Kappa from dual screening" | "Cohen's Kappa under cryptographically-enforced independence" |
| §Gold standard | "Random sampling" | "VRF-verifiable random sampling, anyone can audit" |
| §Audit export | "Merkle-rooted exports" | "Merkle-rooted exports with on-chain inclusion proofs + Bitcoin anchoring" |

The "stronger claim" column is what differentiates a proof-of-concept SR audit from a system that institutional reviewers and IRBs can rely on without trusting the operator.

---

## Cross-references

- **Crypto primitive details:** [`/Users/boniradev/Downloads/testReact/CloakOnAntelope/Crypto-Primitives-Capabilities.md`](../CloakOnAntelope/Crypto-Primitives-Capabilities.md)
- **Chain upgrade status:** [`/Users/boniradev/Downloads/testReact/CloakOnAntelope/Progress-Report-za-Irina-2026-04-26.md`](../CloakOnAntelope/Progress-Report-za-Irina-2026-04-26.md)
- **Current contract:** [`blockchain/sraudit.cpp`](blockchain/sraudit.cpp) v2.1

---

*Forward-looking design notes — execute when chain upgrade Phases C-D complete and there's bandwidth for a `sraudit` v3.x. Track 1 alone is enough to substantially strengthen any v2.x audit-trail claim.*
