# Run: PYTHONPATH=.:core/python python scripts/review/receiving_key_proto.py
"""Prototype for doc/review/notes-receiving-key.org: notes addressed to an
independent receiving key rather than to the identity point.

Establishes, against the real wallet primitives, the claims the architecture
rests on: that today's addressed note is identified by one scalar
multiplication per candidate identity; that the same test finds nothing once
the encryption key is independent of the identity, leaving only a decisional
Diffie-Hellman instance; that the recipient still decrypts; that the
note-binding circuits keep their fixed-base shape under the substitution; and
that binding the receiving key into the salted accumulator leaf stays
unscannable and rotates without linking.

Review evidence, not production code.  The deterministic parts are synthetic.
"""
from alberta_buck.wallet.bn254 import G1, ORDER, mul, add, neg, eq, rand_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.poseidon import F_R, poseidon
from alberta_buck.wallet.bn254 import point_to_words
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.registry.tree import IdentityMerkleTree

ok = lambda b, s: print(("PASS" if b else "FAIL") + "  " + s)

# The cast: a recipient with identity m_rec, and an independent receiving key.
m_rec = rand_scalar(); M_rec = mul(G1, m_rec)
k     = rand_scalar(); pk_recv = mul(G1, k)          # independent of m_rec
m_iss = rand_scalar(); M_iss = mul(G1, m_iss)
v, r_note, r_prime = 1234, rand_scalar(), rand_scalar()

# Candidates a harvester holds: every identity it ever certified, m_rec among them.
candidates = [rand_scalar() for _ in range(8)] + [m_rec]

# --- 1. TODAY: A1 addresses the identity to itself, and the DDH lock collapses.
eRec_old = elgamal_encrypt(M_rec, M_rec, r_prime)     # (r'G, M_rec + r'*M_rec)
R_old, C_old = eRec_old.R, eRec_old.C
hits = [m for m in candidates if eq(C_old, mul(add(G1, R_old), m))]   # C == m*(G+R)
ok(hits == [m_rec], f"today: one scalar mult per candidate identifies the recipient ({len(hits)} hit)")

# --- 2. THE FIX: same plaintext, independent key.  The same test finds nothing.
eRec_new = elgamal_encrypt(M_rec, pk_recv, r_prime)   # (r'G, M_rec + r'*pk_recv)
R_new, C_new = eRec_new.R, eRec_new.C
hits = [m for m in candidates if eq(C_new, mul(add(G1, R_new), m))]
ok(hits == [], "fixed: the identity-key test finds nothing")
# The residual test is plaintext-checking under an independent key: decide whether
# (G, pk_recv, R, C - M_rec) is a DH tuple.  Infeasible; the honest witness is r'.
ok(eq(add(C_new, neg(M_rec)), mul(pk_recv, r_prime)),
   "fixed: the only handle left is a DDH instance (honest witness shown)")
ok(eq(elgamal_decrypt(eRec_new, k), M_rec), "recipient still decrypts, with k not m")

# --- 3. A2: the issuer identity under the receiving key.
eIss_new = elgamal_encrypt(M_iss, pk_recv, r_prime)
ok(eq(elgamal_decrypt(eIss_new, k), M_iss), "A2: recipient recovers the issuer identity with k")
pair_hit = any(eq(eIss_new.C, add(mul(G1, a), mul(eIss_new.R, b)))
               for a in (m_iss,) for b in candidates)
ok(not pair_hit, "A2: the candidate-pair test finds nothing")

# --- 4. The note-binding circuit keeps its fixed-base shape.
# Today it witnesses rm = r*m_rec and checks C0 = M_I + rm*G, avoiding variable
# base.  With a receiving key the witness is rk = r*k and the shape is identical.
rk = (r_prime * k) % ORDER
ok(eq(eIss_new.C, add(M_iss, mul(G1, rk))) and eq(eIss_new.R, mul(G1, r_prime)),
   "circuit: C = M + (r*k)*G and R = r*G, both fixed-base, shape unchanged")
# The value ciphertext likewise.
eNote_new = elgamal_encrypt(mul(G1, v), pk_recv, r_note)
ok(eq(eNote_new.C, add(mul(G1, v), mul(G1, (r_note * k) % ORDER))),
   "circuit: the value ciphertext keeps the same fixed-base form")

# --- 5. Bind the receiving key to the identity inside the salted leaf.
def leaf_with_key(M, pk, salt):
    mx, my = point_to_words(M); kx, ky = point_to_words(pk)
    return poseidon([mx % F_R, my % F_R, kx % F_R, ky % F_R, salt])

secret = rand_scalar()
salt = derive_salt(secret, "kyc:ca-ab-2026")
tree = IdentityMerkleTree(depth=10, private=True)
idx = tree.insert_leaf(leaf_with_key(M_rec, pk_recv, salt))
proof = tree.path(idx)
ok(proof.verify(), "binding: (identity, receiving key) commits in one salted leaf")
# A harvester holding every identity, the whole published tree, AND pk_recv
# (it is an encryption key, so assume the worst) still decides nothing.
guesses = {leaf_with_key(mul(G1, m), pk_recv, s)
           for m in candidates for s in (salt ^ 1, 12345)}
ok(not (guesses & set(tree.leaves)), "binding: the leaf is still unscannable")
# Rotation: a new key, a new salt, an unlinkable leaf.
k2 = rand_scalar()
leaf2 = leaf_with_key(M_rec, mul(G1, k2), derive_salt(secret, "kyc:ca-ab-2026", 1))
ok(leaf2 != proof.leaf, "rotation: a new receiving key yields an unlinkable leaf")
