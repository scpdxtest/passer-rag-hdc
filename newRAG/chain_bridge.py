"""
Chain bridge — Flask blueprint that mirrors paper-scoring events onto
the Antelope `sscore` contract and (for full-payload persistence)
MongoDB.

Endpoints (all under the blueprint root `/chain`):

    POST /chain/startrun        open a run on-chain
    POST /chain/logcell         stream one scored cell
    POST /chain/sealrun         seal the run with a Merkle root
    POST /chain/logimport       log a corpus-ingestion event
    POST /chain/verifycell      build a Merkle path locally + push verify action
    GET  /chain/status          report whether chain + Mongo are wired

The blueprint deliberately tolerates a missing `sscore` deployment and
a missing MongoDB connection: in either failure mode it returns
sensible defaults (`trx_id: null`, `mongo_oid: null`) so the UI and
CSV export keep working. Both are reported via `/chain/status`.

Configuration:

    Chain URL    — pulled from configuration.json passer.BCEndPoints[0].url,
                   overridable via env SSCORE_BC_URL.
    Contract     — hardcoded to 'sscore'.
    Signing key  — loaded from env SSCORE_SIGNING_KEY (see .env.example);
                   never hardcoded. SRAUDIT_SIGNING_KEY and ANALYST_KEYS_JSON
                   are read from the environment the same way.
    Mongo URL/DB — env SSCORE_MONGO_URL / SSCORE_MONGO_DB, falls back to
                   the same instance used by backEnd.py.
"""

from __future__ import annotations
import hashlib
import json
import os
import re
import threading
import time

from flask import Blueprint, jsonify, request

bp = Blueprint("chain", __name__)


# Blueprint-level error handlers — convert remote-dependency exceptions
# (Mongo, sockets) into 503 JSON responses instead of Flask 500s. Same
# rationale as in newRAG/scoring_jobs.py.

try:
    from pymongo.errors import PyMongoError as _PyMongoError
except Exception:                                   # pragma: no cover
    _PyMongoError = Exception


@bp.errorhandler(_PyMongoError)
def _handle_pymongo_error_chain(e):
    msg = str(e)
    if len(msg) > 400:
        msg = msg[:400] + "…"
    return jsonify({
        "error":  "mongo unavailable",
        "detail": msg,
        "kind":   "PyMongoError",
    }), 503


@bp.errorhandler(OSError)
def _handle_os_error_chain(e):
    msg = str(e)
    if len(msg) > 400:
        msg = msg[:400] + "…"
    return jsonify({
        "error":  "network unavailable",
        "detail": msg,
        "kind":   type(e).__name__,
    }), 503

# ---------- Optional deps (worker keeps running if any are absent) ----------

# pyntelope: high-level path. Often broken on modern pydantic (constr regex/
# pattern rename), so we treat it as optional and prefer the HTTP path below.
try:
    import pyntelope
    _HAVE_PYNTELOPE = True
    _PYNTELOPE_ERR = ""
except Exception as e:
    pyntelope = None
    _HAVE_PYNTELOPE = False
    _PYNTELOPE_ERR = str(e)

# pyntelope.utils — pure-Python secp256k1 signer (sign_bytes -> SIG_K1_...).
# We load this *file* directly with importlib so the broken
# pyntelope/__init__.py is never executed. Works as long as the pyntelope
# package is on disk (the user already has it installed).
_SIGN_BYTES = None
_SIGN_BYTES_ERR = ""
def _load_signer():
    global _SIGN_BYTES, _SIGN_BYTES_ERR
    if _SIGN_BYTES is not None or _SIGN_BYTES_ERR:
        return _SIGN_BYTES
    try:
        import importlib.util, sysconfig, glob
        # Find pyntelope/utils.py in any installed site-packages dir
        candidates = []
        for path in sys.path:
            if not path:
                continue
            candidates.extend(glob.glob(os.path.join(path, "pyntelope", "utils.py")))
        candidates = list(dict.fromkeys(candidates))
        for c in candidates:
            try:
                spec = importlib.util.spec_from_file_location("_pyntelope_utils", c)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "sign_bytes"):
                    _SIGN_BYTES = mod.sign_bytes
                    return _SIGN_BYTES
            except Exception as e:
                _SIGN_BYTES_ERR = f"{c}: {e}"
        if not candidates:
            _SIGN_BYTES_ERR = "pyntelope/utils.py not found on sys.path"
    except Exception as e:
        _SIGN_BYTES_ERR = str(e)
    return None

import sys as _sys  # for _load_signer's sys.path access
sys = _sys

try:
    from pymongo import MongoClient
    _HAVE_PYMONGO = True
    _PYMONGO_ERR = ""
except Exception as e:
    MongoClient = None
    _HAVE_PYMONGO = False
    _PYMONGO_ERR = str(e)

try:
    import requests
    _HAVE_REQUESTS = True
except Exception:
    requests = None
    _HAVE_REQUESTS = False

try:
    from Crypto.Hash import RIPEMD160 as _RIPEMD160
    _HAVE_RIPEMD = True
except Exception:
    _RIPEMD160 = None
    _HAVE_RIPEMD = False


# ---------- Settings ----------

