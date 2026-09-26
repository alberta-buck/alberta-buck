#!/bin/bash
# Generate the privacy paper's fixture world (alberta_buck/test/vectors/privacy/world.json):
# one chain, one identity tree, and three Notes -- Aspen Mutual's B1 and A1 batches of
# four and Bob's A2 cheque -- with REAL proofs at every gate.  See gen_privacy_world.py.
#
# Prerequisites (built by the existing setups):
#   build/snark/mint_batch_n4     -- scripts/snark/setup.sh (MINT_BATCH_PINS includes 4)
#   build/snark/mint_batch_a2_n1  -- setup.sh (MINT_BATCH_A2_PINS includes 1)
#   build/snark/spend             -- setup.sh
#   build/snark/deposit_fold_a1   -- make snark-deposit-fold-a1
#   build/snark/deposit_fold_a2   -- make snark-deposit-fold-a2
#   build/snark/b1_membership     -- make snark-b1-membership
#
# Usage:
#   make nix-snark-privacy-fixtures

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT="$ROOT/build/snark/privacy"
B1M="$ROOT/build/snark/b1_membership"
RAPIDSNARK="$ROOT/lib/rapidsnark-macOS-arm64-v0.0.8/bin/prover"
CHAINID=1

now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
field() { python3 -c "import json,sys; print(json.load(open('$OUT/world.json'))$1)"; }

cd "$ROOT"
echo "=== privacy world ==="
PYTHONPATH="$ROOT" python "$SCRIPT_DIR/gen_privacy_world.py" world

: > "$OUT/timings.raw"
for FLAVOR in b1 a1 a2; do
    echo "=== $FLAVOR: mint ==="
    ARGS=$(field "['notes']['$FLAVOR']['mintArgs']" | python3 -c "import ast,sys; print(' '.join(ast.literal_eval(sys.stdin.read())))")
    T0=$(now)
    if [ "$FLAVOR" = "a2" ]; then
        node "$SCRIPT_DIR/prove_mint_batch_a2.js" $ARGS
        MINT_FIX="build/snark/mint_batch_a2_n1/fixtures/privacy_a2.json"
    else
        node "$SCRIPT_DIR/prove_mint_batch.js" $ARGS
        MINT_FIX="build/snark/mint_batch_n4/fixtures/privacy_${FLAVOR}.json"
    fi
    T1=$(now)

    echo "=== $FLAVOR: spend ==="
    LEAF=$(field "['notes']['$FLAVOR']['leafIndex']")
    PAYOUT=$(field "['notes']['$FLAVOR']['payoutAddr']")
    T2=$(now)
    node "$SCRIPT_DIR/prove_spend.js" "$MINT_FIX" "$LEAF" "$PAYOUT" "$CHAINID" \
        "privacy_${FLAVOR}" "build/snark/privacy/${FLAVOR}_leaves.json"
    T3=$(now)

    echo "=== $FLAVOR: deposit gate ==="
    T4=$(now)
    if [ "$FLAVOR" = "b1" ]; then
        npx snarkjs wtns calculate \
            "$B1M/identity_membership_b1_js/identity_membership_b1.wasm" \
            "$OUT/b1_membership_input.json" "$OUT/b1_membership_witness.wtns"
        npx snarkjs groth16 prove "$B1M/b1m_0001.zkey" \
            "$OUT/b1_membership_witness.wtns" \
            "$OUT/b1_membership_proof.json" "$OUT/b1_membership_public.json"
        T5=$(now)
        npx snarkjs groth16 verify "$B1M/verification_key.json" \
            "$OUT/b1_membership_public.json" "$OUT/b1_membership_proof.json"
    else
        FOLD="$ROOT/build/snark/deposit_fold_$FLAVOR"
        bash -c "ulimit -s 65520 && '$FOLD/deposit_fold_${FLAVOR}_cpp/deposit_fold_$FLAVOR' \
            '$OUT/${FLAVOR}_fold_input.json' '$OUT/${FLAVOR}_fold_witness.wtns'"
        "$RAPIDSNARK" "$FOLD/deposit_fold_${FLAVOR}_0001.zkey" \
            "$OUT/${FLAVOR}_fold_witness.wtns" \
            "$OUT/${FLAVOR}_fold_proof.json" "$OUT/${FLAVOR}_fold_public.json"
        T5=$(now)
        npx snarkjs groth16 verify "$FOLD/verification_key.json" \
            "$OUT/${FLAVOR}_fold_public.json" "$OUT/${FLAVOR}_fold_proof.json"
    fi
    echo "$FLAVOR $T0 $T1 $T2 $T3 $T4 $T5" >> "$OUT/timings.raw"
done

# Prover wall times, merged into each note by `assemble`.
python3 - "$OUT" <<'EOF'
import json, sys
out = sys.argv[1]
timings = {}
for line in open(f"{out}/timings.raw"):
    flavor, *t = line.split()
    t = [float(x) for x in t]
    timings[flavor] = {
        "mint_prove_s":         round(t[1] - t[0], 3),
        "spend_prove_s":        round(t[3] - t[2], 3),
        "deposit_gate_prove_s": round(t[5] - t[4], 3),
    }
with open(f"{out}/timings.json", "w") as f:
    json.dump(timings, f, indent=2)
print(f"[timings] {timings}")
EOF

PYTHONPATH="$ROOT" python "$SCRIPT_DIR/gen_privacy_world.py" assemble
echo "=== privacy fixtures complete: alberta_buck/test/vectors/privacy/world.json ==="
