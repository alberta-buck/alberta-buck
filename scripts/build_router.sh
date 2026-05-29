#!/usr/bin/env bash
# Build the real lib/universal-router and stage its compiled artifact where
# the web3 simulation can read it.  The universal-router is a separate
# Foundry sub-project (its own solc 0.8.26 / via_ir / remappings) and is NOT
# part of the root build graph, so its out/ is volatile -- we copy the one
# artifact the simulation needs into a stable location.  (The Makefile's
# $(ROUTING_ARTIFACT) target does the same thing; this is the standalone
# equivalent.)
#
# Run once before the simulation:
#   bash scripts/build_router.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UR="$REPO/lib/universal-router"
DEST="$REPO/alberta_buck/sim/artifacts"

echo "Building Universal Router (solc 0.8.26, via_ir) ..."
( cd "$UR" && FORK_URL="http://localhost" forge build --skip test --skip script )

mkdir -p "$DEST"
cp "$UR/out/UniversalRouter.sol/UniversalRouter.json" "$DEST/UniversalRouter.json"
echo "Staged: alberta_buck/sim/artifacts/UniversalRouter.json"
