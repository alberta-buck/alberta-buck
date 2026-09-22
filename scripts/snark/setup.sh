#!/usr/bin/env bash
# Groth16 trusted setup for the BUCK Notes circuits (development only).
#
# Produces (per circuit):
#   build/snark/ptau/pot${POT_POW}_final.ptau   - universal powers of tau
#   build/snark/<c>/<c>_final.zkey              - circuit-specific proving key
#   build/snark/<c>/verification_key.json
#   src/<C>Groth16Verifier.sol                  - auto-generated Solidity verifier
#
# The legacy `mint` and `spend` circuits share a pot15 ptau (`spend` is the
# heavier of the two at ~12k constraints, FFT domain ~2x => 2^15).
#
# `mint_batch` (Phase 7-bis pivot) is the larger circuit -- per-leaf in-circuit
# Merkle insertion costs ~10K R1CS, so each pinned-N variant has its own ptau:
#   N= 16  -> ~166K R1CS  -> need 2^18 = 262144
#   N=128  -> ~1.3M R1CS  -> need 2^21 = 2097152
# The pinned set is configurable via `MINT_BATCH_PINS` (default "16").  Adding
# a larger pin auto-bootstraps a larger ptau; the script reuses ptau files
# when they already exist.
#
# Ceremony contributions use fixed dev-only entropy; production deployments
# must replace this with a real multi-party ceremony.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="$ROOT/build/snark"
POT_DIR="$BUILD/ptau"

mkdir -p "$POT_DIR"

# Ensure a ptau of at least the requested power exists; build one if needed.
# Prints the absolute path of the chosen ptau on stdout.
ensure_ptau() {
    local POW="$1"
    local PTAU0="$POT_DIR/pot${POW}_0000.ptau"
    local PTAU1="$POT_DIR/pot${POW}_0001.ptau"
    local PTAUF="$POT_DIR/pot${POW}_final.ptau"

    if [ ! -f "$PTAUF" ]; then
        echo "[ptau] new bn128 2^${POW}" >&2
        snarkjs ptn bn128 "$POW" "$PTAU0" -v >&2
        echo "[ptau] contribute (dev entropy)" >&2
        echo "alberta-buck-dev-ptau-${POW}" | snarkjs ptc "$PTAU0" "$PTAU1" \
            --name="alberta-buck-dev-${POW}" -v >&2
        echo "[ptau] prepare phase 2" >&2
        snarkjs pt2 "$PTAU1" "$PTAUF" -v >&2
    else
        echo "[ptau] reusing $PTAUF" >&2
    fi
    printf '%s' "$PTAUF"
}

# Per-circuit setup.  Each circuit gets its own zkey, verification key, and
# Solidity verifier.  The Solidity contract name is forced to
# "<C>Groth16Verifier" via sed since snarkjs's template names every contract
# the same generic "Groth16Verifier".
setup_circuit() {
    local CIRCUIT="$1"          # e.g. "mint", "spend"
    local CONTRACT_NAME="$2"    # e.g. "MintGroth16Verifier"
    local PTAU="$3"
    local SRC_NAME="${4:-$CIRCUIT}"  # circom source filename (without .circom)
    local OUT="$BUILD/$CIRCUIT"
    local R1CS="$OUT/${SRC_NAME}.r1cs"
    local ZKEY0="$OUT/${SRC_NAME}_0000.zkey"
    local ZKEYF="$OUT/${SRC_NAME}_final.zkey"
    local VKEY="$OUT/verification_key.json"
    local VERIFIER="$ROOT/src/${CONTRACT_NAME}.sol"

    mkdir -p "$OUT"

    if [ ! -f "$R1CS" ]; then
        echo "[circom] compiling circuits/${SRC_NAME}.circom -> $OUT"
        ( cd "$ROOT/circuits" && \
          circom "${SRC_NAME}.circom" --r1cs --wasm --sym --output "$OUT" -l ../node_modules )
    fi

    if [ ! -f "$ZKEYF" ]; then
        echo "[zkey] groth16 setup ($CIRCUIT)"
        snarkjs g16s "$R1CS" "$PTAU" "$ZKEY0" -v
        echo "[zkey] contribute ($CIRCUIT, dev entropy)"
        echo "alberta-buck-dev-zkey-${CIRCUIT}" | snarkjs zkc "$ZKEY0" "$ZKEYF" \
            --name="alberta-buck-dev-${CIRCUIT}" -v
        echo "[zkey] export verification key ($CIRCUIT)"
        snarkjs zkev "$ZKEYF" "$VKEY"
    else
        echo "[zkey] reusing $ZKEYF"
    fi

    echo "[sol] exporting Solidity verifier ($CIRCUIT) -> ${CONTRACT_NAME}.sol"
    snarkjs zkesv "$ZKEYF" "$VERIFIER"
    sed -i.bak "s/contract Groth16Verifier/contract ${CONTRACT_NAME}/" "$VERIFIER"
    rm -f "$VERIFIER.bak"
    echo "wrote $VERIFIER"
}