DEFAULT_MONGO_URL = "mongodb://195.230.127.227:60017/"   # matches backEnd.py
DEFAULT_MONGO_DB  = "myDB"
CONTRACT          = "sscore"
# sscore@active private key (WIF). Verified pubkey:
# EOS8j4Egh7co1dagMDLEVEf1GVJB1Xi7GGYJEbiS6rGfMeMZMsj7m
# SECURITY: never hardcode the WIF here. It is loaded from the environment
# variable SSCORE_SIGNING_KEY (see .env.example). Chain-signing routes
# return a clear error if it is unset.
SIGNING_KEY       = os.environ.get("SSCORE_SIGNING_KEY", "")

# sraudit@active private key. The sraudit contract's logaudit (and every
# other sraudit action) checks `require_auth(get_self())`, so any caller
# pushing a transaction to the sraudit account must sign with THIS key,
# not the sscore key above. Used by the experiment-anchoring path
# (newRAG/experiments/*/08_anchor_results.py → logaudit).
# SECURITY: loaded from env var SRAUDIT_SIGNING_KEY (see .env.example).
SRAUDIT_CONTRACT     = "sraudit"
SRAUDIT_SIGNING_KEY  = os.environ.get("SRAUDIT_SIGNING_KEY", "")

# v0.2 Track 1 — analyst signing keys for *batch* (unattended) runs.
# Map: Antelope-safe analyst name → WIF private key. The contract verifies
# each startrun/sealrun/logimport is signed by the analyst's registered
# pubkey via k1_recover. Register the matching pubkey on chain with
# `cleos push action sscore setanalyst ...` or POST /chain/setanalyst.
#
# Anchor-wallet signing (interactive, for non-batch runs) is NOT yet
# implemented in v0.2 — when added, the React side will sign client-side
# and pass `analyst_sig` in the request body; this dict is a fallback
# used only when the request omits the signature.
# SECURITY: analyst WIFs are loaded from the environment, never hardcoded.
# Provide them as a JSON object in ANALYST_KEYS_JSON, e.g.
#   ANALYST_KEYS_JSON='{"boni":"5K...","boniradev":"5K..."}'
# The matching pubkey for each analyst must be registered on chain via
# `setanalyst` before signed batch runs will verify.
try:
    ANALYST_KEYS = json.loads(os.environ.get("ANALYST_KEYS_JSON", "{}")) or {}
except (ValueError, TypeError):
    ANALYST_KEYS = {}

CFG_PATH          = os.path.join(os.path.dirname(__file__), "..",
                                 "src", "component", "configuration.json")


def _load_bc_url():
    if os.environ.get("SSCORE_BC_URL"):
        return os.environ["SSCORE_BC_URL"]
    try:
        with open(CFG_PATH) as f:
            cfg = json.load(f)
        eps = cfg.get("passer", {}).get("BCEndPoints", []) or []
        if eps:
            return eps[0]["url"]
    except Exception:
        pass
    return "http://127.0.0.1:8888"


BC_URL = _load_bc_url()


# ---------- Mongo client (lazy) ----------

_MONGO = {"client": None, "db": None, "err": None}
_MONGO_LOCK = threading.Lock()


def mongo_db():
    if not _HAVE_PYMONGO:
        return None
    if _MONGO["db"] is not None:
        return _MONGO["db"]
    with _MONGO_LOCK:
        if _MONGO["db"] is None:
            try:
                client = MongoClient(
                    os.environ.get("SSCORE_MONGO_URL", DEFAULT_MONGO_URL),
                    serverSelectionTimeoutMS=2000,
                )
                client.admin.command("ping")
                _MONGO["client"] = client
                _MONGO["db"] = client[os.environ.get("SSCORE_MONGO_DB", DEFAULT_MONGO_DB)]
                _MONGO["err"] = None
            except Exception as e:
                _MONGO["err"] = str(e)
        return _MONGO["db"]


# ---------- Helpers ----------

NAME_RE = re.compile(r"[^a-z1-5.]")

# Antelope names allow [.a-z1-5] only — digits 0/6/7/8/9 are NOT valid.
# To preserve the information carried by common LLM-tag digits (e.g.
# qwen3:8b vs qwen3:7b) we substitute the disallowed digits with
# letters BEFORE the regex strip. Mapping is deterministic and
# documented in blockchain/README.md so chain names stay decodable.
DIGIT_FOLD = str.maketrans({"0": "o", "6": "g", "7": "s", "8": "t", "9": "n"})


def name_safe(s: str, max_len: int = 12) -> str:
    """Coerce an arbitrary string into an Antelope name.

    Steps:
      1. lowercase
      2. fold disallowed digits 0,6,7,8,9 → o,g,s,t,n
      3. replace anything still outside [.a-z1-5] with '.'
      4. collapse runs of '.', strip leading/trailing '.'
      5. truncate to `max_len`, strip trailing '.'

    The raw (un-munged) name is always kept alongside in MongoDB
    (`{llm_model_raw, llm_model_chain}` and likewise for embed), so
    a reverse lookup is always possible."""
    if not s:
        return "noname"
    out = s.lower().translate(DIGIT_FOLD)
    out = NAME_RE.sub(".", out)
    out = re.sub(r"\.+", ".", out).strip(".")
    if not out:
        return "noname"
    if len(out) > max_len:
        out = out[:max_len]
        out = out.rstrip(".") or "n"
    return out


