#!/usr/bin/env bash
# Regenerate the mint_batch_a2 (private-issuer A2) fixtures the forge tests load:
#   - basic    : self-contained parity fixtures (arbitrary eIss) for
#                test/MintVerifierA2.t.sol (cross-artifact: the proof verifies
#                on chain; a tampered eIss fails the pairing check).
#   - tie      : N=1 leaf whose eIss is pinned to the canonical issuer_reenc
#                binding (test/vectors/identity.json), so test/NotesA2Tie.t.sol
#                can drive the full Notes.mint A2 path with a REAL binding.
#   - tie_dup  : N=2 with leaf 0 pinned to the same binding and leaf 1 an
#                independent eIss -- the duplicate-binding collusion regression
#                (minting with [binding0, binding0] must fail the leaf-tie).
#
# The per-N A2 circuits + zkeys must exist (make snark-a2-setup, or setup.sh
# with MINT_BATCH_A2_PINS covering 1 2).
#
# Usage: scripts/snark/gen_mint_fixtures_a2.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROVE="$ROOT/scripts/snark/prove_mint_batch_a2.js"

# The canonical A2 binding's E_iss = (R.x, R.y, C.x, C.y), read at runtime so the
# fixture stays in lock-step with test/vectors/identity.json if it regenerates.
EISS0="$(node -e '
  const j = require("'"$ROOT"'/test/vectors/identity.json");
  const e = j.issuer_reenc.E_iss;
  console.log([e.R.x, e.R.y, e.C.x, e.C.y].join(","));
')"

gen() { echo ">>> mint_batch_a2 fixture: $*"; node "$PROVE" "$@"; }

# Self-contained parity fixtures (arbitrary eIss).
gen --name=basic --n=1
gen --name=basic --n=2

# Leaf-tie fixtures: leaf 0's eIss == the canonical issuer_reenc binding.
gen --name=tie     --n=1 --eiss="0:${EISS0}"
gen --name=tie_dup --n=2 --eiss="0:${EISS0}"

echo "all mint_batch_a2 fixtures regenerated"
