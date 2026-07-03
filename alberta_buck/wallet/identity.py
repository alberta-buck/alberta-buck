"""Identity data canonicalization and the identity scalar m = H(identity_data) mod ORDER.

THE canonical JSON dialect -- one dialect for every spec surface (the
identity preimage here, the AB-RCPT/1 receipt core in envelope.py):

    sorted keys, compact separators, raw UTF-8 (ensure_ascii=False),
    values restricted to strings and integers (floats are not canonical).

Raw UTF-8 is the dialect JSON.stringify and serde_json produce natively
(and the RFC 8785 / JCS direction), so every future wallet implementation
reproduces `m = keccak(canonical) mod ORDER` without a Python-idiosyncratic
escaping pass.  Pinned across Rust/Python/JS by the unicode rows of
core/vectors/identity-kernel-vectors.json.
"""

from __future__ import annotations

import json
from typing import Mapping

from alberta_buck.wallet.bn254 import ORDER
from alberta_buck.wallet.transcript import keccak_raw


def canonical_json(obj) -> str:
    """THE canonical JSON dialect (see module docstring).  Shared by the
    identity preimage and the receipt envelope so the two cannot drift."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def canonical_identity_data(fields: Mapping) -> str:
    """Serialize an identity dict to its canonical JSON form."""
    return canonical_json(dict(fields))


def identity_scalar(canonical_or_fields) -> int:
    """m = keccak256(canonical_identity_data) mod ORDER.

    Accepts either an already-canonicalized JSON string or a dict.
    """
    if isinstance(canonical_or_fields, str):
        canonical = canonical_or_fields
    else:
        canonical = canonical_identity_data(canonical_or_fields)
    digest = keccak_raw(canonical.encode("utf-8"))
    return int.from_bytes(digest, "big") % ORDER
