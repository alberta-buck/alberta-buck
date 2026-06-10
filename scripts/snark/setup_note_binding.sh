#!/bin/bash
# Note-binding circuit: note_binding.circom
# Compiles the circuit, generates witness, runs Groth16 setup, exports
# the Solidity verifier, and regenerates Forge test vectors.
#
# WITNESS GENERATION — uses the circom C++ witness calculator (--no_asm).
# The WASM calculator cannot handle this circuit (~5.9M wires; "memory
# access out of bounds").  Two non-obvious requirements for the C++ path:
#
#   1. -fno-strict-aliasing is REQUIRED.  The generic (no_asm) fr.cpp
#      passes uint64_t* into GMP's inline mpn_* functions whose declared
#      limb type is unsigned long*.  Same width, but DIFFERENT types for
#      C++ strict-aliasing purposes: gcc -O3 concludes the uint64_t
#      stores cannot alias the unsigned long loads, reorders/elides them,
#      and field comparisons (Fr_eq etc.) silently read stale stack
#      garbage.  Manifests as "Assertion failed: (Fr_isTrue(&expaux[0]))"
#      on trivially-true constraints (e.g. dummy*dummy === 0).
#   2. ulimit -s 65520 (64 MB stack).  The generated template-run
#      functions allocate the BN254 G-powers table expansion in stack
#      frames of ~5.4 MB each; nested runs exceed the macOS 8 MB default.
#
# PROVING — uses rapidsnark (lib/rapidsnark-macOS-arm64-v0.0.8/bin) when
# present; the 2.4M-constraint proof takes seconds instead of the many
# minutes snarkjs needs.  Falls back to snarkjs if rapidsnark is missing.
#
# Prerequisites: a pot22+ Powers of Tau (build/snark/ptau/pot22_final.ptau).
# The circuit has ~2.4M non-linear constraints, so pot20 is NOT enough.
# A missing ptau is bootstrapped here with DEV entropy (see !! DEV ENTROPY !!
# in scripts/snark/setup.sh -- same caveat applies: do NOT use in production).
#
# Usage:
#   make nix-snark-note-binding
#   nix develop --command bash scripts/snark/setup_note_binding.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CIRCUIT="$REPO_ROOT/circuits/note_binding.circom"
BUILD_DIR="$REPO_ROOT/build/snark/note_binding"
POT_DIR="$REPO_ROOT/build/snark/ptau"
RAPIDSNARK_BIN="$REPO_ROOT/lib/rapidsnark-macOS-arm64-v0.0.8/bin"

# snarkjs needs a large heap for the 784 MB R1CS / pot22 operations.
export NODE_OPTIONS="--max-old-space-size=16384"

echo "=== Note-binding circuit setup ==="
echo "Circuit: $CIRCUIT"
echo "Build:   $BUILD_DIR"

mkdir -p "$BUILD_DIR" "$POT_DIR"

# ---- Step 1: Compile circuit (R1CS + C++ witness generator) ----
if [ -f "$BUILD_DIR/note_binding.r1cs" ] \
        && [ -f "$BUILD_DIR/note_binding_cpp/note_binding.cpp" ] \
        && [ "$BUILD_DIR/note_binding.r1cs" -nt "$CIRCUIT" ]; then
    echo "--- Circuit up to date (skipping circom) ---"
else
    echo "--- Compiling circuit (circom --c --no_asm) ---"
    circom "$CIRCUIT" --r1cs --c --no_asm --output "$BUILD_DIR"
fi
echo "  R1CS: $(ls -lh $BUILD_DIR/note_binding.r1cs | awk '{print $5}')"

# ---- Step 2: Build the C++ witness generator ----
echo "--- Building C++ witness generator ---"
( cd "$BUILD_DIR/note_binding_cpp" && \
  make CC=g++ "CFLAGS=-std=c++11 -O3 -fno-strict-aliasing -I. -Wno-deprecated-declarations -fpermissive" 2>&1 \
      | tail -1 )

# ---- Step 3: Generate test input ----
echo "--- Generating test input ---"
PYTHONPATH="$REPO_ROOT" python "$REPO_ROOT/scripts/snark/gen_note_binding_input.py" \
    > "$BUILD_DIR/input.json" 2>"$BUILD_DIR/witness_diag.txt"
cat "$BUILD_DIR/witness_diag.txt"

# ---- Step 4: Generate witness (64 MB stack; see header comment) ----
echo "--- Generating witness ---"
bash -c "ulimit -s 65520 && '$BUILD_DIR/note_binding_cpp/note_binding' \
    '$BUILD_DIR/input.json' '$BUILD_DIR/witness.wtns'"
echo "  Witness: $(ls -lh $BUILD_DIR/witness.wtns | awk '{print $5}')"

