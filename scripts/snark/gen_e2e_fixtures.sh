#!/bin/bash
# Generate the end-to-end Notes fixtures (test/vectors/e2e/{a1,a2,b1}.json)
# consumed by test/NotesE2E.t.sol: one mutually-consistent world per flavor,
# with REAL proofs at every gate (mint, spend, sigma, membership, and -- for
# A2 -- the note<->eEnc binding).
#
# Prerequisites (built by the existing setups):
#   build/snark/mint_batch_n1     -- scripts/snark/setup.sh (MINT_BATCH_PINS=1..)
#   build/snark/mint_batch_a2_n1  -- setup.sh (MINT_BATCH_A2_PINS=1..)
#   build/snark/spend             -- setup.sh
#   build/snark/g1tie             -- make snark-g1tie
#   build/snark/note_binding      -- make snark-note-binding
#
# Usage:
#   make nix-snark-e2e-fixtures
#   nix develop --command bash scripts/snark/gen_e2e_fixtures.sh [a1|a2|b1 ...]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
E2E="$ROOT/build/snark/e2e"
G1TIE="$ROOT/build/snark/g1tie"
NB="$ROOT/build/snark/note_binding"
RAPIDSNARK="$ROOT/lib/rapidsnark-macOS-arm64-v0.0.8/bin/prover"
CHAINID=1

FLAVORS=("${@:-a1 a2 b1}")
[ $# -gt 0 ] && FLAVORS=("$@") || FLAVORS=(a1 a2 b1)

for FLAVOR in "${FLAVORS[@]}"; do
    echo "=== e2e fixture: $FLAVOR ==="
    OUT="$E2E/$FLAVOR"

    # ---- 1. World (identities, note, sigma, prover inputs) ----
    PYTHONPATH="$ROOT" python "$SCRIPT_DIR/gen_e2e_world.py" world --flavor="$FLAVOR"

    # ---- 2. Mint proof (pinned wallet opening) ----
    MINT_ARGS=$(python3 -c "
import json; print(' '.join(json.load(open('$OUT/world.json'))['mint_args']))")
    if [ "$FLAVOR" = "a2" ]; then
        node "$SCRIPT_DIR/prove_mint_batch_a2.js" $MINT_ARGS
        MINT_FIX="build/snark/mint_batch_a2_n1/fixtures/e2e_a2.json"
    else
        node "$SCRIPT_DIR/prove_mint_batch.js" $MINT_ARGS
        MINT_FIX="build/snark/mint_batch_n1/fixtures/e2e_${FLAVOR}.json"
    fi

    # ---- 3. Spend proof (leaf 0 of the e2e mint, world payout, chainid=1) ----
    PAYOUT=$(python3 -c "
import json; print(json.load(open('$OUT/world.json'))['payout'])")
    node "$SCRIPT_DIR/prove_spend.js" "$MINT_FIX" 0 "$PAYOUT" "$CHAINID" "e2e_${FLAVOR}"
    cp "$ROOT/build/snark/spend/fixtures/e2e_${FLAVOR}.json" "$OUT/spend.json"

    # ---- 4. g1tie membership proof ----
    npx snarkjs wtns calculate \
        "$G1TIE/identity_membership_g1tie_js/identity_membership_g1tie.wasm" \
        "$OUT/g1tie_input.json" "$OUT/g1tie_witness.wtns"
    npx snarkjs groth16 prove "$G1TIE/g1tie_0001.zkey" \
        "$OUT/g1tie_witness.wtns" "$OUT/g1tie_proof.json" "$OUT/g1tie_public.json"
    npx snarkjs groth16 verify "$G1TIE/verification_key.json" \
        "$OUT/g1tie_public.json" "$OUT/g1tie_proof.json"

    # ---- 5. Note-binding proof (A2 only; A1's idHash layout is unsupported
    #         by the current binding circuit -- the fixture carries an empty
    #         proof and NotesE2E documents the gap) ----
    if [ "$FLAVOR" = "a2" ]; then
        bash -c "ulimit -s 65520 && '$NB/note_binding_cpp/note_binding' \
            '$OUT/note_binding_input.json' '$OUT/note_binding_witness.wtns'"
        "$RAPIDSNARK" "$NB/note_binding_0001.zkey" "$OUT/note_binding_witness.wtns" \
            "$OUT/note_binding_proof.json" "$OUT/note_binding_public.json"
        npx snarkjs groth16 verify "$NB/verification_key.json" \
            "$OUT/note_binding_public.json" "$OUT/note_binding_proof.json"
    fi

    # ---- 6. Assemble ----
    PYTHONPATH="$ROOT" python "$SCRIPT_DIR/gen_e2e_world.py" assemble --flavor="$FLAVOR"
done

echo "=== e2e fixtures complete: test/vectors/e2e/{${FLAVORS[*]// /,}}.json ==="
