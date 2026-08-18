#!/bin/bash
# Verifier regression test — regenerates artifacts from scratch, then tests
# both freshly-generated and pre-existing (known-working) verifiers on forge.
#
# Run:  make snark-test-regression
#   or  nix develop --command bash scripts/snark/test_verifier_regression.sh
#
# Exits 0 if all tests pass, 1 if any fail.
# See alberta-buck-verifier-bug.org for context.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SNARKJS="$ROOT/node_modules/.bin/snarkjs"
BUILD="$ROOT/build/snark/regression"
VECTORS="$ROOT/test/vectors/regression"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass_count=0
fail_count=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; pass_count=$((pass_count + 1)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; fail_count=$((fail_count + 1)); }

# ---- Step 1: Atomic pipeline for identity_membership -------------------------

echo "=== [1/5] Fresh atomic pipeline: identity_membership ==="
rm -rf "$BUILD" "$VECTORS"
mkdir -p "$BUILD" "$VECTORS"

circom "$ROOT/circuits/identity_membership.circom" --r1cs --wasm --output "$BUILD" 2>&1 | tail -1

PYTHONPATH="$ROOT" python3 -c "
import json
from alberta_buck.wallet.bn254 import G1, mul, point_to_words
from alberta_buck.registry.tree import IdentityMerkleTree
from alberta_buck.wallet.poseidon import F_R
M=mul(G1,12345); Mx,My=point_to_words(M)
t=IdentityMerkleTree(depth=10); t.insert_identity(M); p=t.path(0)
w={\"identityRoot\":str(p.root),\"Mx\":str(Mx%F_R),\"My\":str(My%F_R),\"pathElements\":[str(s)for s in p.siblings],\"pathIndices\":[str(b)for b in p.index_bits]}
with open(\"$BUILD/input.json\",\"w\")as f:json.dump(w,f)
" 2>/dev/null
echo "  Circuit compiled + witness generated"

# groth16 setup (non-deterministic delta — but full pipeline is atomic)
"$SNARKJS" g16s "$BUILD/identity_membership.r1cs" "$ROOT/build/snark/ptau/pot13_final.ptau" "$BUILD/z.zkey" -v 2>/dev/null
echo "fixed-entropy" | "$SNARKJS" zkc "$BUILD/z.zkey" "$BUILD/z1.zkey" --name=regression -v 2>/dev/null
"$SNARKJS" zkev "$BUILD/z1.zkey" "$BUILD/vk.json" 2>/dev/null

# Prove (fullprove = witness + prove in one step)
"$SNARKJS" g16f "$BUILD/input.json" "$BUILD/identity_membership_js/identity_membership.wasm" \
    "$BUILD/z1.zkey" "$BUILD/proof.json" "$BUILD/public.json" 2>/dev/null

# Verify off-chain
if "$SNARKJS" g16v "$BUILD/vk.json" "$BUILD/public.json" "$BUILD/proof.json" 2>/dev/null; then
    pass "off-chain snarkjs verify"
else
    fail "off-chain snarkjs verify"
fi

# Export Solidity verifier (rename to avoid collision with source-tree verifier)
"$SNARKJS" zkesv "$BUILD/z1.zkey" "$BUILD/RegressVerifier.sol" 2>/dev/null
# The verifier stays STOCK; the EIP-197 pi_b swap is applied at vector
# packing below (matching `snarkjs zkey export soliditycalldata`).
perl -i -pe 's/contract Groth16Verifier/contract RegressVerifier/g; s/public view returns/public returns/g' "$BUILD/RegressVerifier.sol"
echo "  Solidity verifier exported"

# ---- Step 2: Generate Forge test vectors ------------------------------------

echo "=== [2/5] Generate Forge test vectors ==="
PYTHONPATH="$ROOT" python3 -c "
import json, os, re
with open('$BUILD/proof.json') as f: p=json.load(f)
with open('$BUILD/public.json') as f: pub=json.load(f)
v={'a':[str(p['pi_a'][0]),str(p['pi_a'][1])],
   'b':[str(p['pi_b'][0][1]),str(p['pi_b'][0][0]),str(p['pi_b'][1][1]),str(p['pi_b'][1][0])],
   'c':[str(p['pi_c'][0]),str(p['pi_c'][1])],'pub':[str(x) for x in pub]}
os.makedirs('$VECTORS',exist_ok=True)
with open('$VECTORS/proof.json','w')as f:json.dump(v,f,indent=2)
print(f'  Vectors written: {len(pub)} public signals')
"

# ---- Step 3: Build Forge test contract --------------------------------------

echo "=== [3/5] Build Forge test harness ==="
cat > "$ROOT/test/RegressionTest.sol" << 'TEOF'
// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";

// Freshly-generated verifier (from this script run)
import {RegressVerifier} from "../build/snark/regression/RegressVerifier.sol";

// Pre-existing known-working verifier
import {IdentityMembershipVerifier} from "../src/IdentityMembershipVerifier.sol";

contract RegressionTest is Test {
    // ---- Test A: Freshly-generated verifier + proof ------------------------

    function test_freshVerifier_acceptsFreshProof() public {
        string memory vj = vm.readFile("test/vectors/regression/proof.json");
        uint256[] memory av = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory bv = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory cv = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pv = vm.parseJsonUintArray(vj, ".pub");
        assertTrue(
            new RegressVerifier().verifyProof(
                [av[0], av[1]],
                [[bv[0], bv[1]], [bv[2], bv[3]]],
                [cv[0], cv[1]],
                [pv[0]]
            ),
            "fresh verifier must accept fresh proof"
        );
    }

    function test_freshVerifier_rejectsTamperedRoot() public {
        string memory vj = vm.readFile("test/vectors/regression/proof.json");
        uint256[] memory av = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory bv = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory cv = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pv = vm.parseJsonUintArray(vj, ".pub");
        pv[0] ^= 1; // flip identityRoot
        assertFalse(
            new RegressVerifier().verifyProof(
                [av[0], av[1]],
                [[bv[0], bv[1]], [bv[2], bv[3]]],
                [cv[0], cv[1]],
                [pv[0]]
            ),
            "fresh verifier must reject tampered root"
        );
    }

    // ---- Test B: Pre-existing known-working verifier -----------------------

    function test_knownVerifier_acceptsKnownProof() public {
        string memory vj = vm.readFile("test/vectors/identity_membership.json");
        uint256[] memory av = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory bv = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory cv = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pv = vm.parseJsonUintArray(vj, ".pub");
        assertTrue(
            new IdentityMembershipVerifier().verifyProof(
                [av[0], av[1]],
                [[bv[0], bv[1]], [bv[2], bv[3]]],
                [cv[0], cv[1]],
                [pv[0]]
            ),
            "known-working verifier must accept known proof"
        );
    }
}
TEOF
echo "  Test harness written to test/RegressionTest.sol"

# ---- Step 4: Run Forge tests ------------------------------------------------

echo "=== [4/5] Run Forge tests ==="
export SOLC_PATH="${SOLC_PATH:-$(which solc)}"

rm -rf "$ROOT/out"

# Helper: run a forge test and report pass/fail
run_forge_test() {
    local test_name="$1"
    if forge test --skip 'src/uniswap_v2_build/**' --skip 'src/uniswap_v3_build/**' \
        --match-test "$test_name" -vvv 2>&1 | grep -q "Suite result: ok"; then
        pass "$test_name"
    else
        fail "$test_name"
    fi
}

run_forge_test "test_freshVerifier_acceptsFreshProof"
run_forge_test "test_freshVerifier_rejectsTamperedRoot"
run_forge_test "test_knownVerifier_acceptsKnownProof"

# ---- Step 5: Summary --------------------------------------------------------

echo "=== [5/5] Summary ==="
echo "  Passed: $pass_count"
echo "  Failed: $fail_count"

if [ "$fail_count" -eq 0 ]; then
    echo -e "  ${GREEN}ALL TESTS PASSED — on-chain verifiers work on this architecture${NC}"
    exit 0
else
    echo -e "  ${RED}FAILURES DETECTED — on-chain verifier regression on this architecture${NC}"
    if [ "$pass_count" -ge 1 ]; then
        echo "  Known-working verifier: $( [ $pass_count -ge 3 ] && echo 'PASS' || echo 'FAIL' )"
        echo "  Fresh verifier:        $( [ $pass_count -ge 1 ] && echo 'CHECK' || echo 'FAIL' )"
    fi
    exit 1
fi
