#!/bin/bash
# rapidsnark diagnostic: native C++ prove/verify vs snarkjs JS/WASM prove/verify.
# Isolates whether the JS/WASM layer (ffjavascript/wasmcurves) is the root cause.
# Usage: nix develop --command bash scripts/snark/test_rapidsnark.sh
set -euo pipefail
R="$(cd "$(dirname "$0")/../.." && pwd)"; S="$R/node_modules/.bin/snarkjs"
RP=~/src/rapidsnark/package_macos_arm64/bin; D=/tmp/rp-test

echo "=== rapidsnark diagnostic ==="
rm -rf "$D"; mkdir -p "$D"

# 1. snarkjs compile + setup + export verifier
circom "$R/circuits/identity_membership.circom" --r1cs --wasm --output "$D" 2>&1 | tail -1
PYTHONPATH="$R" python3 -c "
import json, sys
sys.path.insert(0,'$R')
from alberta_buck.wallet.bn254 import G1,mul,point_to_words
from alberta_buck.registry.tree import AGGREGATOR_DEPTH, IdentityMerkleTree
from alberta_buck.wallet.poseidon import F_R
M=mul(G1,12345); Mx,My=point_to_words(M); salt=67890
t=IdentityMerkleTree(depth=AGGREGATOR_DEPTH,private=True); t.insert_identity_salted(M,salt); p=t.path(0)
w={'identityRoot':str(p.root),'Mx':str(Mx%F_R),'My':str(My%F_R),'salt':str(salt),'pathElements':[str(s)for s in p.siblings],'pathIndices':[str(b)for b in p.index_bits]}
with open('$D/input.json','w')as f:json.dump(w,f)
"
node "$D/identity_membership_js/generate_witness.js" "$D/identity_membership_js/identity_membership.wasm" "$D/input.json" "$D/witness.wtns" 2>/dev/null
"$S" g16s "$D/identity_membership.r1cs" "$R/build/snark/ptau/pot15_final.ptau" "$D/z.zkey" -v 2>/dev/null
echo fixed | "$S" zkc "$D/z.zkey" "$D/z1.zkey" --name=t -v 2>/dev/null
"$S" zkev "$D/z1.zkey" "$D/vk.json" 2>/dev/null
"$S" zkesv "$D/z1.zkey" "$D/V.sol" 2>/dev/null
perl -i -pe 's/contract Groth16Verifier/contract TestV/g; s/public view returns/public returns/g' "$D/V.sol"
echo "snarkjs setup complete"

# 2. rapidsnark prove + verify (C++ native)
echo ""; echo "--- rapidsnark (C++) ---"
"$RP/prover" "$D/z1.zkey" "$D/witness.wtns" "$D/proof_rs.json" "$D/public_rs.json" 2>&1
"$RP/verifier" "$D/vk.json" "$D/public_rs.json" "$D/proof_rs.json" 2>&1

# 3. snarkjs prove + verify (JS/WASM)
echo ""; echo "--- snarkjs (JS/WASM) ---"
"$S" g16p "$D/z1.zkey" "$D/witness.wtns" "$D/proof_js.json" "$D/public_js.json" 2>/dev/null
"$S" g16v "$D/vk.json" "$D/public_js.json" "$D/proof_js.json" 2>/dev/null && echo "OK" || echo "FAIL"

# 4. Compare
echo ""; echo "--- Comparison ---"
python3 -c "
import json
rs=json.load(open('$D/proof_rs.json')); js=json.load(open('$D/proof_js.json'))
m=rs['pi_a']==js['pi_a'] and rs['pi_b']==js['pi_b'] and rs['pi_c']==js['pi_c']
print(f'Proofs identical: {m}')
prs=json.load(open('$D/public_rs.json')); pjs=json.load(open('$D/public_js.json'))
print(f'Public signals identical: {prs==pjs}')
"

# 5. On-chain test with rapidsnark proof
echo ""; echo "--- On-chain test (rapidsnark proof) ---"
cp "$D/V.sol" "$R/src/TestV.sol"
python3 -c "
import json, os
p=json.load(open('$D/proof_rs.json')); pub=json.load(open('$D/public_rs.json'))
v={'a':[str(p['pi_a'][0]),str(p['pi_a'][1])],'b':[str(p['pi_b'][0][0]),str(p['pi_b'][0][1]),str(p['pi_b'][1][0]),str(p['pi_b'][1][1])],'c':[str(p['pi_c'][0]),str(p['pi_c'][1])],'pub':[str(x) for x in pub]}
os.makedirs('$R/test/vectors/regen',exist_ok=True)
json.dump(v,open('$R/test/vectors/regen/proof.json','w'),indent=2)
"
export SOLC_PATH="${SOLC_PATH:-$(which solc)}"
forge test --skip 'src/uniswap_v2_build/**' --skip 'src/uniswap_v3_build/**' \
    --match-contract TestVTest -vvv 2>&1 | grep -E "Suite result|FAIL|PASS"
