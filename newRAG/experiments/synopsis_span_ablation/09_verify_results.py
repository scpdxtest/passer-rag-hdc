"""
Step 9: verify a previously-anchored audit manifest against (a) the
local CSVs and (b) the on-chain sraudit `auditlog` table.

Three independent checks:
  1. Each judge CSV's SHA-256 matches the value recorded in the manifest.
  2. Recomputed Merkle leaves and root match the manifest's leaves and
     `merkle_root` exactly.
  3. The on-chain `auditlog` table on the sraudit contract has a row
     with the same (project_id, milestone) whose `merkleroot` matches
     our local recomputation. (Skipped with --skip-chain.)

Exit code 0 → all checks pass. Non-zero → at least one check failed,
and the script prints which one.

Usage::

    python3 09_verify_results.py \\
        --manifest audit_manifest.json \\
        --metrics "Mistral 7B"=metrics_mistral.csv \\
        --metrics "Llama 3.1 70B"=metrics_llama70b.csv \\
        --metrics "Gemma 4 31B"=metrics_gemma.csv
"""
import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "newRAG"))
try:
    from chain_bridge import (  # type: ignore
        merkle_root_sha256, canonical_bytes, sha256_hex, BC_URL,
    )
except Exception:
    sys.path.insert(0, str(HERE.parent.parent))
    from chain_bridge import (  # type: ignore
        merkle_root_sha256, canonical_bytes, sha256_hex, BC_URL,
    )


def parse_judge_arg(s):
    if "=" not in s:
        raise argparse.ArgumentTypeError(f"expected LABEL=PATH, got {s!r}")
    label, path = s.split("=", 1)
    return (label.strip(), path.strip())


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_leaf(judge_label, row, experiment_tag):
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
    return sha256_hex(canonical_bytes(obj))


