"""The sim's chain handle: buck_core's Web3Session under its original name.

The generic session -- send/call/deploy with expect=OK|REVERT declared
expectations, JSONL journaling, LocalAccount + unlocked-account senders,
the memoized balance cache, revert-reason replay -- lives in
core/python/buck_core/session.py (alberta-buck-platform.org, Layer 1).
This module keeps the sim-facing names stable: Chain, load_artifact,
repo_root, and the gas constants.

Importing alberta_buck (this module's parent package) runs the bootstrap
sys.path shim that makes buck_core importable from a repo checkout.
"""

from __future__ import annotations

from buck_core.artifacts import load_artifact, repo_root          # noqa: F401
from buck_core.session import (                                   # noqa: F401
    CALL_GAS, DEPLOY_GAS, Expect, Journal, Web3Session,
)

# Original private names, kept for any straggling imports.
_DEPLOY_GAS = DEPLOY_GAS
_CALL_GAS = CALL_GAS


class Chain(Web3Session):
    """The sim's Web3Session; see buck_core.session for the full API."""
