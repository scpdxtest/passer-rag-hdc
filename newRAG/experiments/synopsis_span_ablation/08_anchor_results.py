"""
Step 8: anchor per-row experimental results on the Antelope chain via the
existing sraudit `logaudit` action, reusing the Merkle-tree scheme that
LLMScreening uses for screening-decision audit batches.

What gets anchored
------------------
Every (judge, q_id, config) metric row becomes one Merkle-tree leaf. With 3
judges × 25 questions × 2 configs we get 150 leaves; the SHA-256 Merkle root
plus a SHA-256 of the canonical manifest JSON are submitted in a single
`logaudit` transaction to the sraudit contract.

A sidecar `audit_manifest.json` is always written; the chain submit is opt-in
via `--submit`. Without `--submit` the script is a pure local dry-run: it
computes leaves, root, and manifest, and prints what *would* be submitted.

Audit verification path
-----------------------
Given the CSVs and the manifest, `09_verify_results.py` recomputes the
leaves, the root, and queries the on-chain `auditlog` table on the sraudit
contract to confirm the submitted root matches.

Usage (dry-run by default — safe to run repeatedly)::

    python3 08_anchor_results.py \\
        --metrics "Mistral 7B"=metrics_mistral.csv \\
        --metrics "Llama 3.1 70B"=metrics_llama70b.csv \\
        --metrics "Gemma 4 31B"=metrics_gemma.csv \\
        --project-id passer-rag \\
        --milestone synopsis_span_ablation_v1 \\
        --experiment "PaSSER-RAG/BdKCSE-ext/V.B" \\
        --out audit_manifest.json

To actually submit to chain::

    python3 08_anchor_results.py ... \\
        --submit --admin <antelope-account> --contract sraudit
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Reuse the production Merkle / canonicalisation helpers — keeps the
# leaf format identical to the LLMScreening pattern and ensures the
# on-chain verification algorithm matches. Uses chain_bridge.push_action
# (HTTP-fallback signer) so submissions still work when pyntelope is
# broken in the local venv (e.g. Pydantic v1/v2 incompatibility).
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "newRAG"))
try:
    from chain_bridge import (  # type: ignore
        merkle_root_sha256, canonical_bytes, sha256_hex,
        SIGNING_KEY, SRAUDIT_SIGNING_KEY, SRAUDIT_CONTRACT,
        BC_URL, push_action,
    )
except Exception:  # pragma: no cover — keep dry-run path usable
    sys.path.insert(0, str(HERE.parent.parent))
    from chain_bridge import (  # type: ignore
        merkle_root_sha256, canonical_bytes, sha256_hex,
        SIGNING_KEY, SRAUDIT_SIGNING_KEY, SRAUDIT_CONTRACT,
        BC_URL, push_action,
    )


METRIC_COLUMNS = [
    "q_id", "config", "subset", "target_chapter",
    "recall_at_10", "synopsis_recall_at_10", "hedged", "faithfulness",
]


def parse_judge_arg(s):
    if "=" not in s:
        raise argparse.ArgumentTypeError(f"expected LABEL=PATH, got {s!r}")
    label, path = s.split("=", 1)
    return (label.strip(), path.strip())


def git_short_sha():
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=HERE, stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return None


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_leaf(judge_label, row, experiment_tag):
    # Deterministic, judge- and column-namespaced leaf payload. The
    # field order doesn't matter — canonical_bytes sorts keys — but
    # picking explicit string types avoids float-precision surprises
    # when CSV "0.40" round-trips through json.
    obj = {
        "experiment":          experiment_tag,
        "judge":               judge_label,
        "q_id":                str(row.get("q_id", "")),
        "config":              str(row.get("config", "")),
        "subset":              str(row.get("subset", "")),
        "target_chapter":      str(row.get("target_chapter", "")),
        "recall_at_10":        str(row.get("recall_at_10", "")),
        "synopsis_recall_at_10": str(row.get("synopsis_recall_at_10", "")),
        "hedged":              str(row.get("hedged", "")),
        "faithfulness":        str(row.get("faithfulness", "")),
    }
    blob = canonical_bytes(obj)
    return sha256_hex(blob), obj


def build_manifest(judge_args, project_id, milestone, experiment_tag):
    judges = []
    leaves = []
    leaf_index = []   # parallel array describing what each leaf is

    for label, path in judge_args:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        # Deterministic per-judge ordering: ascending q_id (numeric),
        # then H before N. Anyone re-running verification will sort the
        # same way, regardless of how the CSV was written.
        rows.sort(key=lambda r: (int(r["q_id"]), 0 if r["config"] == "H" else 1))
        judges.append({
            "label":       label,
            "csv_path":    os.path.relpath(path, HERE),
            "csv_sha256":  file_sha256(path),
            "row_count":   len(rows),
        })
        for r in rows:
            leaf_hex, leaf_obj = canonical_leaf(label, r, experiment_tag)
            leaves.append(leaf_hex)
            leaf_index.append({
                "judge":  label,
                "q_id":   leaf_obj["q_id"],
                "config": leaf_obj["config"],
                "leaf":   leaf_hex,
            })

    root = merkle_root_sha256(leaves)

    manifest_core = {
        "project_id":   project_id,
        "milestone":    milestone,
        "experiment":   experiment_tag,
        "judges":       judges,
        "leaf_count":   len(leaves),
        "merkle_root":  root,
        "git_sha":      git_short_sha(),
    }
    # File hash is taken over the canonicalised manifest core — same
    # contents that anyone re-running verification will rebuild.
    file_hash = sha256_hex(canonical_bytes(manifest_core))

    manifest = dict(manifest_core)
    manifest["file_hash"] = file_hash
    manifest["leaf_index"] = leaf_index
    return manifest, leaves, root, file_hash


def submit_to_chain(admin, contract, manifest):
    """Submit logaudit(admin, projectid, milestone, merkleroot, filehash,
    leafcount) via chain_bridge.push_action.

    Two important details:
    1. The sraudit contract's logaudit action checks
       `require_auth(get_self())` — i.e. only the sraudit account
       itself can sign. So the actor is the contract account
       (default `sraudit`), and we sign with SRAUDIT_SIGNING_KEY,
       NOT the sscore SIGNING_KEY.
    2. The `admin` parameter is logged as a data field (the *logical*
       author of the export) but is not chain-authenticated. We keep
       passing `sscore` so the audit row attributes the submission
       to the producer-scoring system, mirroring the LLMScreening
       pattern.
    """
    if not SRAUDIT_SIGNING_KEY:
        return {"ok": False,
                "error": "SRAUDIT_SIGNING_KEY not configured in chain_bridge.py"}

    payload = [
        {"name": "admin",      "value": admin,                         "type": "name"},
        {"name": "projectid",  "value": manifest["project_id"][:32],   "type": "string"},
        {"name": "milestone",  "value": manifest["milestone"][:32],    "type": "string"},
        {"name": "merkleroot", "value": manifest["merkle_root"][:64],  "type": "string"},
        {"name": "filehash",   "value": manifest["file_hash"][:64],    "type": "string"},
        {"name": "leafcount",  "value": int(manifest["leaf_count"]),   "type": "uint32"},
    ]
    # actor = the contract account itself (satisfies require_auth(get_self())).
    # signing_key = sraudit@active's WIF (not sscore's).
    result = push_action("logaudit", payload,
                         actor=contract, account=contract,
                         signing_key=SRAUDIT_SIGNING_KEY)

    if result.get("trx_id"):
        return {"ok": True, "tx_id": result["trx_id"], "raw": result.get("raw"),
                "bc_url": BC_URL, "contract": contract, "admin": admin}
    return {"ok": False,
            "error": result.get("error") or "unknown push failure",
            "bc_url": BC_URL, "contract": contract, "admin": admin}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metrics", action="append", type=parse_judge_arg,
                    required=True, dest="metrics",
                    help="LABEL=PATH for one judge's metrics CSV; repeat for each judge")
    ap.add_argument("--project-id", required=True,
                    help="On-chain projectid (max 32 chars)")
    ap.add_argument("--milestone", required=True,
                    help="On-chain milestone label (max 32 chars)")
    ap.add_argument("--experiment", default="PaSSER-RAG/BdKCSE-ext/V.B",
                    help="Free-text experiment tag baked into each leaf")
    ap.add_argument("--out", default="audit_manifest.json",
                    help="Where to write the local manifest")
    ap.add_argument("--submit", action="store_true",
                    help="Actually push logaudit to the chain (default: dry-run)")
    ap.add_argument("--admin", default=None,
                    help="Antelope account submitting (required with --submit)")
    ap.add_argument("--contract", default="sraudit",
                    help="Antelope contract account hosting logaudit (default: sraudit)")
    args = ap.parse_args()

    if len(args.project_id) > 32:
        sys.exit(f"--project-id too long ({len(args.project_id)} > 32)")
    if len(args.milestone) > 32:
        sys.exit(f"--milestone too long ({len(args.milestone)} > 32)")

    print(f"reading {len(args.metrics)} judge CSV(s)…", file=sys.stderr)
    manifest, leaves, root, file_hash = build_manifest(
        args.metrics, args.project_id, args.milestone, args.experiment)

    print(f"  leaves      : {len(leaves)}", file=sys.stderr)
    print(f"  merkle_root : {root}", file=sys.stderr)
    print(f"  file_hash   : {file_hash}", file=sys.stderr)
    print(f"  git_sha     : {manifest.get('git_sha')}", file=sys.stderr)

    if args.submit:
        if not args.admin:
            sys.exit("--admin is required with --submit")
        print(f"submitting logaudit → {args.contract}@{BC_URL} "
              f"as {args.admin}…", file=sys.stderr)
        result = submit_to_chain(args.admin, args.contract, manifest)
        manifest["submission"] = result
        manifest["submission_time"] = dt.datetime.utcnow().isoformat() + "Z"
        if result.get("ok"):
            print(f"  ✓ tx_id      : {result['tx_id']}", file=sys.stderr)
        else:
            print(f"  ✗ failed     : {result.get('error')}", file=sys.stderr)
    else:
        manifest["submission"] = {"ok": False, "error": "dry-run (no --submit)"}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
    print(f"wrote {args.out}", file=sys.stderr)

    sub = manifest["submission"]
    if not args.submit:
        print(f"\nDRY-RUN — nothing submitted. To anchor on chain, add:\n"
              f"   --submit --admin <antelope-account>",
              file=sys.stderr)
    elif not sub.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
