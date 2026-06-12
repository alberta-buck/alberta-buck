#!/usr/bin/env bash
# Compile circuits/identity_membership.circom and check that the Python
# reference IdentityTree's authentication path satisfies the circuit (a genuine
# member is accepted; a wrong root is rejected).  This pins the native
# Poseidon-Merkle half of the unified Notes membership gate to
# alberta_buck.wallet.unilateral_a2.IdentityTree.
#
# Run:  nix develop --command bash scripts/snark/test_identity_membership.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/build/snark/identity_membership"
mkdir -p "$OUT"

echo "[1/4] compile circuit"
( cd "$ROOT/circuits" && \
  circom identity_membership.circom --r1cs --wasm --sym --output "$OUT" -l "$ROOT/node_modules" )

echo "[2/4] generate witness inputs from the Python IdentityTree"
python "$ROOT/scripts/snark/gen_identity_membership_input.py" "$OUT"

WGEN="$OUT/identity_membership_js/generate_witness.js"
WASM="$OUT/identity_membership_js/identity_membership.wasm"

echo "[3/4] genuine member must be ACCEPTED (witness generation succeeds)"
node "$WGEN" "$WASM" "$OUT/input.json" "$OUT/witness.wtns"
echo "      -> accepted"

echo "[4/4] wrong root must be REJECTED (constraint identityRoot === root fails)"
if node "$WGEN" "$WASM" "$OUT/input_bad.json" "$OUT/witness_bad.wtns" 2>/dev/null; then
    echo "      -> ERROR: wrong-root witness was accepted" >&2
    exit 1
else
    echo "      -> rejected (as required)"
fi

# Report the R1CS size (the native membership is small).
echo "--- circuit info ---"
snarkjs r1cs info "$OUT/identity_membership.r1cs" | grep -E 'Constraints|Wires|Public|Private' || true

echo "PASS: identity_membership agrees with the Python IdentityTree reference."