# Ptau power needed for a given mint_batch N.  Empirically N=16 produces
# ~164K non-linear + ~185K linear constraints; snarkjs's groth16 setup needs
# an FFT domain >= total constraints, so 2^19 = 524288 suffices for N=16.
# Each doubling of N roughly doubles the constraints; small-N variants
# (N=1..8) sit comfortably inside pot15..pot18.
ptau_pow_for_n() {
    local N="$1"
    case "$N" in
          1) echo 15 ;;
          2) echo 16 ;;
          4) echo 17 ;;
          8) echo 18 ;;
         16) echo 19 ;;
         32) echo 20 ;;
         64) echo 21 ;;
        128) echo 22 ;;
        256) echo 23 ;;
        512) echo 24 ;;
       1024) echo 25 ;;
        *)  echo "unsupported N=$N" >&2; return 1 ;;
    esac
}

# Render a per-N copy of mint_batch.circom with the `component main` line
# rewritten.  Keeps mint_batch.circom as the canonical template; per-N copies
# live alongside it under circuits/mint_batch_n${N}.circom (gitignored at
# generation; tracked here as ephemeral build inputs).
render_mint_batch_n() {
    local N="$1"
    local SRC="$ROOT/circuits/mint_batch.circom"
    local DST="$ROOT/circuits/mint_batch_n${N}.circom"
    sed -e "s|component main { public \[ oldRoot, newRoot, nextLeafIndex, totalFace, cm \] } = MintBatch(16, 20);|component main { public [ oldRoot, newRoot, nextLeafIndex, totalFace, cm ] } = MintBatch(${N}, 20);|" \
        "$SRC" > "$DST"
}

# Same idea for the A2 (private-issuer) mint circuit, which exposes per-leaf
# eIss and the binding's T as public outputs so Notes.mint can tie each
# committed leaf to its re-encryption binding (the collusion-resistant A2
# leaf-tie).  Reuses the same ptau as mint_batch (the extra Poseidon-10 per leaf
# is small relative to the dual Merkle walk that dominates).
render_mint_batch_a2_n() {
    local N="$1"
    local SRC="$ROOT/circuits/mint_batch_a2.circom"
    local DST="$ROOT/circuits/mint_batch_a2_n${N}.circom"
    sed -e "s|= MintBatchA2(16, 20);|= MintBatchA2(${N}, 20);|" \
        "$SRC" > "$DST"
}

# Section guards let `make snark-a2` rebuild only the A2 family (reusing the
# existing ptau) without re-running the legacy circuits or the base mint_batch.
DO_LEGACY="${DO_LEGACY:-1}"
DO_MINT_BATCH="${DO_MINT_BATCH:-1}"
MINT_BATCH_A2_PINS="${MINT_BATCH_A2_PINS:-}"

# ---- legacy mint + spend (pot15) -----------------------------------------

if [ "$DO_LEGACY" = "1" ]; then
    PTAU15="$(ensure_ptau 15)"
    setup_circuit mint    MintGroth16Verifier   "$PTAU15"
    setup_circuit spend   SpendGroth16Verifier  "$PTAU15"
fi

# ---- mint_batch per-N (Phase 7-bis pivot) --------------------------------

if [ "$DO_MINT_BATCH" = "1" ]; then
    MINT_BATCH_PINS="${MINT_BATCH_PINS:-16}"
    for N in $MINT_BATCH_PINS; do
        POW="$(ptau_pow_for_n "$N")"
        PTAU="$(ensure_ptau "$POW")"
        render_mint_batch_n "$N"
        setup_circuit \
            "mint_batch_n${N}" \
            "MintBatchN${N}Groth16Verifier" \
            "$PTAU" \
            "mint_batch_n${N}"
    done
fi

# ---- mint_batch_a2 per-N (private-issuer eIss leaf-tie) -------------------
# Opt-in via MINT_BATCH_A2_PINS (empty by default, so a plain `make snark` does
# not rebuild the A2 family).  `make snark-a2` sets it and disables the blocks
# above.  Reuses the mint_batch ptau (same ptau_pow_for_n mapping).

for N in $MINT_BATCH_A2_PINS; do
    POW="$(ptau_pow_for_n "$N")"
    PTAU="$(ensure_ptau "$POW")"
    render_mint_batch_a2_n "$N"
    setup_circuit \
        "mint_batch_a2_n${N}" \
        "MintBatchA2N${N}Groth16Verifier" \
        "$PTAU" \
        "mint_batch_a2_n${N}"
done

echo "setup complete"
