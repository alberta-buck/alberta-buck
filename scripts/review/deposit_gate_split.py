# Run: PYTHONPATH=.:core/python python scripts/review/deposit_gate_split.py
"""Why the receiving-key change forces the finding-5 fold.

The deposit-coupling sigma with the receiving key split out.

Today one witness m_rec does two jobs: it decrypts the note's eIss, and it is
the identity inside the deposit account's registered credential.  The shared
Fiat-Shamir nonce across those two relations IS the tie between "I can read
this note" and "I am this registered identity".

With an independent receiving key the two jobs belong to two scalars, so the
sigma proves them separately and the tie moves to the accumulator leaf, which
commits the pair.  This checks that the split sigma is sound and complete, and
that the leaf carries the tie.
"""
from alberta_buck.wallet.bn254 import G1, ORDER, mul, add, neg, eq, rand_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.review.known_log import H_KNOWN as H
from alberta_buck.wallet.poseidon import F_R, poseidon
from alberta_buck.wallet.bn254 import point_to_words
from alberta_buck.wallet.transcript import keccak_scalar
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.registry.tree import IdentityMerkleTree
import random
ok = lambda b, s: print(("PASS" if b else "FAIL") + "  " + s)
_s = random.Random(0xC0FFEE); rng = lambda: _s.getrandbits(256)

def tr(*pts, **kw):
    w = []
    for P in pts: w += list(point_to_words(P))
    return keccak_scalar(*w, *kw.values())

def prove(m_rec, k, sk, b, E_dep, eIss):
    """Witnesses (m_rec, k, sk, b).  Four relations, three of them independent."""
    R_d, C_d = E_dep.R, E_dep.C
    R_e, C_e = eIss.R, eIss.C
    M_I  = add(C_e, neg(mul(R_e, k)))            # decrypt with k, not m_rec
    P_I  = add(M_I, mul(H, b))                   # hide it
    pk   = mul(G1, sk)
    n_m, n_k, n_sk, n_b = (rand_scalar(rng) for _ in range(4))
    A4 = mul(G1, n_sk)                                       # (E4) key ownership
    A2 = add(mul(G1, n_m), mul(R_d, n_sk))                   # (E2) credential holds m_rec
    A3 = add(mul(R_e, n_k), neg(mul(H, n_b)))                # (E3) k decrypts to P_I
    e  = tr(pk, R_d, C_d, R_e, C_e, P_I, A2, A3, A4)
    return dict(P_I=P_I, A2=A2, A3=A3, A4=A4, e=e,
                s_m=(n_m + e*m_rec) % ORDER, s_k=(n_k + e*k) % ORDER,
                s_sk=(n_sk + e*sk) % ORDER, s_b=(n_b + e*b) % ORDER)

def verify(pk, E_dep, eIss, p):
    R_d, C_d = E_dep.R, E_dep.C
    R_e, C_e = eIss.R, eIss.C
    if p["e"] != tr(pk, R_d, C_d, R_e, C_e, p["P_I"], p["A2"], p["A3"], p["A4"]):
        return False
    e = p["e"]
    if not eq(mul(G1, p["s_sk"]), add(p["A4"], mul(pk, e))): return False
    if not eq(add(mul(G1, p["s_m"]), mul(R_d, p["s_sk"])),
              add(p["A2"], mul(C_d, e))): return False
    lhs = add(mul(R_e, p["s_k"]), neg(mul(H, p["s_b"])))
    rhs = add(p["A3"], mul(add(C_e, neg(p["P_I"])), e))
    return eq(lhs, rhs)

# The cast.
m_rec = rand_scalar(rng); M_rec = mul(G1, m_rec)
k = rand_scalar(rng);     pk_recv = mul(G1, k)
sk = rand_scalar(rng);    pk = mul(G1, sk)
m_iss = rand_scalar(rng); M_I = mul(G1, m_iss)
E_dep = elgamal_encrypt(M_rec, pk, rand_scalar(rng))          # registered credential
eIss  = elgamal_encrypt(M_I, pk_recv, rand_scalar(rng))       # note, addressed to pk_recv
b = rand_scalar(rng)