def query_chain_auditlog(contract, project_id, milestone, bc_url):
    """Look up the auditlog row whose (projectid, milestone) match. We do
    a forward table scan via /v1/chain/get_table_rows; the table is
    typically small (one row per export milestone) so this is fine."""
    import requests
    rows = []
    next_lower = ""
    for _ in range(40):    # cap pages
        body = {
            "json": True, "code": contract, "scope": contract,
            "table": "audits", "lower_bound": next_lower, "limit": 100,
        }
        r = requests.post(f"{bc_url.rstrip('/')}/v1/chain/get_table_rows",
                          json=body, timeout=15)
        r.raise_for_status()
        j = r.json()
        page = j.get("rows", [])
        rows.extend(page)
        if not j.get("more"):
            break
        next_lower = j.get("next_key") or (str(page[-1].get("id", "")) if page else "")
        if not next_lower:
            break

    matches = [r for r in rows
               if r.get("projectid") == project_id
               and r.get("milestone") == milestone]
    return matches, rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--metrics", action="append", type=parse_judge_arg, required=True)
    ap.add_argument("--contract", default="sraudit")
    ap.add_argument("--bc-url", default=None,
                    help="Override BC RPC URL; defaults to chain_bridge.BC_URL")
    ap.add_argument("--skip-chain", action="store_true",
                    help="Run only the local SHA-256 + Merkle re-derivation")
    args = ap.parse_args()

    with open(args.manifest, encoding="utf-8") as f:
        manifest = json.load(f)

    print(f"manifest      : {args.manifest}", file=sys.stderr)
    print(f"  project_id  : {manifest['project_id']}", file=sys.stderr)
    print(f"  milestone   : {manifest['milestone']}", file=sys.stderr)
    print(f"  experiment  : {manifest['experiment']}", file=sys.stderr)
    print(f"  recorded root: {manifest['merkle_root']}", file=sys.stderr)
    print(f"  leaf_count  : {manifest['leaf_count']}", file=sys.stderr)
    print()

    fails = []

    # ---- 1. CSV SHA-256 match ----
    print("CHECK 1 — CSV file hashes vs manifest")
    manifest_judges = {j["label"]: j for j in manifest["judges"]}
    for label, path in args.metrics:
        if label not in manifest_judges:
            fails.append(f"judge {label!r} not in manifest")
            print(f"  ✗ {label}: not in manifest")
            continue
        local = file_sha256(path)
        recorded = manifest_judges[label]["csv_sha256"]
        ok = local == recorded
        print(f"  {'✓' if ok else '✗'} {label}: {local[:16]}…  "
              f"recorded {recorded[:16]}…")
        if not ok:
            fails.append(f"CSV hash mismatch for {label}: {path}")

    # ---- 2. Recomputed leaves + root ----
    print()
    print("CHECK 2 — recomputed Merkle leaves + root")
    leaves = []
    judge_args = {label: path for label, path in args.metrics}
    for jrec in manifest["judges"]:
        label = jrec["label"]
        path = judge_args.get(label)
        if not path:
            fails.append(f"no --metrics passed for judge {label!r}")
            print(f"  ✗ {label}: no CSV passed")
            continue
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        rows.sort(key=lambda r: (int(r["q_id"]), 0 if r["config"] == "H" else 1))
        for r in rows:
            leaves.append(canonical_leaf(label, r, manifest["experiment"]))

    recomputed_root = merkle_root_sha256(leaves)
    if recomputed_root == manifest["merkle_root"]:
        print(f"  ✓ root matches: {recomputed_root}")
    else:
        fails.append("Merkle root mismatch")
        print(f"  ✗ root mismatch")
        print(f"     local     : {recomputed_root}")
        print(f"     recorded  : {manifest['merkle_root']}")

    if len(leaves) != manifest["leaf_count"]:
        fails.append(f"leaf count mismatch: {len(leaves)} local vs "
                     f"{manifest['leaf_count']} recorded")
        print(f"  ✗ leaf_count: {len(leaves)} local vs "
              f"{manifest['leaf_count']} recorded")

    # ---- 3. On-chain match ----
    if args.skip_chain:
        print()
        print("CHECK 3 — skipped (--skip-chain)")
    else:
        print()
        print(f"CHECK 3 — on-chain auditlog at {args.contract}@"
              f"{args.bc_url or BC_URL}")
        try:
            matches, _all = query_chain_auditlog(
                args.contract, manifest["project_id"], manifest["milestone"],
                args.bc_url or BC_URL)
        except Exception as e:
            fails.append(f"chain query failed: {e}")
            print(f"  ✗ chain query failed: {e}")
            matches = []

        if not matches:
            fails.append(f"no on-chain auditlog row for "
                         f"({manifest['project_id']}, {manifest['milestone']})")
            print(f"  ✗ no auditlog row for "
                  f"({manifest['project_id']}, {manifest['milestone']})")
        else:
            chain_roots = {r.get("merkleroot") for r in matches}
            if manifest["merkle_root"] in chain_roots:
                print(f"  ✓ on-chain row found with matching merkleroot")
                for r in matches:
                    if r.get("merkleroot") == manifest["merkle_root"]:
                        print(f"     auditlog id : {r.get('id')}")
                        print(f"     admin       : {r.get('admin')}")
                        print(f"     leafcount   : {r.get('leafcount')}")
                        print(f"     filehash    : {r.get('filehash')}")
                        break
            else:
                fails.append("on-chain merkleroot does not match local")
                print(f"  ✗ on-chain merkleroot does not match local")
                for r in matches:
                    print(f"     row id {r.get('id')}: "
                          f"on-chain {r.get('merkleroot')}")
                print(f"     local             : {manifest['merkle_root']}")

    print()
    if fails:
        print(f"VERIFICATION FAILED — {len(fails)} issue(s):")
        for x in fails:
            print(f"  · {x}")
        sys.exit(1)
    print("VERIFICATION OK ✓ — all checks pass")


if __name__ == "__main__":
    main()
