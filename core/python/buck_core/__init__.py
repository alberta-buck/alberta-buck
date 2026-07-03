"""Alberta Buck core platform (Python).

See alberta-buck-platform.org for the architecture.  Layer 1 lives here
today: the ChainSession API (buck_core.session) and the Foundry artifact
helpers (buck_core.artifacts).  Layer 2 kernels (buck-math, buck-identity)
arrive as PyO3 bindings re-exported from this package.

Dependency rule: buck_core never imports alberta_buck.
"""

from buck_core.artifacts import load_artifact, repo_root
from buck_core.session import (
    CALL_GAS, DEPLOY_GAS, Expect, Journal, Web3Session,
)

__all__ = [
    "CALL_GAS", "DEPLOY_GAS", "Expect", "Journal", "Web3Session",
    "load_artifact", "repo_root",
]
