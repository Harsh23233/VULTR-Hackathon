"""Hash-chained, Ed25519-signed mission receipts that anyone can verify."""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

GENESIS = "0" * 64


def _portable(obj):
    """Whole-number floats become ints, so a receipt survives a round trip through JavaScript (0.0 -> 0)."""
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    if isinstance(obj, dict):
        return {k: _portable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_portable(v) for v in obj]
    return obj


def canonical(obj) -> bytes:
    return json.dumps(_portable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


class Chain:
    """Append-only list where each record carries the hash of the one before it."""

    def __init__(self):
        self.records: list[dict] = []

    @property
    def head(self) -> str:
        return self.records[-1]["hash"] if self.records else GENESIS

    def add(self, kind: str, **fields) -> dict:
        record = {"seq": len(self.records) + 1, "kind": kind, **fields, "prev_hash": self.head}
        record["hash"] = sha256(canonical(record))
        self.records.append(record)
        return record


class Signer:
    def __init__(self, seed_b64: str):
        if not seed_b64:
            raise SystemExit("Set RECEIPT_SIGNING_KEY (python3 scripts/init_env.py writes one).")
        self.key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64))
        self.public_b64 = base64.b64encode(self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        self.key_id = sha256(self.public_b64)[:16]

    def seal(self, receipt: dict) -> dict:
        body = {k: v for k, v in receipt.items() if k != "signature"}
        digest = hashlib.sha256(canonical(body)).digest()
        receipt["signature"] = {"alg": "Ed25519", "key_id": self.key_id, "public_key": self.public_b64,
                                "signed": "sha256(canonical(receipt without signature))",
                                "value": base64.b64encode(self.key.sign(digest)).decode()}
        return receipt


def verify(receipt: dict, trusted_public_b64: str | None = None) -> tuple[bool, str]:
    """Recompute every record hash and the chain links, then check the signature."""
    try:
        prev = GENESIS
        for record in receipt["records"]:
            body = {k: v for k, v in record.items() if k != "hash"}
            if record["prev_hash"] != prev:
                return False, f"record {record['seq']} does not link to the record before it"
            if sha256(canonical(body)) != record["hash"]:
                return False, f"record {record['seq']} was changed after it was written"
            prev = record["hash"]
        if receipt["chain_head"] != prev:
            return False, "chain head does not match the last record"
        sig = receipt["signature"]
        if trusted_public_b64 and sig["public_key"] != trusted_public_b64:
            return False, "signed by a key this server does not trust"
        body = {k: v for k, v in receipt.items() if k != "signature"}
        Ed25519PublicKey.from_public_bytes(base64.b64decode(sig["public_key"])).verify(
            base64.b64decode(sig["value"]), hashlib.sha256(canonical(body)).digest())
    except InvalidSignature:
        return False, "signature does not match the receipt's contents"
    except (KeyError, TypeError, ValueError) as e:
        return False, f"malformed receipt ({type(e).__name__}: {e})"
    return True, f"{len(receipt['records'])} records intact; signature valid (key {receipt['signature']['key_id']})"
