# Run: PYTHONPATH=. python scripts/review/g1tie_membership_mismatch.py  -- review evidence; see doc/review/identity-findings.md
"""Review finding 5, executed: does the REAL identity_membership_g1tie circuit
(build/snark/g1tie, matched wasm + zkey) accept a witness T = P - M where P
was opened by a DIFFERENT identity than the tree member M?

Honest control first, then the mismatched witness.  Uses only the preserved
review helpers and existing artifacts; writes only to the scratchpad.
"""
import json, sys, time
from pathlib import Path

from alberta_buck.review.examples import mismatched_membership, membership_input
from alberta_buck.review.integration import g1tie_prove, REPO
from alberta_buck.wallet.bn254 import G1, add, mul
from alberta_buck.wallet.issuer_reenc import H_POINT
from alberta_buck.registry.tree import IdentityMerkleTree

OUT = Path(sys.argv[1])

# --- honest control: P = M + b*H, T = b*H, M in the tree -------------------
member = mul(G1, 12345)
b = 22222
P_honest = add(member, mul(H_POINT, b))
tree = IdentityMerkleTree(depth=10)
tree.insert_identity(member)
w_honest = membership_input(tree, 0, member, P_honest)
t0 = time.time()
res = g1tie_prove(OUT / "honest", w_honest)
print(f"honest   : proof generated+verified by snarkjs in {time.time()-t0:.1f}s; "
      f"publicSignals[0]={res['publicSignals'][0][:12]}... (root)")

# --- mismatched: P = outsider + 22222*H, T = P - member ---------------------
member2, outsider, P_bad, tree2, w_bad = mismatched_membership()
assert outsider != member2
t0 = time.time()
res2 = g1tie_prove(OUT / "mismatch", w_bad)
print(f"mismatch : proof generated+verified by snarkjs in {time.time()-t0:.1f}s")
# public inputs: [identityRoot, PI_x[0..3], PI_y[0..3]]
ps = res2["publicSignals"]
from alberta_buck.wallet.bn254 import point_to_words
px, py = point_to_words(P_bad)
limbs = lambda n: [str((n >> (64*i)) & ((1 << 64)-1)) for i in range(4)]
assert ps[0] == str(tree2.root()), "root public input mismatch"
assert ps[1:5] == limbs(px) and ps[5:9] == limbs(py), "P public input mismatch"
print("mismatch : public inputs = (root containing M_member, P opened by OUTSIDER)")
print("RESULT   : circuit accepted a membership proof for P whose identity is NOT in the tree")
open(OUT / "ok","w").write("done")
