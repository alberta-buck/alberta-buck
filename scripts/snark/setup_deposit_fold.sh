#!/bin/bash
# The folded deposit gates: circuits/deposit_fold_a1.circom, deposit_fold_a2.circom
#
#   bash scripts/snark/setup_deposit_fold.sh a1
#   bash scripts/snark/setup_deposit_fold.sh a2
#
# ~3.3M constraints each, so pot22 and the circom C++ witness calculator (the
# WASM one does not cope at this size).  The C++ build REQUIRES
# -fno-strict-aliasing and a 64 MB stack, both handled below -- see
# setup_note_binding.sh for why.
#
# IMPORTANT: snarkjs groth16 setup is non-deterministic.  The zkey, proof,
# verifier and vectors are a MATCHED SET from a single run.
#
# DEV ENTROPY: the contribution is reproducible and dev-only.  It is NOT a
# ceremony and MUST NOT be used for a deployment.

set -euo pipefail

FLAVOR="${1:-a1}"
case "$FLAVOR" in a1|a2) ;; *) echo "usage: $0 a1|a2"; exit 1 ;; esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
NAME="deposit_fold_${FLAVOR}"
CIRCUIT="$REPO_ROOT/circuits/${NAME}.circom"
BUILD_DIR="$REPO_ROOT/build/snark/${NAME}"

echo "=== ${NAME} setup (DEV ENTROPY) ==="
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "--- Compiling (C++ calculator) ---"
circom "$CIRCUIT" --r1cs --c --no_asm --O2 --output "$BUILD_DIR"
( cd "$BUILD_DIR/${NAME}_cpp" && \
  make CC=g++ "CFLAGS=-std=c++11 -O3 -fno-strict-aliasing -I. -Wno-deprecated-declarations -fpermissive" )

echo "--- Witness ---"
PYTHONPATH="$REPO_ROOT:$REPO_ROOT/core/python" python \
    "$REPO_ROOT/scripts/snark/gen_${NAME}_input.py" \
    > "$BUILD_DIR/input.json" 2>"$BUILD_DIR/witness_diag.txt"
cat "$BUILD_DIR/witness_diag.txt"
bash -c "ulimit -s 65520 && '$BUILD_DIR/${NAME}_cpp/${NAME}' '$BUILD_DIR/input.json' '$BUILD_DIR/witness.wtns'"
snarkjs wtns check "$BUILD_DIR/${NAME}.r1cs" "$BUILD_DIR/witness.wtns"

echo "--- Groth16 setup (pot22) ---"
PTAU="$REPO_ROOT/build/snark/ptau/pot22_final.ptau"
[ -f "$PTAU" ] || { echo "ERROR: pot22_final.ptau missing"; exit 1; }
snarkjs groth16 setup "$BUILD_DIR/${NAME}.r1cs" "$PTAU" "$BUILD_DIR/${NAME}_0000.zkey"
snarkjs zkey contribute "$BUILD_DIR/${NAME}_0000.zkey" "$BUILD_DIR/${NAME}_0001.zkey" \
    --name="alberta-buck-dev-${NAME}" -v -e="alberta-buck-dev-${NAME}-entropy"
snarkjs zkey export verificationkey "$BUILD_DIR/${NAME}_0001.zkey" \
    "$BUILD_DIR/verification_key.json"

echo "--- Prove + verify ---"
if [ -x "$REPO_ROOT/lib/rapidsnark-macOS-arm64-v0.0.8/bin/prover" ]; then
    "$REPO_ROOT/lib/rapidsnark-macOS-arm64-v0.0.8/bin/prover" \
        "$BUILD_DIR/${NAME}_0001.zkey" "$BUILD_DIR/witness.wtns" \
        "$BUILD_DIR/proof.json" "$BUILD_DIR/public.json"
else
    snarkjs groth16 prove "$BUILD_DIR/${NAME}_0001.zkey" "$BUILD_DIR/witness.wtns" \
        "$BUILD_DIR/proof.json" "$BUILD_DIR/public.json"
fi
snarkjs groth16 verify "$BUILD_DIR/verification_key.json" \
    "$BUILD_DIR/public.json" "$BUILD_DIR/proof.json"

echo "--- Solidity verifier ---"
snarkjs zkey export solidityverifier "$BUILD_DIR/${NAME}_0001.zkey" \
    "$BUILD_DIR/Groth16Verifier.sol"
UP="$(echo "$FLAVOR" | tr 'a-z' 'A-Z')"
sed -i.bak "s/contract Groth16Verifier/contract DepositFold${UP}Verifier/g" \
    "$BUILD_DIR/Groth16Verifier.sol"
sed -i.bak 's/public view returns/public returns/g' "$BUILD_DIR/Groth16Verifier.sol"
rm -f "$BUILD_DIR/Groth16Verifier.sol.bak"

echo "=== done: $BUILD_DIR ==="
