#!/usr/bin/env bash
# Regenerate the mint_batch_a2 (private-issuer A2) fixtures the forge tests load:
#   - basic    : self-contained parity fixtures (arbitrary eIss) for
#                test/MintVerifierA2.t.sol (cross-artifact: the proof verifies
#                on chain; a tampered eIss fails the pairing check).
#   - tie      : N=1 leaf whose eIss and T are pinned to the canonical
#                issuer_reenc binding (test/vectors/identity.json), so test/NotesA2Tie.t.sol
#                can drive the full Notes.mint A2 path with a REAL binding.
#   - tie_dup  : N=2 with leaf 0 pinned to the same binding and leaf 1 an
#                independent eIss -- the duplicate-binding collusion regression
#                (minting with [binding0, binding0] must fail the leaf-tie).
#
# The per-N A2 circuits + zkeys must exist (make snark-a2-setup, or setup.sh
# with MINT_BATCH_A2_PINS covering 1 2).
#
# Usage: scripts/snark/gen_mint_fixtures_a2.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROVE="$ROOT/scripts/snark/prove_mint_batch_a2.js"

# The canonical A2 binding's E_iss = (R.x, R.y, C.x, C.y) and T = (x, y), read at
# runtime so the fixture stays in lock-step with test/vectors/identity.json if it
# regenerates.
EISS0="$(node -e '
  const j = require("'"$ROOT"'/test/vectors/identity.json");
  const e = j.issuer_reenc.E_iss;
  console.log([e.R.x, e.R.y, e.C.x, e.C.y].join(","));
')"
T0="$(node -e '
  const j = require("'"$ROOT"'/test/vectors/identity.json");
  const t = j.issuer_reenc.proof.T;
  console.log([t.x, t.y].join(","));
')"

gen() { echo ">>> mint_batch_a2 fixture: $*"; node "$PROVE" "$@"; }

# Self-contained parity fixtures (arbitrary eIss).
gen --name=basic --n=1
gen --name=basic --n=2

# Leaf-tie fixtures: leaf 0's eIss and T == the canonical issuer_reenc binding's.
gen --name=tie     --n=1 --eiss="0:${EISS0}" --t="0:${T0}"
gen --name=tie_dup --n=2 --eiss="0:${EISS0}" --t="0:${T0}"

# The N=32 verifier is table-rewritten for EIP-170 (scripts/snark/table_verifier.py).
# Its proof vector, committed, is what test/VerifierTable.t.sol holds the rewrite
# and its stock original to -- the public signals in the circuit's order.
if [ -d "$ROOT/build/snark/mint_batch_a2_n32" ]; then
    gen --name=table --n=32
    python3 - "$ROOT" <<'EOF'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
f = json.loads((root / "build/snark/mint_batch_a2_n32/fixtures/table.json").read_text())
p = f["public"]
signals = ([w for leaf in p["eIss"] for w in leaf] + [w for leaf in p["T"] for w in leaf]
           + [p["oldRoot"], p["newRoot"], p["nextLeafIndex"], p["totalFace"]] + p["cm"])
assert len(signals) == 7 * f["N"] + 4
out = root / "test/vectors/mint_batch_a2_n32/proof.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"pA": f["proof"]["pA"], "pB": f["proof"]["pB"], "pC": f["proof"]["pC"],
                           "publicSignals": signals}, indent=1) + "\n")
print(f"wrote {out.relative_to(root)} ({len(signals)} public signals)")
EOF
fi

echo "all mint_batch_a2 fixtures regenerated"
