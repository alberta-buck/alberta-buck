#!/usr/bin/env bash
# Regenerate every mint_batch fixture that test/MintVerifier.t.sol loads.
#
# The per-N circuits + zkeys must already be built (scripts/snark/setup.sh with
# MINT_BATCH_PINS covering 1 2 4 8 16 32).  Each fixture's args are pinned to the
# assertions in test/MintVerifier.t.sol; `successive` / `after_n16` /
# `partial_after_basic` chain off the `basic` batch's -state.json, so basic is
# always generated first for its N.  Chained batches take a distinct --seed:
# with the default seed their leaves would repeat the basic batch's
# (rho, idHash) draws and hence its commitments, which Notes.mint refuses
# ("duplicate public commitment", the issuance-attribution guard).
#
# Usage: scripts/snark/gen_mint_fixtures.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROVE="$ROOT/scripts/snark/prove_mint_batch.js"
FIX() { echo "$ROOT/build/snark/mint_batch_n$1/fixtures"; }

E18=000000000000000000
gen() { echo ">>> mint_batch fixture: $*"; node "$PROVE" "$@"; }

# ---- N=1 ----
gen --name=basic      --n=1
gen --name=successive --n=1 --start-leaf=1 --initial-state="$(FIX 1)/basic-state.json" --seed=2
gen --name=zero_face  --n=1 --live-leaves=0

# ---- N=2 ----
gen --name=basic      --n=2
gen --name=successive --n=2 --start-leaf=2 --initial-state="$(FIX 2)/basic-state.json" --seed=2
gen --name=partial    --n=2 --live-leaves=15${E18:1}            # 1.5 BUCK, 1 dummy

# ---- N=4 ----
gen --name=basic                --n=4
gen --name=successive           --n=4 --start-leaf=4 --initial-state="$(FIX 4)/basic-state.json" --seed=2
gen --name=partial              --n=4 --live-leaves=1${E18},15${E18:1}          # 1 + 1.5 = 2.5
gen --name=partial_after_basic  --n=4 --start-leaf=4 \
    --initial-state="$(FIX 4)/basic-state.json" --live-leaves=15${E18:1},5${E18:1}  # 1.5 + 0.5 = 2

# ---- N=8 ----
gen --name=basic      --n=8
gen --name=successive --n=8 --start-leaf=8 --initial-state="$(FIX 8)/basic-state.json" --seed=2
gen --name=partial    --n=8 --live-leaves=1${E18},2${E18},2${E18}   # 1 + 2 + 2 = 5

# ---- N=16 ----
gen --name=basic      --n=16
gen --name=successive --n=16 --start-leaf=16 --initial-state="$(FIX 16)/basic-state.json" --seed=2
gen --name=partial    --n=16 --live-leaves=1${E18},25${E18:1},3${E18},5${E18:1},15${E18:1}  # 1+2.5+3+0.5+1.5 = 8.5

# ---- N=32 ----
gen --name=basic     --n=32
# Cross-N chain: 32 leaves appended on top of the N=16 basic batch (leaf 16..47).
gen --name=after_n16 --n=32 --start-leaf=16 --initial-state="$(FIX 16)/basic-state.json" --seed=3

echo "all mint_batch fixtures regenerated"
