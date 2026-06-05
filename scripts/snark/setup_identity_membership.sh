#!/usr/bin/env bash
# Full Groth16 wiring for circuits/identity_membership.circom: proving key
# (reusing the dev pot13 ptau), exported Solidity verifier, and a real proof of a
# genuine member generated from the Python IdentityTree -- verified by snarkjs.
# Emits the proof + public signal as a vector for the Foundry parity test.
#
# Run:  nix develop --command bash scripts/snark/setup_identity_membership.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/build/snark/identity_membership"
PTAU="$ROOT/build/snark/ptau/pot13_final.ptau"
NAME="identity_membership"
mkdir -p "$OUT" "$ROOT/test/vectors"

# (Re)compile if needed.
if [ ! -f "$OUT/${NAME}.r1cs" ]; then
    ( cd "$ROOT/circuits" && \
      circom ${NAME}.circom --r1cs --wasm --sym --output "$OUT" -l "$ROOT/node_modules" )
fi

echo "[1/6] groth16 setup (reusing $(basename "$PTAU"))"
snarkjs g16s "$OUT/${NAME}.r1cs" "$PTAU" "$OUT/${NAME}_0000.zkey" -v >/dev/null
echo "[2/6] contribute (dev entropy)"
echo "alberta-buck-dev-zkey-${NAME}" | \
    snarkjs zkc "$OUT/${NAME}_0000.zkey" "$OUT/${NAME}_final.zkey" --name="dev" -v >/dev/null
echo "[3/6] export verification key + Solidity verifier"
snarkjs zkev "$OUT/${NAME}_final.zkey" "$OUT/${NAME}_vkey.json" >/dev/null
snarkjs zkesv "$OUT/${NAME}_final.zkey" "$ROOT/src/IdentityMembershipVerifier.sol" >/dev/null
# Rename the default contract to avoid the generic `Groth16Verifier` collision.
sed -i.bak 's/contract Groth16Verifier {/contract IdentityMembershipVerifier {/' \
    "$ROOT/src/IdentityMembershipVerifier.sol" && rm -f "$ROOT/src/IdentityMembershipVerifier.sol.bak"

echo "[4/6] generate inputs + witness (a genuine member from the Python tree)"
python "$ROOT/scripts/snark/gen_identity_membership_input.py" "$OUT"
node "$OUT/${NAME}_js/generate_witness.js" \
     "$OUT/${NAME}_js/${NAME}.wasm" "$OUT/input.json" "$OUT/witness.wtns"

echo "[5/6] prove + verify"
snarkjs g16p "$OUT/${NAME}_final.zkey" "$OUT/witness.wtns" "$OUT/proof.json" "$OUT/public.json"
snarkjs g16v "$OUT/${NAME}_vkey.json" "$OUT/public.json" "$OUT/proof.json"

echo "[6/6] emit Solidity calldata vector"
snarkjs generatecall "$OUT/public.json" "$OUT/proof.json" > "$OUT/calldata.txt"
python - "$OUT/calldata.txt" "$ROOT/test/vectors/identity_membership.json" <<'PY'
import json, re, sys
raw = open(sys.argv[1]).read().strip()
# snarkjs generatecall prints: [a0,a1],[[b00,b01],[b10,b11]],[c0,c1],[pub0,...]
nums = re.findall(r'"(0x[0-9a-fA-F]+)"', raw)
a = nums[0:2]; b = nums[2:6]; c = nums[6:8]; pub = nums[8:]
out = {"a": a, "b": b, "c": c, "pub": pub}
json.dump(out, open(sys.argv[2], "w"), indent=2)
print("wrote", sys.argv[2], "pub signals:", len(pub))
PY

echo "PASS: identity_membership Groth16 proof verifies; verifier at src/IdentityMembershipVerifier.sol"
