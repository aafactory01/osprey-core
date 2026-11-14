"""HMAC-SHA256 audit receipts with a hash chain.

Every sample stage transition / gate decision in config/audit_receipts.json (an illustrative release
scenario, labelled as such in the API and UI) becomes a receipt body
that embeds `prev_signature` (the previous receipt's signature, or the configured genesis marker) and
is signed over its canonical JSON with the key from AUDIT_HMAC_KEY. Tampering with any field breaks
that receipt's signature; removing or reordering receipts breaks the chain links after it. Without a
key the module raises AuditKeyError: it never invents signatures.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping

from app.errors import AuditKeyError

KEY_ID_DOMAIN = b"osprey-core/audit-key-id/v1:"
PASS = "PASS"
FAIL = "FAIL"


def canonical_json(body: Any) -> bytes:
    """Deterministic serialization: sorted keys, compact separators, UTF-8, finite numbers only."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def load_key(cfg: Mapping[str, Any], environ: Mapping[str, str]) -> bytes:
    """Read the HMAC key from the configured env var; raise AuditKeyError when absent or too short."""
    raw = environ.get(cfg["key_env_var"], "")
    if not raw:
        raise AuditKeyError(f"audit key not configured: set {cfg['key_env_var']}")
    key = raw.encode("utf-8")
    if len(key) < cfg["min_key_bytes"]:
        raise AuditKeyError(f"audit key not configured: {cfg['key_env_var']} must be at least {cfg['min_key_bytes']} bytes")
    return key


def key_fingerprint(key: bytes, chars: int) -> str:
    """Public key id: a domain-separated SHA-256 prefix. The key itself is never returned."""
    return "k-" + hashlib.sha256(KEY_ID_DOMAIN + key).hexdigest()[:chars]


def sign(key: bytes, body: Mapping[str, Any]) -> str:
    return hmac.new(key, canonical_json(body), hashlib.sha256).hexdigest()


def _digest_equal(left: Any, right: Any) -> bool:
    """Constant-time comparison that tolerates non-string input (always False)."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def build_chain(cfg: Mapping[str, Any], key: bytes) -> list[dict[str, Any]]:
    """Sign the configured events in order, linking each body to the previous signature."""
    key_id = key_fingerprint(key, cfg["key_id_chars"])
    prev = cfg["genesis_prev_signature"]
    chain: list[dict[str, Any]] = []
    for seq, event in enumerate(cfg["events"], start=1):
        body = {**event, "chain_id": cfg["chain_id"], "scenario": cfg["scenario"], "seq": seq, "prev_signature": prev,
                "key_id": key_id, "algorithm": cfg["algorithm"]}
        signature = sign(key, body)
        chain.append({"receipt": body, "signature": signature})
        prev = signature
    return chain


def describe_chain(cfg: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    """API view of the chain: receipts, short signatures, key id (never the key)."""
    chain = build_chain(cfg, key)
    short = cfg["short_signature_chars"]
    items = [{**item, "short_signature": item["signature"][:short],
              "short_prev_signature": str(item["receipt"]["prev_signature"])[:short]} for item in chain]
    return {
        "chain_id": cfg["chain_id"],
        "scenario": cfg["scenario"],
        "source_label": cfg["source_label"],
        "algorithm": cfg["algorithm"],
        "canonicalization": cfg["canonicalization"],
        "key_id": key_fingerprint(key, cfg["key_id_chars"]),
        "key_env_var": cfg["key_env_var"],
        "genesis_prev_signature": cfg["genesis_prev_signature"],
        "count": len(items),
        "chain_valid": verify_chain(cfg, key, chain)["valid"],
        "receipts": items,
    }


def _signature_ok(key: bytes, receipt: Any, signature: Any) -> tuple[bool, str]:
    if not isinstance(receipt, Mapping):
        return False, "receipt must be a JSON object"
    try:
        expected = sign(key, receipt)
    except (TypeError, ValueError, RecursionError) as exc:
        return False, f"receipt cannot be canonicalized: {type(exc).__name__}"
    if not _digest_equal(expected, signature):
        return False, "signature mismatch: payload or signature was altered"
    return True, "signature matches canonical payload"


def verify_receipt(cfg: Mapping[str, Any], key: bytes, receipt: Any, signature: Any) -> dict[str, Any]:
    """Check one receipt: HMAC over its canonical body plus its link to the authoritative chain."""
    sig_ok, sig_detail = _signature_ok(key, receipt, signature)
    chain = build_chain(cfg, key)
    seq = receipt.get("seq") if isinstance(receipt, Mapping) else None
    link_ok, link_detail = False, "receipt seq is not part of this chain"
    if isinstance(seq, int) and not isinstance(seq, bool) and 1 <= seq <= len(chain):
        expected_prev = chain[seq - 1]["receipt"]["prev_signature"]
        link_ok = _digest_equal(receipt.get("prev_signature"), expected_prev)
        link_detail = "prev_signature links to the previous receipt" if link_ok else "prev_signature does not match the chain"
    valid = sig_ok and link_ok
    return {
        "verdict": PASS if valid else FAIL,
        "valid": valid,
        "checks": {"signature": sig_ok, "chain_link": link_ok},
        "reason": sig_detail if not sig_ok else link_detail,
        "receipt_id": receipt.get("id") if isinstance(receipt, Mapping) else None,
        "key_id": key_fingerprint(key, cfg["key_id_chars"]),
    }


def verify_chain(cfg: Mapping[str, Any], key: bytes, items: Any) -> dict[str, Any]:
    """Check a full chain: every signature, every prev link, and a gap-free sequence."""
    if not isinstance(items, list) or not items:
        return {"verdict": FAIL, "valid": False, "broken_at": 0, "reason": "chain must be a non-empty list"}
    prev = cfg["genesis_prev_signature"]
    for index, item in enumerate(items):
        receipt = item.get("receipt") if isinstance(item, Mapping) else None
        signature = item.get("signature") if isinstance(item, Mapping) else None
        sig_ok, detail = _signature_ok(key, receipt, signature)
        if not sig_ok:
            return {"verdict": FAIL, "valid": False, "broken_at": index, "reason": detail}
        if not _digest_equal(receipt.get("prev_signature"), prev):
            return {"verdict": FAIL, "valid": False, "broken_at": index, "reason": "chain link broken: prev_signature mismatch"}
        if receipt.get("seq") != index + 1:
            return {"verdict": FAIL, "valid": False, "broken_at": index, "reason": "sequence gap or reorder"}
        prev = signature
    return {"verdict": PASS, "valid": True, "broken_at": None, "reason": f"{len(items)} receipts verified"}
