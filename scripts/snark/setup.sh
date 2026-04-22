#!/usr/bin/env bash
# Groth16 trusted setup for the BUCK Notes circuits (development only).
#
# Produces (per circuit <c> in {mint, spend}):
#   build/snark/ptau/pot${POT_POW}_final.ptau   - universal powers of tau
#   build/snark/<c>/<c>_final.zkey              - circuit-specific proving key
#   build/snark/<c>/verification_key.json
#   src/<C>Groth16Verifier.sol                  - auto-generated Solidity verifier
#
# The mint circuit is small enough for pow 12; the spend circuit
# (~12k total constraints from the depth-20 Merkle path + Poseidon openings)
# bumps the universal ceremony to pow 15 -- snarkjs sizes the FFT domain at
# `2 * constraints` rounded up to the next power of 2, so 12120 * 2 = 24240
# requires 2^15 = 32768.  Both circuits share that pot15 ptau -- ptau is
# "universal" within a single curve, larger only costs constant time per
# circuit beyond what each needs.
#
# Ceremony contributions use fixed dev-only entropy; production deployments
# must replace this with a real multi-party ceremony.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="$ROOT/build/snark"
POT_DIR="$BUILD/ptau"
POT_POW=15
PTAU0="$POT_DIR/pot${POT_POW}_0000.ptau"
PTAU1="$POT_DIR/pot${POT_POW}_0001.ptau"
PTAUF="$POT_DIR/pot${POT_POW}_final.ptau"

mkdir -p "$POT_DIR"

if [ ! -f "$PTAUF" ]; then
    echo "[ptau] new bn128 2^${POT_POW}"
    snarkjs ptn bn128 "$POT_POW" "$PTAU0" -v
    echo "[ptau] contribute (dev entropy)"
    echo "alberta-buck-dev-ptau" | snarkjs ptc "$PTAU0" "$PTAU1" \
        --name="alberta-buck-dev" -v
    echo "[ptau] prepare phase 2"
    snarkjs pt2 "$PTAU1" "$PTAUF" -v
else
    echo "[ptau] reusing $PTAUF"
fi

# ---------------------------------------------------------------------------
# Per-circuit setup.  Each circuit gets its own zkey, verification key, and
# Solidity verifier.  The Solidity contract name is forced to
# "<C>Groth16Verifier" via sed since snarkjs's template names every contract
# the same generic "Groth16Verifier".

setup_circuit() {
    local CIRCUIT="$1"          # e.g. "mint", "spend"
    local CONTRACT_PREFIX="$2"  # e.g. "Mint", "Spend"
    local OUT="$BUILD/$CIRCUIT"
    local R1CS="$OUT/${CIRCUIT}.r1cs"
    local ZKEY0="$OUT/${CIRCUIT}_0000.zkey"
    local ZKEYF="$OUT/${CIRCUIT}_final.zkey"
    local VKEY="$OUT/verification_key.json"
    local VERIFIER="$ROOT/src/${CONTRACT_PREFIX}Groth16Verifier.sol"

    mkdir -p "$OUT"

    if [ ! -f "$R1CS" ]; then
        echo "[circom] compiling circuits/${CIRCUIT}.circom"
        ( cd "$ROOT/circuits" && \
          circom "${CIRCUIT}.circom" --r1cs --wasm --sym --output "$OUT" -l ../node_modules )
    fi

    if [ ! -f "$ZKEYF" ]; then
        echo "[zkey] groth16 setup ($CIRCUIT)"
        snarkjs g16s "$R1CS" "$PTAUF" "$ZKEY0" -v
        echo "[zkey] contribute ($CIRCUIT, dev entropy)"
        echo "alberta-buck-dev-zkey-${CIRCUIT}" | snarkjs zkc "$ZKEY0" "$ZKEYF" \
            --name="alberta-buck-dev-${CIRCUIT}" -v
        echo "[zkey] export verification key ($CIRCUIT)"
        snarkjs zkev "$ZKEYF" "$VKEY"
    else
        echo "[zkey] reusing $ZKEYF"
    fi

    echo "[sol] exporting Solidity verifier ($CIRCUIT)"
    snarkjs zkesv "$ZKEYF" "$VERIFIER"
    sed -i.bak "s/contract Groth16Verifier/contract ${CONTRACT_PREFIX}Groth16Verifier/" "$VERIFIER"
    rm -f "$VERIFIER.bak"
    echo "wrote $VERIFIER"
}

setup_circuit mint  Mint
setup_circuit spend Spend

echo "setup complete"
