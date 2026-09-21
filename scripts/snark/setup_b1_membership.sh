#!/bin/bash
# B1's P-bound identity membership: circuits/identity_membership_b1.circom
#
# The repaired successor to setup_g1tie.sh.  Same shape; the circuit differs in
# that T = b*H_PEDERSEN is PROVEN rather than witnessed, the witnessed limbs are
# range-checked, the one addition has its precondition enforced, and the leaf is
# the salted one.  ~489K constraints, so pot20 is ample.
#
# IMPORTANT: snarkjs groth16 setup is non-deterministic (delta varies per run).
# The zkey, proof, verifier and vectors are a MATCHED SET from a single run.
#
# DEV ENTROPY: the contribution below is reproducible and dev-only.  It is NOT
# a ceremony and MUST NOT be used for a deployment.
#
# Usage:
#   nix develop --command bash scripts/snark/setup_b1_membership.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CIRCUIT="$REPO_ROOT/circuits/identity_membership_b1.circom"
BUILD_DIR="$REPO_ROOT/build/snark/b1_membership"
NAME="identity_membership_b1"

echo "=== B1 membership circuit setup (DEV ENTROPY) ==="
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "--- Compiling ---"
# --O2 is REQUIRED, not an optimisation: it substitutes the linear constraints
# away.  Without it this circuit is 1,240,788 constraints and snarkjs refuses
# ("circuit too big for this power of tau ceremony"); with it, 489,344, which
# pot20 covers.  It also has to match the flags the witness was validated under.
circom "$CIRCUIT" --r1cs --wasm --O2 --output "$BUILD_DIR"

echo "--- Witness ---"
PYTHONPATH="$REPO_ROOT:$REPO_ROOT/core/python" python \
    "$REPO_ROOT/scripts/snark/gen_b1_membership_input.py" \
    > "$BUILD_DIR/input.json" 2>"$BUILD_DIR/witness_diag.txt"
cat "$BUILD_DIR/witness_diag.txt"
snarkjs wtns calculate "$BUILD_DIR/${NAME}_js/${NAME}.wasm" \
    "$BUILD_DIR/input.json" "$BUILD_DIR/witness.wtns"

echo "--- Groth16 setup ---"
PTAU=""
for pot in pot20 pot22; do
    candidate="$REPO_ROOT/build/snark/ptau/${pot}_final.ptau"
    [ -f "$candidate" ] && { PTAU="$candidate"; break; }
done
[ -n "$PTAU" ] || { echo "ERROR: no pot20+ ptau"; exit 1; }
echo "  ptau: $PTAU"

snarkjs groth16 setup "$BUILD_DIR/${NAME}.r1cs" "$PTAU" "$BUILD_DIR/b1m_0000.zkey"
snarkjs zkey contribute "$BUILD_DIR/b1m_0000.zkey" "$BUILD_DIR/b1m_0001.zkey" \
    --name="alberta-buck-dev-b1-membership" -v -e="alberta-buck-dev-b1-entropy"
snarkjs zkey export verificationkey "$BUILD_DIR/b1m_0001.zkey" \
    "$BUILD_DIR/verification_key.json"

echo "--- Prove + verify ---"
snarkjs groth16 prove "$BUILD_DIR/b1m_0001.zkey" "$BUILD_DIR/witness.wtns" \
    "$BUILD_DIR/proof.json" "$BUILD_DIR/public.json"
snarkjs groth16 verify "$BUILD_DIR/verification_key.json" \
    "$BUILD_DIR/public.json" "$BUILD_DIR/proof.json"

echo "--- Solidity verifier ---"
snarkjs zkey export solidityverifier "$BUILD_DIR/b1m_0001.zkey" \
    "$BUILD_DIR/Groth16Verifier.sol"
sed -i.bak 's/contract Groth16Verifier/contract IdentityMembershipB1Verifier/g' \
    "$BUILD_DIR/Groth16Verifier.sol"
sed -i.bak 's/public view returns/public returns/g' "$BUILD_DIR/Groth16Verifier.sol"
rm -f "$BUILD_DIR/Groth16Verifier.sol.bak"

cp "$BUILD_DIR/Groth16Verifier.sol" \
    "$REPO_ROOT/src/IdentityMembershipB1Verifier.sol"
# Committed STOCK -- byte-for-byte as snarkjs exports it, after the rename.
# The EIP-197 pi_b swap happens at vector PACKING below instead, which is what
# snarkjs's own `zkey export soliditycalldata` does.
echo "  -> src/IdentityMembershipB1Verifier.sol"

echo "--- Forge test vectors ---"
VECTORS_DIR="$REPO_ROOT/test/vectors/b1_membership"
mkdir -p "$VECTORS_DIR"
python3 -c "
import json
proof = json.load(open('$BUILD_DIR/proof.json'))
pub   = json.load(open('$BUILD_DIR/public.json'))
json.dump({
    'a': [str(proof['pi_a'][0]), str(proof['pi_a'][1])],
    # snarkjs stores each pi_b pair in the opposite order to the Solidity
    # verifier's EIP-197 expectation; swap within each pair.
    'b': [str(proof['pi_b'][0][1]), str(proof['pi_b'][0][0]),
          str(proof['pi_b'][1][1]), str(proof['pi_b'][1][0])],
    'c': [str(proof['pi_c'][0]), str(proof['pi_c'][1])],
    'pub': [str(x) for x in pub],
}, open('$VECTORS_DIR/proof.json', 'w'), indent=2)
"
echo "  -> test/vectors/b1_membership/proof.json"

echo "=== done: $BUILD_DIR ==="
