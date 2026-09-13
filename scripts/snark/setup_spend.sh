#!/usr/bin/env bash
# Isolated Groth16 setup for circuits/spend.circom ONLY.
#
# Compiles, sets up, and exports into an isolated directory, then copies the
# matched set (r1cs, wasm, zkey, verification key, Solidity verifier) into
# build/snark/spend and src/SpendGroth16Verifier.sol.  Does not touch mint,
# g1tie, or note-binding artifacts.
#
# Prerequisites: pot15 Powers of Tau at build/snark/ptau/pot15_final.ptau
# and node_modules/circomlib (npm install / flake hook).
#
# Usage:
#   make nix-snark-spend
#   nix develop --command bash scripts/snark/setup_spend.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="$ROOT/build/snark"
ISOLATED="$BUILD/_spend_isolated"
OUT="$BUILD/spend"
PTAU="$BUILD/ptau/pot15_final.ptau"
VERIFIER="$ROOT/src/SpendGroth16Verifier.sol"

if [ ! -f "$PTAU" ]; then
    echo "ERROR: missing $PTAU -- copy or symlink pot15_final.ptau (see doc/snark-regeneration.org)" >&2
    exit 1
fi
if [ ! -d "$ROOT/node_modules/circomlib" ]; then
    echo "ERROR: missing node_modules/circomlib; run npm install or enter nix develop" >&2
    exit 1
fi

rm -rf "$ISOLATED"
mkdir -p "$ISOLATED"

echo "[circom] compiling circuits/spend.circom -> $ISOLATED"
( cd "$ROOT/circuits" && \
  circom spend.circom --r1cs --wasm --sym --output "$ISOLATED" -l ../node_modules )

R1CS="$ISOLATED/spend.r1cs"
ZKEY0="$ISOLATED/spend_0000.zkey"
ZKEYF="$ISOLATED/spend_final.zkey"
VKEY="$ISOLATED/verification_key.json"
SOL="$ISOLATED/SpendGroth16Verifier.sol"

echo "[zkey] groth16 setup (spend, isolated)"
snarkjs g16s "$R1CS" "$PTAU" "$ZKEY0" -v
echo "[zkey] contribute (spend, dev entropy)"
echo "alberta-buck-dev-zkey-spend" | snarkjs zkc "$ZKEY0" "$ZKEYF" \
    --name="alberta-buck-dev-spend" -v
echo "[zkey] export verification key (spend)"
snarkjs zkev "$ZKEYF" "$VKEY"

echo "[sol] exporting Solidity verifier -> isolated SpendGroth16Verifier.sol"
snarkjs zkesv "$ZKEYF" "$SOL"
sed -i.bak "s/contract Groth16Verifier/contract SpendGroth16Verifier/" "$SOL"
rm -f "$SOL.bak"

echo "[copy] matched set -> $OUT and $VERIFIER"
rm -rf "$OUT"
mkdir -p "$OUT"
cp "$R1CS" "$ISOLATED/spend.sym" "$ZKEY0" "$ZKEYF" "$VKEY" "$OUT/"
cp -R "$ISOLATED/spend_js" "$OUT/spend_js"
cp "$SOL" "$VERIFIER"

rm -rf "$ISOLATED"
echo "setup_spend: wrote $OUT and $VERIFIER"

echo "[vectors] re-proving e2e spend openings against the new zkey"
PYTHON="${PYTHON:-python3}"
if [ -x /Users/perry/src/alberta-buck.venv-0.1.0-nix-darwin-cpython-313/bin/python ]; then
    PYTHON=/Users/perry/src/alberta-buck.venv-0.1.0-nix-darwin-cpython-313/bin/python
fi
PYTHONPATH="$ROOT" "$PYTHON" "$ROOT/scripts/snark/regen_spend_vectors.py"
echo "setup_spend: matched spend set ready"