def canonical_bytes(obj) -> bytes:
    """Deterministic JSON: keys sorted, no whitespace, UTF-8 preserved
    (not \\uXXXX-escaped). A third party can recompute the hash by
    matching this serialisation."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def derive_run_id(analyst: str, started_at: str, collection: str,
                  criteria_hash: str) -> int:
    """Deterministic 63-bit non-negative int from the run's identity.

    Truncating to 63 bits (rather than the full 64) keeps the value
    within MongoDB BSON's *signed* int64 range (max 2**63 - 1), while
    the Antelope contract still accepts it as `uint64_t`. Collision
    space is 2**63 ≈ 9.2×10**18 — still astronomically unlikely.
    """
    blob = "|".join([analyst, started_at, collection, criteria_hash]).encode()
    return int.from_bytes(hashlib.sha256(blob).digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF


def merkle_root_sha256(leaf_hex: list[str]) -> str:
    """Binary Merkle tree over SHA-256 leaves. Duplicates the last leaf
    when a level has odd size. Matches the on-chain verifycell algo."""
    if not leaf_hex:
        return "00" * 32
    level = [bytes.fromhex(h) for h in leaf_hex]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left
            nxt.append(hashlib.sha256(left + right).digest())
        level = nxt
    return level[0].hex()


def merkle_path_sha256(leaf_hex: list[str], target_index: int):
    """Return (path, path_bits) for the leaf at target_index. path[i] is
    the sibling hash at level i; path_bits[i] = 0 means current was on
    the left at level i, 1 means on the right."""
    if not leaf_hex:
        return [], []
    path = []
    bits = []
    level = [bytes.fromhex(h) for h in leaf_hex]
    idx = target_index
    while len(level) > 1:
        if idx % 2 == 0:
            sibling = level[idx + 1] if idx + 1 < len(level) else level[idx]
            bits.append(0)
        else:
            sibling = level[idx - 1]
            bits.append(1)
        path.append(sibling.hex())
        nxt = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left
            nxt.append(hashlib.sha256(left + right).digest())
        level = nxt
        idx //= 2
    return path, bits


# ---------- Antelope binary serialisation (for HTTP push path) ----------

ANTELOPE_NAME_CHARSET = ".12345abcdefghijklmnopqrstuvwxyz"


def name_to_uint64(s: str) -> int:
    """Encode an Antelope name (≤13 chars from [.a-z1-5]) to uint64.

    The 13th character (if present) only fills the bottom 4 bits, so it
    must come from [.1-5a-j] (values 0..15)."""
    if not s:
        return 0
    if len(s) > 13:
        raise ValueError(f"name '{s}' too long (max 13)")
    result = 0
    for i, c in enumerate(s):
        try:
            v = ANTELOPE_NAME_CHARSET.index(c)
        except ValueError:
            raise ValueError(f"invalid char '{c}' in name '{s}'")
        if i == 12:
            if v > 0x0F:
                raise ValueError(f"13th char of '{s}' must be in [.1-5a-j]")
            result |= v & 0x0F
        else:
            result |= v << (64 - 5 * (i + 1))
    return result & 0xFFFFFFFFFFFFFFFF


def _varuint32(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _serialize_action(account: str, action_name: str,
                      auths: list, data_hex: str) -> bytes:
    out = bytearray()
    out += name_to_uint64(account).to_bytes(8, "little")
    out += name_to_uint64(action_name).to_bytes(8, "little")
    out += _varuint32(len(auths))
    for actor, perm in auths:
        out += name_to_uint64(actor).to_bytes(8, "little")
        out += name_to_uint64(perm).to_bytes(8, "little")
    data_bytes = bytes.fromhex(data_hex)
    out += _varuint32(len(data_bytes))
    out += data_bytes
    return bytes(out)


def _serialize_tx_header(expiration_iso: str, ref_block_num: int,
                         ref_block_prefix: int) -> bytes:
    import calendar
    from datetime import datetime
    exp_dt = datetime.strptime(expiration_iso, "%Y-%m-%dT%H:%M:%S")
    exp_unix = calendar.timegm(exp_dt.utctimetuple())
    out = bytearray()
    out += exp_unix.to_bytes(4, "little")
    out += (ref_block_num & 0xFFFF).to_bytes(2, "little")
    out += (ref_block_prefix & 0xFFFFFFFF).to_bytes(4, "little")
    out += _varuint32(0)        # max_net_usage_words
    out += bytes([0])           # max_cpu_usage_ms
    out += _varuint32(0)        # delay_sec
    return bytes(out)


# ---------- Push to chain (tolerant of missing deps / deploy) ----------

def _chain_disabled_reason() -> str | None:
    """Return a human-readable reason the chain is disabled, or None if
    we're fully wired. Order matches the fallback chain in push_action()."""
    if not SIGNING_KEY:
        return "SSCORE_SIGNING_KEY env var not set"
    # We need ONE working path: pyntelope OR (requests + pyntelope.utils signer).
    if _HAVE_PYNTELOPE:
        return None
    if not _HAVE_REQUESTS:
        return "requests not installed and pyntelope import failed"
    if _load_signer() is None:
        return (f"pyntelope import failed ({_PYNTELOPE_ERR}); "
                f"signer load failed ({_SIGN_BYTES_ERR})")
    return None


def _serialize_signature(sig_k1_str: str) -> bytes:
    """Convert SIG_K1_<base58> string into Antelope wire format:
    1 type byte (0=K1) + 65 raw signature bytes."""
    import base58
    if not sig_k1_str or not sig_k1_str.startswith("SIG_K1_"):
        raise ValueError("expected SIG_K1_ prefix")
    decoded = base58.b58decode(sig_k1_str[len("SIG_K1_"):])
    if len(decoded) != 69:  # 65 sig + 4 ripemd160 checksum
        raise ValueError(f"signature decode length {len(decoded)} != 69")
    return b"\x00" + decoded[:65]


