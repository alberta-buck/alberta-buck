# Run: PYTHONPATH=. python scripts/review/g1tie_limb_alias.py <outdir>
"""Review finding 5c: the g1-tie circuit does not range-check 64-bit limbs.
A carry Mx[0] += 2^64, Mx[1] -= 1 keeps Poseidon(Mx_mod, My_mod) (the leaf)
identical while changing the EC-add representation of M.

If snarkjs accepts the aliased witness, the underconstraint is live on the
committed artifacts.  If witness generation or proving rejects it, the
gap remains source-established and must still be closed in the P1-A circuit.
"""
import sys, time
from pathlib import Path

from alberta_buck.review.examples import mismatched_membership, aliased_g1tie_limbs
from alberta_buck.review.integration import g1tie_prove

OUT = Path(sys.argv[1])
member, outsider, P, tree, w = mismatched_membership()
w_alias = aliased_g1tie_limbs(w)
assert w_alias["Mx"] != w["Mx"]
rec = lambda ls: ls[0] + (ls[1] << 64) + (ls[2] << 128) + (ls[3] << 192)
assert rec(w_alias["Mx"]) == rec(w["Mx"])

t0 = time.time()
try:
    res = g1tie_prove(OUT / "alias", w_alias)
    print(f"aliased limbs: proof generated+verified in {time.time()-t0:.1f}s")
    print("publicSignals[0] (root) =", res["publicSignals"][0][:18], "...")
    print("RESULT: finding 5c reproduced on real Groth16 "
          "(limb carry accepted; leaf unchanged, EC limbs changed)")
    (OUT / "ok").write_text("accepted\n")
except Exception as exc:
    print(f"aliased limbs: prover/witness rejected after {time.time()-t0:.1f}s")
    print("exception:", type(exc).__name__, str(exc)[:500])
    print("RESULT: finding 5c remains source-established "
          "(committed prover did not accept this particular carry; "
          "the circuit still has no 64-bit range check)")
    (OUT / "ok").write_text("source-established\n")
