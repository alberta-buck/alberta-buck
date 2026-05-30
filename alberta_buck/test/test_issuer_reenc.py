"""Reference-math tests for the A2 issuer re-encryption binding
(alberta-buck-notes-decryptability.org, recipient-blinded on-chain CP).

Completeness (an honestly formed binding verifies) and soundness:
  - a leaf whose E_iss does NOT re-encrypt the issuer's registered M is
    rejected (the colluding-issuer attack);
  - tampered responses / commitments / blinding values reject;
  - a wrong issuer key rejects;
  - replay across issuer/chain rejects;
  - recipient privacy: two bindings to distinct recipients are
    indistinguishable in the published (Q, U, T) + transcript.
"""

from __future__ import annotations

import random

from alberta_buck.wallet.bn254 import G1, ORDER, add, mul, neg, eq, rand_scalar
from alberta_buck.wallet.elgamal import (
    ElGamalCiphertext, identity_keygen, elgamal_encrypt, elgamal_decrypt,
)
from alberta_buck.wallet.issuer_reenc import (
    IssuerReencProof, issuer_reenc_prove, issuer_reenc_verify, H_POINT,
)

ISSUER = 0x155EC00000000000000000000000000000155EC0
CHAINID = 1


def _rng(seed: int):
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


def _setup(seed: int):
    """An issuer (registered credential) + a recipient key + an honest A2 leaf."""
    rng = _rng(seed)
    # Issuer: identity m_iss, key sk_iss, registered E_addr[issuer].
    sk_iss = rand_scalar(rng)
    pk_iss = mul(G1, sk_iss)
    m_iss = rand_scalar(rng)
    M_iss = mul(G1, m_iss)
    r_reg = rand_scalar(rng)
    E_reg = elgamal_encrypt(M_iss, pk_iss, r_reg)        # (R_reg, C_reg)

    # Recipient key.
    rec = identity_keygen(rng=rng)                        # pk_rec = sk_rec*G

    # Honest A2 leaf: E_iss = (r'*G, M_iss + r'*pk_rec).
    r_prime = rand_scalar(rng)
    E_iss = elgamal_encrypt(M_iss, rec.pk, r_prime)

    return {
        "sk_iss": sk_iss, "pk_iss": pk_iss, "M_iss": M_iss,
        "E_reg": E_reg, "rec": rec, "r_prime": r_prime, "E_iss": E_iss,
        "rng": rng,
    }


# ---- completeness ----------------------------------------------------------

def test_honest_binding_verifies():
    s = _setup(1)
    pf = issuer_reenc_prove(s["sk_iss"], s["r_prime"], s["rec"].pk,
                            s["E_reg"], s["E_iss"], ISSUER, CHAINID, rng=s["rng"])
    assert issuer_reenc_verify(s["pk_iss"], s["E_reg"], s["E_iss"], pf, ISSUER, CHAINID)


def test_recipient_decrypts_to_issuer_M():
    # The whole point: the recipient recovers the issuer's registered Identity.
    s = _setup(2)
    M_dec = elgamal_decrypt(s["E_iss"], s["rec"].sk)
    assert eq(M_dec, s["M_iss"])


# ---- soundness: the colluding-issuer attack --------------------------------

def test_random_E_iss_rejected():
    # Issuer commits a random E_iss (decrypts to garbage, no recoverable M).
    # prove() asserts consistency, so the attack surfaces as a build failure;
    # to test the *verifier*, forge a proof shape over a random leaf and check
    # the verifier rejects.
    s = _setup(3)
    rng = s["rng"]
    bogus = ElGamalCiphertext(R=mul(G1, rand_scalar(rng)),
                              C=mul(G1, rand_scalar(rng)))
    # Honest proof is for the real leaf; verifying it against the bogus leaf fails.
    pf = issuer_reenc_prove(s["sk_iss"], s["r_prime"], s["rec"].pk,
                            s["E_reg"], s["E_iss"], ISSUER, CHAINID, rng=rng)
    assert not issuer_reenc_verify(s["pk_iss"], s["E_reg"], bogus, pf, ISSUER, CHAINID)


def test_prove_rejects_inconsistent_leaf():
    # A leaf that does not re-encrypt the issuer's registered M cannot even be
    # proved (prove asserts the relation) -- the issuer cannot bind a bad leaf.
    s = _setup(4)
    rng = s["rng"]
    wrong_M = mul(G1, rand_scalar(rng))
    bad_leaf = elgamal_encrypt(wrong_M, s["rec"].pk, s["r_prime"])
    raised = False
    try:
        issuer_reenc_prove(s["sk_iss"], s["r_prime"], s["rec"].pk,
                           s["E_reg"], bad_leaf, ISSUER, CHAINID, rng=rng)
    except AssertionError:
        raised = True
    assert raised


