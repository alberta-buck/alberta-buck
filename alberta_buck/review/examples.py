# SPDX-License-Identifier: GPL-3.0-or-later
"""Fresh malicious witnesses for doc/review/identity-findings.md.

Only synthetic identities and local fixtures are used. Deterministic randomness
is for reproducibility and MUST NOT be used for actual credentials or payments.
The examples call the existing wallet API; BUCK_IDENTITY_BACKEND selects its
Python reference or Rust/PyO3 implementation.
"""

from dataclasses import dataclass
import random

from alberta_buck.wallet.bn254 import G1, ORDER, add, mul, neg, point_to_words
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_rerandomize
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.nizk import registration_prove
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_prove
from alberta_buck.wallet.issuer_reenc import H_POINT, H_SCALAR
from alberta_buck.registry.tree import IdentityMerkleTree


def seeded(seed=120926):
    rng = random.Random(seed)
    return lambda: rng.getrandbits(256)


@dataclass(frozen=True)
class Account:
    m: int
    sk: int
    r: int

    @property
    def M(self):
        return mul(G1, self.m)

    @property
    def pk(self):
        return mul(G1, self.sk)

    @property
    def E(self):
        return elgamal_encrypt(self.M, self.pk, self.r)


def harvested_registration(registrant=0xBAD, registry=0):
    """§1–2: the attacker receives only the published signature and disclosed m."""
    issuer = ps_keygen(seeded())
    owner = Account(12345, 45678, 98765)
    published, _ = ps_rerandomize(ps_sign(issuer, owner.m, seeded(1)), seeded(2))
    # No use of issuer signing keys or owner's sk/r below this boundary.
    sigma, _ = ps_rerandomize(published, seeded(3))
    attacker = Account(owner.m, 22222, 33333)
    proof = registration_prove(sigma, owner.m, attacker.r, attacker.pk,
                               attacker.E, registrant, attacker.sk, 1, seeded(4),
                               registry=registry)
    return issuer, owner, published, attacker, sigma, proof


def false_identity_approval(sender=0xA, spender=0xB, chainid=1,
                            registry=0, nonce=0):
    """§3: sender knowingly substitutes a third party's identity with a new proof."""
    alice, bob = Account(12345, 45678, 98765), Account(67890, 22222, 77777)
    victim_m, r_prime = 54321, 33333
    fake_sk = (alice.sk + (alice.m - victim_m) * pow(alice.r, -1, ORDER)) % ORDER
    forged = elgamal_encrypt(mul(G1, victim_m), bob.pk, r_prime)
    proof = chaum_pedersen_prove(alice.E, forged, alice.pk, bob.pk, fake_sk,
                                 r_prime, sender, spender, chainid, seeded(5),
                                 registry=registry, nonce=nonce)
    return alice, bob, victim_m, fake_sk, r_prime, forged, proof


def double_opening():
    """§5b: two scalar openings of the implementation's same public commitment."""
    m1, m2, b1 = 12345, 54321, 44444
    b2 = (b1 + (m1 - m2) * pow(H_SCALAR, -1, ORDER)) % ORDER
    P = add(mul(G1, m1), mul(H_POINT, b1))
    return m1, b1, m2, b2, P


def limbs(n):
    return [(n >> (64*i)) & ((1 << 64)-1) for i in range(4)]


def membership_input(tree, index, M, P):
    """§5a: actual G1-tie witness. No discrete logarithm of T is needed."""
    T = add(P, neg(M))
    mx, my = point_to_words(M)
    tx, ty = point_to_words(T)
    px, py = point_to_words(P)
    path = tree.path(index)
    assert path.verify()
    return dict(identityRoot=tree.root(), PI_x=limbs(px), PI_y=limbs(py),
                Mx=limbs(mx), My=limbs(my), Mx_mod=mx % ORDER, My_mod=my % ORDER,
                Tx=limbs(tx), Ty=limbs(ty), pathElements=path.siblings,
                pathIndices=path.index_bits)


def mismatched_membership():
    member = mul(G1, 12345)
    outsider = mul(G1, 54321)
    P = add(outsider, mul(H_POINT, 22222))
    tree = IdentityMerkleTree(depth=10)
    tree.insert_identity(member)
    return member, outsider, P, tree, membership_input(tree, 0, member, P)


def aliased_g1tie_limbs(witness):
    """§5c: carry 2^64 from limb 0 into limb 1; Poseidon leaf unchanged.

    The g1-tie circuit reconstructs Mx_mod from four limbs with no 64-bit
    range check, then hashes Mx_mod for the Merkle leaf while feeding the
    raw limbs to EC add.  A carry between limbs keeps the field sum (and
    therefore the leaf) identical and changes the EC-add representation.
    """
    w = dict(witness)
    mx = list(w["Mx"])
    assert mx[1] >= 1, "need a borrowable high limb"
    mx[0] = mx[0] + (1 << 64)
    mx[1] = mx[1] - 1
    w["Mx"] = mx
    return w


def uncontrolled_registration(registrant=0xBAD, registry=0):
    """§9 inverted: NUMS pk with a dummy sk; production verify must reject."""
    from alberta_buck.review.mitigations import independent_generator
    issuer = ps_keygen(seeded())
    owner = Account(12345, 45678, 98765)
    published, _ = ps_rerandomize(ps_sign(issuer, owner.m, seeded(1)), seeded(2))
    sigma, _ = ps_rerandomize(published, seeded(3))
    # pk is a NUMS point: there is no exported scalar sk with pk = sk*G.
    pk = independent_generator()
    r = 33333
    E = elgamal_encrypt(owner.M, pk, r)
    proof = registration_prove(sigma, owner.m, r, pk, E, registrant, owner.sk,
                               1, seeded(4), registry=registry)
    return issuer, owner, pk, E, sigma, proof
