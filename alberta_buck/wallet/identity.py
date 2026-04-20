"""Identity data canonicalization and the identity scalar m = H(identity_data) mod ORDER.

The canonical form is JSON with sorted keys and no whitespace, exactly as in
alberta-buck-identity-example.org so the resulting m is reproducible.
"""

from __future__ import annotations

import json
from typing import Mapping

from alberta_buck.wallet.bn254 import ORDER
from alberta_buck.wallet.transcript import keccak_raw


def canonical_identity_data(fields: Mapping) -> str:
    """Serialize an identity dict to its canonical JSON form."""
    return json.dumps(dict(fields), sort_keys=True, separators=(",", ":"))


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