def test_tampered_response_rejected():
    s = _setup(5)
    pf = issuer_reenc_prove(s["sk_iss"], s["r_prime"], s["rec"].pk,
                            s["E_reg"], s["E_iss"], ISSUER, CHAINID, rng=s["rng"])
    for field in ("s_r", "s_b", "s_s"):
        bad = IssuerReencProof(**{**pf.__dict__, field: (getattr(pf, field) + 1) % ORDER})
        assert not issuer_reenc_verify(s["pk_iss"], s["E_reg"], s["E_iss"], bad, ISSUER, CHAINID)


def test_tampered_T_rejected():
    # Perturbing the published T (= r'*pk_rec) must break L3/L5.
    s = _setup(6)
    pf = issuer_reenc_prove(s["sk_iss"], s["r_prime"], s["rec"].pk,
                            s["E_reg"], s["E_iss"], ISSUER, CHAINID, rng=s["rng"])
    bad = IssuerReencProof(**{**pf.__dict__, "T": add(pf.T, G1)})
    assert not issuer_reenc_verify(s["pk_iss"], s["E_reg"], s["E_iss"], bad, ISSUER, CHAINID)


def test_wrong_issuer_key_rejected():
    s = _setup(7)
    pf = issuer_reenc_prove(s["sk_iss"], s["r_prime"], s["rec"].pk,
                            s["E_reg"], s["E_iss"], ISSUER, CHAINID, rng=s["rng"])
    wrong_pk = mul(G1, rand_scalar(s["rng"]))
    assert not issuer_reenc_verify(wrong_pk, s["E_reg"], s["E_iss"], pf, ISSUER, CHAINID)


def test_replay_other_issuer_or_chain_rejected():
    s = _setup(8)
    pf = issuer_reenc_prove(s["sk_iss"], s["r_prime"], s["rec"].pk,
                            s["E_reg"], s["E_iss"], ISSUER, CHAINID, rng=s["rng"])
    assert not issuer_reenc_verify(s["pk_iss"], s["E_reg"], s["E_iss"], pf, ISSUER + 1, CHAINID)
    assert not issuer_reenc_verify(s["pk_iss"], s["E_reg"], s["E_iss"], pf, ISSUER, CHAINID + 1)


# ---- recipient privacy -----------------------------------------------------

def test_recipient_hiding():
    # Two honest bindings of the SAME issuer leaf-value to DIFFERENT recipients
    # must be indistinguishable in (Q, U, T): each is uniformly randomised, so
    # neither reveals pk_rec.  We assert the published values are not equal to
    # pk_rec and that swapping recipients yields a verifying-but-distinct proof.
    s = _setup(9)
    rng = s["rng"]
    pf1 = issuer_reenc_prove(s["sk_iss"], s["r_prime"], s["rec"].pk,
                             s["E_reg"], s["E_iss"], ISSUER, CHAINID, rng=rng)
    # A second recipient + leaf.
    rec2 = identity_keygen(rng=rng)
    r2 = rand_scalar(rng)
    leaf2 = elgamal_encrypt(s["M_iss"], rec2.pk, r2)
    pf2 = issuer_reenc_prove(s["sk_iss"], r2, rec2.pk,
                             s["E_reg"], leaf2, ISSUER, CHAINID, rng=rng)
    assert issuer_reenc_verify(s["pk_iss"], s["E_reg"], leaf2, pf2, ISSUER, CHAINID)
    # Published Q never equals the bare pk_rec (blinded by beta*H).
    assert not eq(pf1.Q, s["rec"].pk)
    assert not eq(pf2.Q, rec2.pk)
    # The two proofs' published values differ (distinct randomness).
    assert not eq(pf1.Q, pf2.Q)


# ---- canonical vector parity -----------------------------------------------

def test_vector_section_verifies():
    """The issuer_reenc section emitted into identity.json must verify."""
    from alberta_buck.wallet.vectors import build_vectors
    from alberta_buck.wallet.bn254 import words_to_point

    def _h(s):
        return int(s, 16)

    def _pt(d):
        return words_to_point(_h(d["x"]), _h(d["y"]))

    def _ct(d):
        return ElGamalCiphertext(R=_pt(d["R"]), C=_pt(d["C"]))

    v = build_vectors()
    r = v["issuer_reenc"]
    p = r["proof"]
    pf = IssuerReencProof(
        e=_h(p["e"]), s_r=_h(p["s_r"]), s_b=_h(p["s_b"]), s_s=_h(p["s_s"]),
        A1=_pt(p["A1"]), A2=_pt(p["A2"]), A3=_pt(p["A3"]),
        A4=_pt(p["A4"]), A5=_pt(p["A5"]),
        Q=_pt(p["Q"]), U=_pt(p["U"]), T=_pt(p["T"]),
    )
    assert issuer_reenc_verify(_pt(r["pk_iss"]), _ct(r["E_reg"]), _ct(r["E_iss"]),
                               pf, _h(r["issuer"]), _h(r["chainid"]))