# ---- Step 5: Check the witness against ALL R1CS constraints ----
echo "--- Checking witness (snarkjs wtns check) ---"
snarkjs wtns check "$BUILD_DIR/note_binding.r1cs" "$BUILD_DIR/witness.wtns"

# ---- Step 6: Groth16 setup ----
PTAU=""
for pot in pot28 pot27 pot26 pot25 pot24 pot23 pot22; do
    candidate="$POT_DIR/${pot}_final.ptau"
    if [ -f "$candidate" ]; then
        PTAU="$candidate"
        break
    fi
done
if [ -z "$PTAU" ]; then
    echo "--- No pot22+ ptau found; bootstrapping pot22 (DEV entropy) ---"
    snarkjs ptn bn128 22 "$POT_DIR/pot22_0000.ptau"
    echo "alberta-buck-dev-ptau-22" | snarkjs ptc \
        "$POT_DIR/pot22_0000.ptau" "$POT_DIR/pot22_0001.ptau" \
        --name="alberta-buck-dev-22"
    snarkjs pt2 "$POT_DIR/pot22_0001.ptau" "$POT_DIR/pot22_final.ptau"
    PTAU="$POT_DIR/pot22_final.ptau"
fi
echo "--- Groth16 setup (ptau: $PTAU) ---"

snarkjs groth16 setup "$BUILD_DIR/note_binding.r1cs" "$PTAU" \
    "$BUILD_DIR/note_binding_0000.zkey"
echo "  Phase-2 setup done"

# DEV ENTROPY
snarkjs zkey contribute "$BUILD_DIR/note_binding_0000.zkey" \
    "$BUILD_DIR/note_binding_0001.zkey" \
    --name="alberta-buck-dev-note-binding" \
    -e="alberta-buck-dev-note-binding-entropy"
echo "  Contribution done"

snarkjs zkey export verificationkey "$BUILD_DIR/note_binding_0001.zkey" \
    "$BUILD_DIR/verification_key.json"

# ---- Step 7: Prove and verify off-chain ----
if [ -x "$RAPIDSNARK_BIN/prover" ]; then
    echo "--- Proving (rapidsnark) ---"
    "$RAPIDSNARK_BIN/prover" "$BUILD_DIR/note_binding_0001.zkey" \
        "$BUILD_DIR/witness.wtns" \
        "$BUILD_DIR/proof.json" "$BUILD_DIR/public.json"
else
    echo "--- Proving (snarkjs; run 'make lib/rapidsnark-macOS-arm64-v0.0.8.zip' for speed) ---"
    snarkjs groth16 prove "$BUILD_DIR/note_binding_0001.zkey" \
        "$BUILD_DIR/witness.wtns" \
        "$BUILD_DIR/proof.json" "$BUILD_DIR/public.json"
fi
echo "  Proof generated"

snarkjs groth16 verify "$BUILD_DIR/verification_key.json" \
    "$BUILD_DIR/public.json" "$BUILD_DIR/proof.json"
echo "  Off-chain verification (snarkjs): PASSED"

if [ -x "$RAPIDSNARK_BIN/verifier" ]; then
    "$RAPIDSNARK_BIN/verifier" "$BUILD_DIR/verification_key.json" \
        "$BUILD_DIR/public.json" "$BUILD_DIR/proof.json"
    echo "  Off-chain verification (rapidsnark): PASSED"
fi

# ---- Step 8: Export Solidity verifier ----
snarkjs zkey export solidityverifier "$BUILD_DIR/note_binding_0001.zkey" \
    "$BUILD_DIR/Groth16Verifier.sol"
sed -i.bak 's/contract Groth16Verifier/contract NoteBindingGroth16Verifier/g' \
    "$BUILD_DIR/Groth16Verifier.sol"
sed -i.bak 's/public view returns/public returns/g' \
    "$BUILD_DIR/Groth16Verifier.sol"
rm -f "$BUILD_DIR/Groth16Verifier.sol.bak"
cp "$BUILD_DIR/Groth16Verifier.sol" \
    "$REPO_ROOT/src/NoteBindingGroth16Verifier.sol"
python3 "$REPO_ROOT/scripts/snark/fix_verifier_g2.py" \
    "$REPO_ROOT/src/NoteBindingGroth16Verifier.sol"
echo "  -> src/NoteBindingGroth16Verifier.sol (EIP-197 G2 fix applied)"

# ---- Step 9: Generate Forge test vectors ----
VECTORS_DIR="$REPO_ROOT/test/vectors/note_binding"
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
echo "=== Note-binding circuit setup complete ==="
echo "Verifier: src/NoteBindingGroth16Verifier.sol"
echo "Adapter:  src/NoteBindingVerifierAdapter.sol"
echo "Vectors:  test/vectors/note_binding/proof.json"
echo "Run: forge test --match-contract NoteBindingVerifier"
