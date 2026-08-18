#!/bin/bash
# G1-tie circuit: identity_membership_g1tie.circom
# Compiles the circuit, generates witness, runs Groth16 setup, exports
# the Solidity verifier, and regenerates Forge test vectors.
#
# Prerequisites: a pot16+ Powers of Tau (build/snark/ptau/pot16_final.ptau).
#   make snark-ptau   # if ptau not yet built
#
# Usage:
#   make snark-g1tie
#   nix develop --command bash scripts/snark/setup_g1tie.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CIRCUIT="$REPO_ROOT/circuits/identity_membership_g1tie.circom"
BUILD_DIR="$REPO_ROOT/build/snark/g1tie"

echo "=== G1-tie circuit setup ==="
echo "Circuit: $CIRCUIT"
echo "Build:   $BUILD_DIR"

mkdir -p "$BUILD_DIR"

# ---- Step 1: Compile circuit ----
echo "--- Compiling circuit ---"
circom "$CIRCUIT" --r1cs --wasm --output "$BUILD_DIR"
echo "  R1CS: $(ls -lh $BUILD_DIR/identity_membership_g1tie.r1cs | awk '{print $5}')"
echo "  WASM: $(ls -lh $BUILD_DIR/identity_membership_g1tie_js/identity_membership_g1tie.wasm | awk '{print $5}')"

# ---- Step 2: Generate witness ----
echo "--- Generating test witness ---"
PYTHONPATH="$REPO_ROOT" python "$REPO_ROOT/scripts/snark/gen_g1tie_input.py" \
    > "$BUILD_DIR/input.json" 2>"$BUILD_DIR/witness_diag.txt"
cat "$BUILD_DIR/witness_diag.txt"

snarkjs wtns calculate \
    "$BUILD_DIR/identity_membership_g1tie_js/identity_membership_g1tie.wasm" \
    "$BUILD_DIR/input.json" \
    "$BUILD_DIR/witness.wtns"
echo "  Witness generated"

# ---- Step 3: Groth16 setup ----
echo "--- Groth16 setup ---"
PTAU=""
for pot in pot16 pot17 pot18 pot19 pot20; do
    candidate="$REPO_ROOT/build/snark/ptau/${pot}_final.ptau"
    if [ -f "$candidate" ]; then
        PTAU="$candidate"
        break
    fi
done
if [ -z "$PTAU" ]; then
    echo "ERROR: No suitable ptau found in build/snark/ptau/. Run make snark-ptau first."
    exit 1
fi
echo "  Using ptau: $PTAU"

snarkjs groth16 setup "$BUILD_DIR/identity_membership_g1tie.r1cs" "$PTAU" \
    "$BUILD_DIR/g1tie_0000.zkey"
echo "  Phase-2 setup done"

# DEV ENTROPY — reproducible dev-only contribution
snarkjs zkey contribute "$BUILD_DIR/g1tie_0000.zkey" "$BUILD_DIR/g1tie_0001.zkey" \
    --name="alberta-buck-dev-g1tie" -v -e="alberta-buck-dev-g1tie-entropy"
echo "  Contribution done"

# Export verification key BEFORE proving (needed for verify step)
snarkjs zkey export verificationkey "$BUILD_DIR/g1tie_0001.zkey" \
    "$BUILD_DIR/verification_key.json"
echo "  Verification key exported"

# ---- Step 4: Prove and verify off-chain ----
echo "--- Proving ---"
snarkjs groth16 prove "$BUILD_DIR/g1tie_0001.zkey" "$BUILD_DIR/witness.wtns" \
    "$BUILD_DIR/proof.json" "$BUILD_DIR/public.json"
echo "  Proof generated"

echo "--- Verifying (off-chain) ---"
snarkjs groth16 verify "$BUILD_DIR/verification_key.json" \
    "$BUILD_DIR/public.json" "$BUILD_DIR/proof.json"
echo "  Off-chain verification: PASSED"

# ---- Step 5: Export Solidity verifier ----
echo "--- Exporting Solidity verifier ---"
snarkjs zkey export solidityverifier "$BUILD_DIR/g1tie_0001.zkey" \
    "$BUILD_DIR/Groth16Verifier.sol"
# Rename + view fix using explicit backup extension (macOS sed -i requires it)
sed -i.bak 's/contract Groth16Verifier/contract IdentityMembershipG1TieVerifier/g' \
    "$BUILD_DIR/Groth16Verifier.sol"
sed -i.bak 's/public view returns/public returns/g' \
    "$BUILD_DIR/Groth16Verifier.sol"
rm -f "$BUILD_DIR/Groth16Verifier.sol.bak"
cp "$BUILD_DIR/Groth16Verifier.sol" \
    "$REPO_ROOT/src/IdentityMembershipG1TieVerifier.sol"
# EIP-197 G2 encoding fix: MUST run after cp to avoid macOS sed temp-file issues
# (sed -i uses atomic rename; python must open the file after sed completes)
# --b-only is REQUIRED for snarkjs 0.7.5+: it already emits the VK constants
# in EIP-197 order, so the default mode swaps them a SECOND time and every
# proof then fails against the pairing precompile.  Only the proof-B swap is
# still wanted.  Omitting this flag silently produced a verifier that
# rejected all 11 membership proofs in the suite.
python3 "$REPO_ROOT/scripts/snark/fix_verifier_g2.py" --b-only \
    "$REPO_ROOT/src/IdentityMembershipG1TieVerifier.sol"
echo "  -> src/IdentityMembershipG1TieVerifier.sol (EIP-197 G2 fix applied)"

# ---- Step 6: Generate Forge test vectors ----
echo "--- Generating Forge test vectors ---"
VECTORS_DIR="$REPO_ROOT/test/vectors/g1tie"
mkdir -p "$VECTORS_DIR"

python3 -c "
import json, os
with open('$BUILD_DIR/proof.json') as f: proof = json.load(f)
with open('$BUILD_DIR/public.json') as f: pub = json.load(f)
vectors = {
    'a': [str(proof['pi_a'][0]), str(proof['pi_a'][1])],
    'b': [
        str(proof['pi_b'][0][0]), str(proof['pi_b'][0][1]),
        str(proof['pi_b'][1][0]), str(proof['pi_b'][1][1]),
    ],
    'c': [str(proof['pi_c'][0]), str(proof['pi_c'][1])],
    'pub': [str(p) for p in pub],
}
with open('$VECTORS_DIR/proof.json', 'w') as f:
    json.dump(vectors, f, indent=2)
"
echo "  -> $VECTORS_DIR/proof.json"

echo ""
echo "=== G1-tie circuit setup complete ==="
echo "Verifier: src/IdentityMembershipG1TieVerifier.sol"
echo "Vectors:  test/vectors/g1tie/proof.json"
echo ""
echo "Run: forge test --match-contract IdentityMembershipG1TieVerifier"