def _serialize_public_key(pk_str: str) -> bytes:
    """Convert EOS<base58> (legacy) or PUB_K1_<base58> public key into
    Antelope wire format: 1 type byte (0=K1) + 33 compressed pubkey bytes."""
    import base58
    if not pk_str:
        raise ValueError("empty pubkey")
    if pk_str.startswith("EOS"):
        decoded = base58.b58decode(pk_str[3:])
    elif pk_str.startswith("PUB_K1_"):
        decoded = base58.b58decode(pk_str[len("PUB_K1_"):])
    else:
        raise ValueError(f"unknown pubkey format: {pk_str[:10]}")
    if len(decoded) != 37:  # 33 pubkey + 4 checksum
        raise ValueError(f"pubkey decode length {len(decoded)} != 37")
    return b"\x00" + decoded[:33]


def _serialize_action_data(payload: list) -> str:
    """Serialize an action's data field directly to hex, in the field
    order defined by the contract action signature.

    We do this client-side because the spring node's chain_api_plugin
    on this network does not expose /v1/chain/abi_json_to_bin
    (returns 404). The payload list IS the action ABI in order: each
    entry's `type` tells us how to encode `value`."""
    out = bytearray()
    for entry in payload:
        v = entry["value"]
        t = entry["type"]
        if t == "name":
            out += name_to_uint64(v).to_bytes(8, "little")
        elif t == "uint8":
            out += int(v).to_bytes(1, "little")
        elif t == "uint16":
            out += int(v).to_bytes(2, "little")
        elif t == "uint32":
            out += int(v).to_bytes(4, "little")
        elif t == "uint64":
            out += int(v).to_bytes(8, "little")
        elif t == "string":
            sb = (v or "").encode("utf-8")
            out += _varuint32(len(sb))
            out += sb
        elif t == "checksum256":
            hx = (v or "0" * 64).strip()
            if hx.startswith("0x"):
                hx = hx[2:]
            hx = hx.rjust(64, "0").lower()
            b = bytes.fromhex(hx)
            if len(b) != 32:
                raise ValueError(f"checksum256 must encode to 32 bytes, got {len(b)} (hex='{hx}')")
            out += b
        elif t == "signature":
            out += _serialize_signature(v)
        elif t == "public_key":
            out += _serialize_public_key(v)
        else:
            raise ValueError(f"unsupported ABI type in payload: {t}")
    return bytes(out).hex()


def _analyst_sign(analyst: str, params_for_sig: list) -> str:
    """Compute a SIG_K1_ signature by the analyst over the canonical
    serialization of `params_for_sig` (the action's parameters MINUS the
    trailing analyst_sig field). The contract independently re-packs the
    same params and verifies via recover_key.

    Looks up the analyst's WIF in ANALYST_KEYS. Returns None if no key is
    registered (caller produces a clear error to the requester)."""
    wif = ANALYST_KEYS.get(analyst)
    if not wif:
        return None
    signer = _load_signer()
    if not signer:
        return None
    canon_hex = _serialize_action_data(params_for_sig)
    canon_bytes = bytes.fromhex(canon_hex)
    return signer(bytes_=canon_bytes, key=wif)


