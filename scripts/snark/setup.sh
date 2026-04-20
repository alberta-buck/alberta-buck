#!/usr/bin/env bash
# Groth16 trusted setup for the mint circuit (development only).
#
# Produces:
#   build/snark/ptau/pot12_final.ptau   - universal powers of tau
#   build/snark/mint/mint_final.zkey    - circuit-specific proving key
#   build/snark/mint/verification_key.json
#   src/MintGroth16Verifier.sol         - auto-generated Solidity verifier
#
# The ceremony contributions use fixed dev-only entropy; production deployments
# must replace this with a real multi-party ceremony.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="$ROOT/build/snark"
POT_DIR="$BUILD/ptau"
MINT="$BUILD/mint"
POT_POW=12
PTAU0="$POT_DIR/pot${POT_POW}_0000.ptau"
PTAU1="$POT_DIR/pot${POT_POW}_0001.ptau"
PTAUF="$POT_DIR/pot${POT_POW}_final.ptau"
R1CS="$MINT/mint.r1cs"
ZKEY0="$MINT/mint_0000.zkey"
ZKEYF="$MINT/mint_final.zkey"
VKEY="$MINT/verification_key.json"
VERIFIER="$ROOT/src/MintGroth16Verifier.sol"

mkdir -p "$POT_DIR" "$MINT"

if [ ! -f "$R1CS" ]; then
    echo "[circom] compiling circuits/mint.circom"
    mkdir -p "$MINT"
    ( cd "$ROOT/circuits" && \
      circom mint.circom --r1cs --wasm --sym --output "$MINT" -l ../node_modules )
fi

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

if [ ! -f "$ZKEYF" ]; then
    echo "[zkey] groth16 setup"
    snarkjs g16s "$R1CS" "$PTAUF" "$ZKEY0" -v
    echo "[zkey] contribute (dev entropy)"
    echo "alberta-buck-dev-zkey" | snarkjs zkc "$ZKEY0" "$ZKEYF" \
        --name="alberta-buck-dev" -v
    echo "[zkey] export verification key"
    snarkjs zkev "$ZKEYF" "$VKEY"
else
    echo "[zkey] reusing $ZKEYF"
fi

echo "[sol] exporting Solidity verifier"
snarkjs zkesv "$ZKEYF" "$VERIFIER"
# snarkjs's generic template names the contract Groth16Verifier; rename to
# match the rest of the project.
sed -i.bak 's/contract Groth16Verifier/contract MintGroth16Verifier/' "$VERIFIER"
rm -f "$VERIFIER.bak"
echo "wrote $VERIFIER"

echo "setup complete"
