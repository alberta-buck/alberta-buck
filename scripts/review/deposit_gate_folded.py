# Run: PYTHONPATH=.:core/python python scripts/review/deposit_gate_folded.py
import random
from alberta_buck.wallet.bn254 import G1, ORDER, mul, add, neg, eq, rand_scalar, point_to_words
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.issuer_reenc import H_POINT as H
from alberta_buck.wallet.poseidon import F_R, poseidon
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.registry.tree import IdentityMerkleTree
ok = lambda b, s: print(("PASS" if b else "FAIL") + "  " + s)
_s = random.Random(0xF01DED); rng = lambda: _s.getrandbits(256)

def recv_leaf(M, pk_recv, salt):
    mx, my = point_to_words(M); kx, ky = point_to_words(pk_recv)
    return poseidon([mx % F_R, my % F_R, kx % F_R, ky % F_R, salt])

def deposit_witness(*, m_rec, k, sk, b, salt, E_dep, eNoteCt, tree):
    """One witness for the whole deposit gate.  Every relation asserted here is
    a constraint the folded circuit must carry; nothing is inferred from two
    proofs sharing a public point."""
    M_rec, pk_recv, pk = mul(G1, m_rec), mul(G1, k), mul(G1, sk)
    M = add(eNoteCt.C, neg(mul(eNoteCt.R, k)))                 # (1) k decrypts
    assert eq(E_dep.C, add(M_rec, mul(E_dep.R, sk))), \
        "account credential does not decrypt to m_rec*G under sk"   # (2) account
    leaf = recv_leaf(M_rec, pk_recv, salt)
    assert tree.contains(leaf), \
        "no registered leaf commits this (identity, receiving key) pair"  # (3) tie
    idx = tree.index_of_leaf(leaf); proof = tree.path(idx)
    assert proof.verify(), "membership path does not fold to the root"
    return dict(P=add(M, mul(H, b)), M=M, leaf=leaf, root=proof.root,
                pk=pk, pk_recv=pk_recv)

# Cast.
m_rec, k, sk, b = (rand_scalar(rng) for _ in range(4))
M_rec, pk_recv, pk = mul(G1, m_rec), mul(G1, k), mul(G1, sk)
m_iss = rand_scalar(rng); M_I = mul(G1, m_iss)
E_dep = elgamal_encrypt(M_rec, pk, rand_scalar(rng))
eIss  = elgamal_encrypt(M_I, pk_recv, rand_scalar(rng))
secret = rand_scalar(rng); salt = derive_salt(secret, "kyc:ca-ab-2026")
tree = IdentityMerkleTree(depth=10, private=True)
tree.insert_leaf(recv_leaf(M_rec, pk_recv, salt))

w = deposit_witness(m_rec=m_rec, k=k, sk=sk, b=b, salt=salt,
                    E_dep=E_dep, eNoteCt=eIss, tree=tree)
ok(eq(w["M"], M_I) and eq(add(w["P"], neg(mul(H, b))), M_I),
   "folded: the honest witness exists and P commits the issuer Identity")

# The thief of the naive split: own identity, own account, stolen receiving key.
m_t, sk_t = rand_scalar(rng), rand_scalar(rng)
E_t = elgamal_encrypt(mul(G1, m_t), mul(G1, sk_t), rand_scalar(rng))
tree.insert_leaf(recv_leaf(mul(G1, m_t), mul(G1, rand_scalar(rng)),
                           derive_salt(rand_scalar(rng), "kyc:ca-ab-2026")))
try:
    deposit_witness(m_rec=m_t, k=k, sk=sk_t, b=rand_scalar(rng), salt=salt,
                    E_dep=E_t, eNoteCt=eIss, tree=tree)
    ok(False, "folded: the thief built a witness (must not happen)")
except AssertionError as exc:
    ok("registered leaf" in str(exc), f"folded: the thief has no witness ({exc})")

# And the recipient's own rotation still works: new key, new leaf, new salt.
k2 = rand_scalar(rng); salt2 = derive_salt(secret, "kyc:ca-ab-2026", 1)
tree.insert_leaf(recv_leaf(M_rec, mul(G1, k2), salt2))
eIss2 = elgamal_encrypt(M_I, mul(G1, k2), rand_scalar(rng))
w2 = deposit_witness(m_rec=m_rec, k=k2, sk=sk, b=rand_scalar(rng), salt=salt2,
                     E_dep=E_dep, eNoteCt=eIss2, tree=tree)
ok(eq(w2["M"], M_I) and w2["leaf"] != w["leaf"],
   "folded: a rotated receiving key spends, under an unlinkable leaf")