def _push_via_http(action_name: str, payload: list, actor: str,
                   account: str | None = None,
                   signing_key: str | None = None) -> dict:
    """Sign and submit directly via /v1/chain HTTP API. Independent of
    pyntelope. Uses pyntelope.utils.sign_bytes loaded via importlib to
    avoid the broken pyntelope/__init__.py codepath.

    `account` overrides the on-chain contract account (default: module
    CONTRACT). `signing_key` overrides the WIF used to sign (default:
    module SIGNING_KEY). Used by external scripts that target other
    contracts on the same chain (e.g. the experiment-anchoring path →
    sraudit, which requires sraudit's own active key)."""
    account = account or CONTRACT
    sign_wif = signing_key or SIGNING_KEY
    if not _HAVE_REQUESTS:
        return {"trx_id": None, "raw": None, "error": "requests not installed"}
    signer = _load_signer()
    if signer is None:
        return {"trx_id": None, "raw": None,
                "error": f"signer unavailable: {_SIGN_BYTES_ERR}"}

    try:
        # 1. Chain info — chain_id + TaPoS reference
        info = requests.get(f"{BC_URL}/v1/chain/get_info", timeout=10).json()
        chain_id = info["chain_id"]
        ref_block_num = int(info["last_irreversible_block_num"])
        ref_block_id_hex = info["last_irreversible_block_id"]
        ref_block_prefix = int.from_bytes(bytes.fromhex(ref_block_id_hex)[8:12], "little")

        from datetime import datetime, timedelta
        expiration = (datetime.utcnow() + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")

        # 2. Encode action data ourselves (chain doesn't expose abi_json_to_bin
        #    on this network). The payload list IS the action's ABI in order.
        data_hex = _serialize_action_data(payload)

        # 3. Build canonical transaction bytes
        tx_header = _serialize_tx_header(expiration, ref_block_num, ref_block_prefix)
        action_bytes = _serialize_action(
            account, action_name,
            [(actor, "active")],
            data_hex,
        )
        tx_bytes = (tx_header
                    + _varuint32(0)                  # context_free_actions
                    + _varuint32(1) + action_bytes   # actions
                    + _varuint32(0))                 # tx extensions

        # 4. Sign. The bytes that get sha256'd are:
        #       chain_id (32) || serialized_transaction || cfd_hash (32 zeros)
        #    Note: cfd_hash is *literally 32 zero bytes* when there is no
        #    context_free_data, NOT sha256(b''). This is what pyntelope's
        #    LinkedTransaction.sign() does, and what nodeos expects.
        cfd_zero = bytes.fromhex("0" * 64)
        sign_input = bytes.fromhex(chain_id) + tx_bytes + cfd_zero
        signature = signer(bytes_=sign_input, key=sign_wif)

        # 5. Submit
        packed = {
            "compression": "none",
            "transaction": {
                "expiration": expiration,
                "ref_block_num": ref_block_num & 0xFFFF,
                "ref_block_prefix": ref_block_prefix,
                "max_net_usage_words": 0,
                "max_cpu_usage_ms": 0,
                "delay_sec": 0,
                "context_free_actions": [],
                "actions": [{
                    "account": account,
                    "name": action_name,
                    "authorization": [{"actor": actor, "permission": "active"}],
                    "data": data_hex,
                }],
                "transaction_extensions": [],
            },
            "signatures": [signature],
            "context_free_data": [],
        }
        push_resp = requests.post(f"{BC_URL}/v1/chain/push_transaction",
                                  json=packed, timeout=30)
        if not push_resp.ok:
            try:
                err = push_resp.json()
                # Extract the most useful piece if it's a nodeos error envelope
                details = err.get("error", {}).get("details") or err.get("message") or err
            except Exception:
                details = push_resp.text[:400]
            return {"trx_id": None, "raw": err if "err" in locals() else None,
                    "error": f"push_transaction {push_resp.status_code}: {details}"}

        data = push_resp.json()
        return {"trx_id": data.get("transaction_id"), "raw": data, "error": None}
    except Exception as e:
        return {"trx_id": None, "raw": None, "error": f"http push failed: {e}"}


def _push_via_pyntelope(action_name: str, payload: list, actor: str,
                        account: str | None = None,
                        signing_key: str | None = None) -> dict:
    """Original pyntelope path. Only used when pyntelope imports cleanly."""
    account = account or CONTRACT
    sign_wif = signing_key or SIGNING_KEY
    try:
        data = []
        for entry in payload:
            name = entry["name"]
            value = entry["value"]
            t = entry["type"]
            ctor = {
                "name":        pyntelope.types.Name,
                "uint8":       pyntelope.types.Uint8,
                "uint32":      pyntelope.types.Uint32,
                "uint64":      pyntelope.types.Uint64,
                "string":      pyntelope.types.String,
                "checksum256": pyntelope.types.Checksum256,
            }[t]
            data.append(pyntelope.Data(name=name, value=ctor(value)))
        auth = pyntelope.Authorization(actor=actor, permission="active")
        action = pyntelope.Action(account=account, name=action_name,
                                  data=data, authorization=[auth])
        tx = pyntelope.Transaction(actions=[action])
        net = pyntelope.Net(host=BC_URL)
        signed = tx.link(net=net).sign(key=sign_wif)
        resp = signed.send()
        return {"trx_id": resp.get("transaction_id"), "raw": resp, "error": None}
    except Exception as e:
        return {"trx_id": None, "raw": None, "error": f"pyntelope push failed: {e}"}


def push_action(action_name: str, payload: list, actor: str | None = None,
                account: str | None = None,
                signing_key: str | None = None) -> dict:
    """Push a transaction. Tries pyntelope first (if it loaded cleanly),
    falls back to the pyntelope-free HTTP+pure-Python-signer path. If
    neither is wired, returns a no-op so the caller proceeds with
    off-chain persistence only.

    `account` overrides the contract account targeted by the action
    (default: module CONTRACT). `signing_key` overrides the WIF used to
    sign (default: module SIGNING_KEY). External scripts can use these
    to push to other contracts on the same chain (e.g. the
    experiment-anchoring path → sraudit's logaudit, which must be
    signed by sraudit@active per `require_auth(get_self())` in the
    contract)."""
    why = _chain_disabled_reason()
    if why:
        return {"trx_id": None, "raw": None, "error": "chain disabled: " + why}
    actor = actor or CONTRACT

    if _HAVE_PYNTELOPE:
        result = _push_via_pyntelope(action_name, payload, actor, account, signing_key)
        # If pyntelope succeeded OR the failure looks runtime (not import-time),
        # don't silently fall through to HTTP. But on a clean pyntelope env this
        # is unreachable when result has trx_id.
        if result.get("trx_id"):
            return result
        # Fall through to HTTP on pyntelope runtime failure as well.
        result_http = _push_via_http(action_name, payload, actor, account, signing_key)
        if result_http.get("trx_id"):
            return result_http
        return result_http if result_http.get("error") else result

    return _push_via_http(action_name, payload, actor, account, signing_key)


# ---------- Endpoints ----------

@bp.route("/chain/setanalyst", methods=["POST"])
def set_analyst():
    """Admin: register or update an analyst on the contract's `analysts`
    table. Pushes with the contract's hot key (sscore@active), so callers
    of this endpoint must already be trusted by the worker host.

    Body: { "analyst": "<name-safe>", "pubkey": "EOS...", "active": true }"""
    j = request.get_json(force=True) or {}
    analyst = name_safe(j.get("analyst") or "")
    pubkey  = j.get("pubkey") or ""
    active  = bool(j.get("active", True))
    if not analyst or not pubkey:
        return jsonify({"error": "analyst and pubkey are required"}), 400
    chain = push_action("setanalyst", [
        {"name": "analyst", "value": analyst, "type": "name"},
        {"name": "pubkey",  "value": pubkey,  "type": "public_key"},
        {"name": "active",  "value": 1 if active else 0, "type": "uint8"},
    ])
    return jsonify({
        "trx_id":      chain.get("trx_id"),
        "chain_error": chain.get("error"),
        "analyst":     analyst,
        "pubkey":      pubkey,
        "active":      active,
    })


@bp.route("/chain/whoami", methods=["GET"])
def whoami():
    """Diagnostic: report which public key the worker would sign with.
    Does NOT leak the private key — only derives and reports the EOS pubkey."""
    info = {
        "wif_loaded": bool(SIGNING_KEY),
        "wif_length": len(SIGNING_KEY) if SIGNING_KEY else 0,
        "wif_first6": SIGNING_KEY[:6] if SIGNING_KEY else "",
        "wif_last4":  SIGNING_KEY[-4:] if SIGNING_KEY else "",
        "env_var_set": "SSCORE_SIGNING_KEY" in os.environ,
        "config_file": os.path.abspath(__file__),
    }
    signer = _load_signer()
    if not signer or not SIGNING_KEY:
        info["public_key"] = None
        info["pubkey_error"] = "signer or WIF unavailable"
        return jsonify(info)
    try:
        # Derive public key using the same pure-Python primitives as the signer.
        # We import _decode_privkey, _fast_multiply, G from the loaded utils module.
        import importlib.util, glob
        utils_path = None
        for p in sys.path:
            if not p: continue
            hits = glob.glob(os.path.join(p, "pyntelope", "utils.py"))
            if hits: utils_path = hits[0]; break
        spec = importlib.util.spec_from_file_location("_pu_for_pub", utils_path)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        priv_int = m._decode_privkey(SIGNING_KEY)
        x, y = m._fast_multiply(m.G, priv_int)
        prefix = b"\x02" if (y % 2) == 0 else b"\x03"
        pub33 = prefix + x.to_bytes(32, "big")
        if _HAVE_RIPEMD:
            h = _RIPEMD160.new(); h.update(pub33); checksum = h.digest()[:4]
        else:
            checksum = hashlib.new("ripemd160", pub33).digest()[:4]
        import base58
        info["public_key"] = "EOS" + base58.b58encode(pub33 + checksum).decode("ascii")
    except Exception as e:
        info["public_key"] = None
        info["pubkey_error"] = str(e)
    return jsonify(info)


@bp.route("/chain/status", methods=["GET"])
def status():
    signer_ok = _load_signer() is not None
    return jsonify({
        "contract":       CONTRACT,
        "bc_url":         BC_URL,
        "pyntelope":      _HAVE_PYNTELOPE,
        "pyntelope_err":  _PYNTELOPE_ERR or None,
        "signer_ok":      signer_ok,
        "signer_err":     _SIGN_BYTES_ERR or None,
        "requests":       _HAVE_REQUESTS,
        "ripemd160":      _HAVE_RIPEMD,
        "pymongo":        _HAVE_PYMONGO,
        "mongo_ok":       mongo_db() is not None,
        "mongo_error":    _MONGO["err"],
        "chain_enabled":  _chain_disabled_reason() is None,
        "chain_disabled_reason": _chain_disabled_reason(),
        "push_path":      ("pyntelope" if _HAVE_PYNTELOPE else
                           ("http" if (_HAVE_REQUESTS and signer_ok and SIGNING_KEY) else "none")),
    })


@bp.route("/chain/startrun", methods=["POST"])
def startrun():
    j = request.get_json(force=True) or {}
    analyst        = name_safe(j.get("analyst") or "anonymous")
    llm_raw        = j.get("llm_model", "")
    embed_raw      = j.get("embed_model", "")
    collection     = j.get("collection") or ""
    criteria       = j.get("criteria") or []
    params         = j.get("params") or {}
    n_papers       = int(j.get("n_papers") or 0)
    n_criteria     = int(j.get("n_criteria") or len(criteria))
    corpus_ref     = j.get("corpus_ref") or {}    # any manifest reference
    sample_seed    = j.get("sample_seed") or ""

    started_at_iso = j.get("started_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    corpus_hash    = sha256_hex(canonical_bytes(corpus_ref))
    criteria_hash  = sha256_hex(canonical_bytes(criteria))
    params_hash    = sha256_hex(canonical_bytes(params))
    run_id         = derive_run_id(analyst, started_at_iso, collection, criteria_hash)

    # Build the action params *without* the analyst signature, in the
    # exact order the contract re-packs them. Then sign and append.
    params_for_sig = [
        {"name": "run_id",        "value": run_id,                 "type": "uint64"},
        {"name": "analyst",       "value": analyst,                "type": "name"},
        {"name": "llm_model",     "value": name_safe(llm_raw),     "type": "name"},
        {"name": "embed_model",   "value": name_safe(embed_raw),   "type": "name"},
        {"name": "collection",    "value": collection,             "type": "string"},
        {"name": "corpus_hash",   "value": corpus_hash,            "type": "checksum256"},
        {"name": "criteria_hash", "value": criteria_hash,          "type": "checksum256"},
        {"name": "params_hash",   "value": params_hash,            "type": "checksum256"},
        {"name": "sample_seed",   "value": sample_seed,            "type": "string"},
        {"name": "n_papers",      "value": n_papers,               "type": "uint32"},
        {"name": "n_criteria",    "value": n_criteria,             "type": "uint32"},
    ]
    # Prefer a client-supplied signature (future Anchor path); else sign
    # server-side using ANALYST_KEYS (batch path).
    analyst_sig = j.get("analyst_sig") or _analyst_sign(analyst, params_for_sig)
    if not analyst_sig:
        return jsonify({
            "error": f"no signing key for analyst '{analyst}'. "
                     f"Add it to ANALYST_KEYS in chain_bridge.py and restart "
                     f"the worker, then register the matching pubkey on chain "
                     f"via setanalyst.",
        }), 400

    chain = push_action("startrun",
                        params_for_sig + [{"name": "analyst_sig",
                                           "value": analyst_sig,
                                           "type": "signature"}])

    db = mongo_db()
    if db is not None:
        try:
            db["sscore_runs"].insert_one({
                "run_id": run_id, "analyst": analyst,
                "llm_model_raw": llm_raw, "llm_model_chain": name_safe(llm_raw),
                "embed_model_raw": embed_raw, "embed_model_chain": name_safe(embed_raw),
                "collection": collection,
                "corpus_hash": corpus_hash, "criteria_hash": criteria_hash,
                "params_hash": params_hash,
                "criteria": criteria, "params": params,
                "n_papers": n_papers, "n_criteria": n_criteria,
                "started_at": started_at_iso, "sample_seed": sample_seed,
                "chain_trx_id": chain.get("trx_id"),
            })
        except Exception:
            pass

    # IMPORTANT: run_id is uint64 (truncated sha256), routinely larger than
    # Number.MAX_SAFE_INTEGER (2**53). Return as a STRING so the JS client
    # keeps the exact value; we accept it as a string on the way back in
    # (/chain/logcell, /chain/sealrun) and convert to int there.
    return jsonify({
        "run_id": str(run_id),
        "trx_id": chain.get("trx_id"),
        "chain_error": chain.get("error"),
        "corpus_hash": corpus_hash,
        "criteria_hash": criteria_hash,
        "params_hash": params_hash,
    })


@bp.route("/chain/logcell", methods=["POST"])
def logcell():
    j = request.get_json(force=True) or {}
    # Accept run_id as either a string ("8731…") or a number; always normalise
    # to int. Browser clients MUST send the string form (uint64 > 2^53) or
    # they'll silently round it. See note in /chain/startrun response.
    run_id  = int(str(j["run_id"]))
    payload = j["payload"]   # the full row from scorePapersBat.js

    # Normalise into canonical form before hashing
    canonical = {
        "v": 1,
        "run_id": run_id,
        "paper_id": str(payload.get("paper_id")),
        "criterion_id": str(payload.get("criterion_id")),
        "score": payload.get("score"),
        "scale": payload.get("scale") or "0-5",
        "source": payload.get("source"),
        "justification": payload.get("justification") or "",
        "evidence": payload.get("evidence") or "",
        "llm_model": payload.get("llm_model") or "",
        "embed_model": payload.get("embed_model") or "",
        "collection": payload.get("collection") or "",
        "run_started_at": payload.get("run_started_at") or "",
    }
    payload_hash = sha256_hex(canonical_bytes(canonical))

    mongo_oid = ""
    db = mongo_db()
    if db is not None:
        try:
            res = db["sscore_cells"].insert_one({**canonical,
                                                 "payload_hash": payload_hash})
            mongo_oid = str(res.inserted_id)
        except Exception:
            mongo_oid = ""

    try:
        scale_max = int(str(canonical["scale"]).split("-")[1])
    except Exception:
        scale_max = 5
    try:
        score_byte = int(canonical["score"]) if canonical["score"] is not None else 0
    except Exception:
        score_byte = 0
    score_byte = max(0, min(255, score_byte))

    chain = push_action("logcell", [
        {"name": "run_id",       "value": run_id,                                 "type": "uint64"},
        {"name": "paper_id",     "value": canonical["paper_id"],                  "type": "string"},
        {"name": "criterion_id", "value": name_safe(canonical["criterion_id"]),   "type": "name"},
        {"name": "score",        "value": score_byte,                             "type": "uint8"},
        {"name": "scale_max",    "value": max(0, min(255, scale_max)),            "type": "uint8"},
        {"name": "payload_hash", "value": payload_hash,                           "type": "checksum256"},
        {"name": "mongo_oid",    "value": mongo_oid,                              "type": "string"},
    ])

    return jsonify({
        "trx_id":       chain.get("trx_id"),
        "chain_error":  chain.get("error"),
        "payload_hash": payload_hash,
        "mongo_oid":    mongo_oid,
    })


@bp.route("/chain/sealrun", methods=["POST"])
def sealrun():
    j = request.get_json(force=True) or {}
    run_id = int(str(j["run_id"]))
    leaves = j.get("payload_hashes") or []
    if not leaves:
        return jsonify({"error": "payload_hashes is required and non-empty"}), 400
    rows_root = merkle_root_sha256(leaves)

    params_for_sig = [
        {"name": "run_id",    "value": run_id,    "type": "uint64"},
        {"name": "rows_root", "value": rows_root, "type": "checksum256"},
    ]
    # The seal must come from the same analyst who opened the run. Look up
    # which analyst that was (from the Mongo mirror); fall back to the
    # request body if present.
    analyst = j.get("analyst")
    if not analyst:
        db = mongo_db()
        if db is not None:
            doc = db["sscore_runs"].find_one({"run_id": run_id}) or {}
            analyst = doc.get("analyst")
    if not analyst:
        return jsonify({"error": "could not resolve analyst for this run"}), 400

    analyst_sig = j.get("analyst_sig") or _analyst_sign(analyst, params_for_sig)
    if not analyst_sig:
        return jsonify({"error": f"no signing key for analyst '{analyst}'"}), 400

    chain = push_action("sealrun",
                        params_for_sig + [{"name": "analyst_sig",
                                           "value": analyst_sig,
                                           "type": "signature"}])

    db = mongo_db()
    if db is not None:
        try:
            db["sscore_runs"].update_one(
                {"run_id": run_id},
                {"$set": {"rows_root": rows_root,
                          "sealed_trx_id": chain.get("trx_id"),
                          "leaves": leaves}},
            )
        except Exception:
            pass

    return jsonify({
        "trx_id":      chain.get("trx_id"),
        "chain_error": chain.get("error"),
        "rows_root":   rows_root,
    })


@bp.route("/chain/verifycell", methods=["POST"])
def verifycell():
    """Build a Merkle path locally from the sealed leaves and push the
    on-chain verifycell action. The chain's check is authoritative; this
    endpoint just produces & submits the proof."""
    j = request.get_json(force=True) or {}
    run_id  = int(str(j["run_id"]))
    leaf    = j["leaf_hash"]
    leaves  = j.get("leaves") or []
    if not leaves:
        # Fallback: pull from MongoDB
        db = mongo_db()
        if db is not None:
            doc = db["sscore_runs"].find_one({"run_id": run_id}) or {}
            leaves = doc.get("leaves") or []
    if leaf not in leaves:
        return jsonify({"error": "leaf_hash not present in sealed leaves"}), 400
    idx = leaves.index(leaf)
    path, bits = merkle_path_sha256(leaves, idx)

    chain = push_action("verifycell", [
        {"name": "run_id",    "value": run_id, "type": "uint64"},
        {"name": "leaf_hash", "value": leaf,   "type": "checksum256"},
        # NOTE: the action expects vector<checksum256> and vector<uint8>.
        # pyntelope vector encoding is wrapped here as JSON-passed lists.
        # If your pyntelope build doesn't expose vector helpers, push this
        # action via cleos for now; see blockchain/README.md §5.
        {"name": "path",      "value": path,   "type": "string"},
        {"name": "path_bits", "value": bits,   "type": "string"},
    ])
    return jsonify({
        "trx_id":     chain.get("trx_id"),
        "chain_error": chain.get("error"),
        "path":       path,
        "path_bits":  bits,
    })


@bp.route("/chain/logimport", methods=["POST"])
def logimport():
    j = request.get_json(force=True) or {}
    analyst     = name_safe(j.get("analyst") or "anonymous")
    collection  = j.get("collection") or ""
    embed_raw   = j.get("embed_model") or ""
    n_papers    = int(j.get("n_papers") or 0)
    manifest    = j.get("manifest_ref") or ""
    corpus_ref  = j.get("corpus_ref") or {}

    corpus_hash = sha256_hex(canonical_bytes(corpus_ref))

    prev_hash = "0" * 64
    db = mongo_db()
    if db is not None:
        try:
            prev = db["sscore_imports"].find_one(
                {"collection": collection}, sort=[("ts", -1)])
            if prev and prev.get("corpus_hash"):
                prev_hash = sha256_hex(
                    canonical_bytes({
                        "v": 1,
                        "analyst": prev.get("analyst"),
                        "collection": collection,
                        "embed_model": prev.get("embed_model_chain"),
                        "corpus_hash": prev["corpus_hash"],
                        "n_papers": prev.get("n_papers"),
                        "ts": prev.get("ts"),
                    })
                )
        except Exception:
            pass

    params_for_sig = [
        {"name": "analyst",      "value": analyst,                  "type": "name"},
        {"name": "collection",   "value": collection,               "type": "string"},
        {"name": "embed_model",  "value": name_safe(embed_raw),     "type": "name"},
        {"name": "corpus_hash",  "value": corpus_hash,              "type": "checksum256"},
        {"name": "n_papers",     "value": n_papers,                 "type": "uint32"},
        {"name": "manifest_ref", "value": str(manifest),            "type": "string"},
        {"name": "prev_hash",    "value": prev_hash,                "type": "checksum256"},
    ]
    analyst_sig = j.get("analyst_sig") or _analyst_sign(analyst, params_for_sig)
    if not analyst_sig:
        return jsonify({"error": f"no signing key for analyst '{analyst}'"}), 400

    chain = push_action("logimport",
                        params_for_sig + [{"name": "analyst_sig",
                                           "value": analyst_sig,
                                           "type": "signature"}])

    if db is not None:
        try:
            db["sscore_imports"].insert_one({
                "analyst": analyst, "collection": collection,
                "embed_model_raw": embed_raw,
                "embed_model_chain": name_safe(embed_raw),
                "corpus_hash": corpus_hash, "n_papers": n_papers,
                "manifest_ref": manifest, "prev_hash": prev_hash,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "chain_trx_id": chain.get("trx_id"),
            })
        except Exception:
            pass

    return jsonify({
        "trx_id":      chain.get("trx_id"),
        "chain_error": chain.get("error"),
        "corpus_hash": corpus_hash,
        "prev_hash":   prev_hash,
    })