p = prove(m_rec, k, sk, b, E_dep, eIss)
ok(verify(pk, E_dep, eIss, p), "split sigma: honest proof verifies")
ok(eq(add(p["P_I"], neg(mul(H, b))), M_I), "split sigma: P_I commits the issuer identity")

# Each relation is load-bearing: break one witness at a time.
# A wrong Identity or account key breaks a relation.  A wrong RECEIVING key does
# not: the sigma says only "P commits whatever this scalar decrypts to", and any
# scalar decrypts to something.  That is the hole the next block exploits.
for name, kw in (("wrong identity", dict(m_rec=rand_scalar(rng))),
                 ("wrong account key", dict(sk=rand_scalar(rng)))):
    bad = dict(m_rec=m_rec, k=k, sk=sk, b=b); bad.update(kw)
    q = prove(bad["m_rec"], bad["k"], bad["sk"], bad["b"], E_dep, eIss)
    ok(not verify(pk, E_dep, eIss, q), f"split sigma: {name} is refused")

# What the sigma no longer says, and what carries it instead.
q = prove(m_rec, k, sk, b, E_dep, eIss)
k_other = rand_scalar(rng)
eIss_other = elgamal_encrypt(M_I, mul(G1, k_other), rand_scalar(rng))
q2 = prove(m_rec, k_other, sk, b, E_dep, eIss_other)
ok(verify(pk, E_dep, eIss_other, q2),
   "split sigma: ANY receiving key passes -- the tie is no longer inside it")

def leaf(M, pk_r, salt):
    mx, my = point_to_words(M); kx, ky = point_to_words(pk_r)
    return poseidon([mx % F_R, my % F_R, kx % F_R, ky % F_R, salt])

secret = rand_scalar(rng); salt = derive_salt(secret, "kyc:ca-ab-2026")
tree = IdentityMerkleTree(depth=10, private=True)
idx = tree.insert_leaf(leaf(M_rec, pk_recv, salt))
ok(tree.path(idx).verify(), "leaf: the registered (identity, receiving key) pair is a member")
ok(not tree.contains(leaf(M_rec, mul(G1, k_other), salt)),
   "leaf: an unregistered receiving key for the same identity is NOT a member")
print("\n  the membership statement therefore grows: it must certify the PAIR,")
print("  not just the point the sigma commits.")

# ---------------------------------------------------------------------------
# The consequence: what the shared nonce was actually buying.
#
# Today ONE witness m_rec satisfies both E2 (my account credential holds this
# identity) and E3 (this scalar decrypts the note).  A payload thief with their
# own registered identity fails E2, because their account is not bound to
# m_rec.  Split the two and nothing inside the sigma connects them.
print()
m_thief = rand_scalar(rng); M_thief = mul(G1, m_thief)
sk_t = rand_scalar(rng); pk_t = mul(G1, sk_t)
E_thief = elgamal_encrypt(M_thief, pk_t, rand_scalar(rng))     # the thief's OWN account
tree.insert_leaf(leaf(M_thief, mul(G1, rand_scalar(rng)), derive_salt(rand_scalar(rng), "kyc:ca-ab-2026")))

# The thief holds a stolen payload, so it holds k.  It proves E3 with k and E2
# with its own identity.
theft = prove(m_thief, k, sk_t, rand_scalar(rng), E_thief, eIss)
stolen = verify(pk_t, E_thief, eIss, theft)
registered = tree.contains(leaf(M_thief, mul(G1, k), salt))
ok(stolen, "THEFT: the split sigma accepts a thief's own identity with the stolen key")
ok(not registered, "and the leaf does not contain (thief, stolen key) -- the tie that must be proven")
print("  -> the tie m_rec <-> k must be re-established INSIDE one proof.")
print("     Finding 5's lesson applies: do not infer equality from two proofs")
print("     sharing a public point.  So the receiving-key change and the")
print("     finding-5 fold are the same piece of work.")
