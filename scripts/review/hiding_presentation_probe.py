# Run: PYTHONPATH=.:core/python python -u scripts/review/hiding_presentation_probe.py  -- review evidence for identity-findings-2.org (R2 on production; Pathway A' candidate battery). Candidate, NOT a repair.
"""Two checks for identity-findings-2.org.

1. R2 on the PRODUCTION registration proof: the proof fields alone
   (A, A_ps, s_m, e) identify a known identity, without the signature's
   second point and without any issuer key.

2. A CANDIDATE hiding presentation that avoids the structural leak of R2/R4
   while still mapping to the EVM pairing precompile.  The textbook PS
   showing commits in G_T, which the precompile cannot consume; separate G1
   commitments leak (R2/R4).  This candidate blinds on the Y base using a G1
   image Y1 = y*G of the issuer's second secret, so message and blinding share
   ONE G1 commitment C1 = m~*A + b~*G paired with Y:

       A  = a*sigma_1                      (uniform)
       B  = (x + m*y)*A + b*Y1             (uniform given A, for uniform b)
       statement:  e(B, G2) = e(A, X) * e(m*A + b*G, Y)
       verifier:   e(s_m*A + s_b*G - C1, Y) * e(e*A, X) * e(-e*B, G2) == 1
                   plus the unchanged ElGamal (b),(c) and key (k) checks.

   The full adversarial battery of the org document is run against it.
   This is a candidate for cryptographic review, NOT a repair.  Publishing
   Y1 changes the issuer-key form to the PS16 s.6.1 (committed-message)
   setting and its assumption; that must be adopted deliberately.

Pure py_ecc.  Synthetic identities.  Nothing in production is modified.
"""

# ---------------------------------------------------------------------------
# PINNED EVIDENCE.  This script reproduces findings against the PRE-A' wallet
# and registry at 75104a8 (feature/paper-editing): it needs the rerandomized
# pair and the A_ps commitment that the A' branch no longer publishes.  On a
# post-A' checkout it stops here.  Run it from a checkout of that commit:
#     git worktree add ../alberta-buck-75104a8 75104a8
# The branch's executable counterpart is the review suite,
# alberta_buck/test/review/test_wallet_failures.py, whose tests 1 and 2 are
# inverted and whose A' battery replaces this script's checks.
# ---------------------------------------------------------------------------
import sys as _sys
try:
    from alberta_buck.wallet import ps as _ps
    _POST_A_PRIME = hasattr(_ps, "ps_present")
except Exception:  # pragma: no cover - the pre-A' tree may lack the package layout
    _POST_A_PRIME = False
if _POST_A_PRIME and "--force" not in _sys.argv:
    _sys.exit(__file__.split("/")[-1] + ": pre-A' review evidence; run it from a "
              "checkout of 75104a8 (see the PINNED EVIDENCE note in the header).")

import os, random, time
os.environ["BUCK_IDENTITY_BACKEND"] = "py"
from alberta_buck.wallet.bn254 import (G1, G2, ORDER, add, mul, neg, eq,
                                       pairing, FQ12_one, point_to_words)
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_rerandomize, ps_verify, PSSignature
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.nizk import registration_prove
from alberta_buck.wallet.identity import identity_scalar
from alberta_buck.wallet.transcript import keccak_scalar

rng = random.Random(20260919 + 1)
R = lambda: rng.getrandbits(256) % ORDER or 1
N = 4
CHAIN, REGISTRY, DOM = 1, 0xCAFE, 0xA11D
inv = lambda z: pow(z, -1, ORDER)

issuer = ps_keygen(rng=R)
X, Y = issuer.pk_X, issuer.pk_Y
Y1 = mul(G1, issuer.sk_y)                       # added public key element (G1 image of y)
kyc = [identity_scalar({"name": f"Synthetic {i}", "id": f"AIC-{i:04d}", "issuer": "Review"}) for i in range(N)]
creds = [ps_sign(issuer, m, rng=R) for m in kyc]

# ---------------------------------------------------------------- 1. R2 on production
print("=== 1. R2: production registration proof fields identify the identity ===")
hits = 0
for i, (m, sigma) in enumerate(zip(kyc, creds)):
    sp, _ = ps_rerandomize(sigma, rng=R)
    sk, r = R(), R(); pk = mul(G1, sk); E = elgamal_encrypt(mul(G1, m), pk, r)
    pf = registration_prove(sp, m, r, pk, E, 0xA11C + i, sk, CHAIN, rng=R, registry=REGISTRY)
    A = sp.sigma_1
    matches = [j for j, mj in enumerate(kyc) if eq(mul(A, pf.s_m), add(pf.A_ps, mul(A, (pf.e * mj) % ORDER)))]
    hits += (matches == [i])
print(f"R2 proof-field scan (A, A_ps, s_m, e only; no sigma_2, no issuer key): {hits}/{N} identified")

# ---------------------------------------------------------------- 2. candidate presentation
def fs(*pts, extra):
    return keccak_scalar(*(w for p in pts for w in point_to_words(p)), *extra)

def show(sigma, m, r, pk, E, registrant, sk, rng_, b=None):
    sp, _ = ps_rerandomize(sigma, rng=rng_)
    b = rng_() if b is None else b
    A, B = sp.sigma_1, add(sp.sigma_2, mul(Y1, b))
    m_, b_, r_, sk_ = rng_(), rng_(), rng_(), rng_()
    C1 = add(mul(A, m_), mul(G1, b_))              # ONE PS-side commitment
    T_C, T_R, T_key = add(mul(G1, m_), mul(pk, r_)), mul(G1, r_), mul(G1, sk_)
    e = fs(A, B, E.R, E.C, pk, C1, T_C, T_R, T_key, extra=(registrant, CHAIN, REGISTRY, DOM))
    s = tuple((k_ + e * w) % ORDER for k_, w in ((m_, m), (b_, b), (r_, r), (sk_, sk)))
    return (A, B), dict(e=e, s=s, C1=C1, T_C=T_C, T_R=T_R, T_key=T_key)

def verify(pair, E, pk, pf, registrant):
    A, B = pair
    e, (s_m, s_b, s_r, s_sk) = pf["e"], pf["s"]
    if e != fs(A, B, E.R, E.C, pk, pf["C1"], pf["T_C"], pf["T_R"], pf["T_key"],
               extra=(registrant, CHAIN, REGISTRY, DOM)):
        return False
    if not eq(add(mul(G1, s_m), mul(pk, s_r)), add(mul(E.C, e), pf["T_C"])): return False   # (b)
    if not eq(mul(G1, s_r), add(mul(E.R, e), pf["T_R"])): return False                        # (c)
    if not eq(mul(G1, s_sk), add(pf["T_key"], mul(pk, e))): return False                     # (k)
    lhs = add(add(mul(A, s_m), mul(G1, s_b)), neg(pf["C1"]))
    chk = pairing(Y, lhs) * pairing(X, mul(A, e)) * pairing(G2, neg(mul(B, e)))            # 3 pairings
    return chk == FQ12_one()

regs = []
for i, (m, sigma) in enumerate(zip(kyc, creds)):
    sk, r = R(), R(); pk = mul(G1, sk); E = elgamal_encrypt(mul(G1, m), pk, r)
    pair, pf = show(sigma, m, r, pk, E, 0xB000 + i, sk, R)
    regs.append(dict(truth=i, pair=pair, pf=pf, E=E, pk=pk, sk=sk, r=r, addr=0xB000 + i))

print("\n=== 2. candidate single-commitment presentation, full adversarial battery ===")
t0 = time.time()
ok = all(verify(g["pair"], g["E"], g["pk"], g["pf"], g["addr"]) for g in regs)
bad_ctx = verify(regs[0]["pair"], regs[0]["E"], regs[0]["pk"], regs[0]["pf"], 0xDEAD)
print(f"honest showings verify: {ok}; wrong registrant rejected: {not bad_ctx}  ({time.time()-t0:.0f}s)")

def scan(label, pred):
    total = sum(1 for g in regs for j, mj in enumerate(kyc) if pred(g, j, mj))
    print(f"{label}: {total} matches over {len(regs)}x{N} candidate tests")
    return total

# raw-pair scans (R1), secret and public key
scan("R1 secret-key pair scan   B == (x+m_i*y)*A          ",
     lambda g, j, mj: eq(g["pair"][1], mul(g["pair"][0], (issuer.sk_x + mj * issuer.sk_y) % ORDER)))
scan("R1 public-key pairing scan ps_verify(X,Y,(A,B),m_i) ",
     lambda g, j, mj: ps_verify(X, Y, PSSignature(*g["pair"]), mj) if j < 2 and g["truth"] < 2 else False)
# proof-field scan (R2) with C1 in the role of A_ps
scan("R2 proof-field scan  s_m*A == C1 + e*m_i*A          ",
     lambda g, j, mj: eq(mul(g["pair"][0], g["pf"]["s"][0]), add(g["pf"]["C1"], mul(g["pair"][0], (g["pf"]["e"] * mj) % ORDER))))

# candidate-adapted checks: for each m_i derive b_i*G from the proof and test the pairing relation,
# and (issuer) strip with y.  Both hold for EVERY candidate -> no discrimination.
def bG_for(g, mj):
    A, pf = g["pair"][0], g["pf"]; e, (s_m, s_b, _, _) = pf["e"], pf["s"]
    return mul(add(add(mul(G1, s_b), neg(pf["C1"])), mul(A, (s_m - e * mj) % ORDER)), inv(e))
def pairing_adapted(g, j, mj):
    A, B = g["pair"]
    P = add(mul(A, mj), bG_for(g, mj))
    return pairing(G2, B) == pairing(X, A) * pairing(Y, P)
n_pa = scan("R2' candidate-adapted pairing relation (public)   ",
            lambda g, j, mj: pairing_adapted(g, j, mj) if g["truth"] < 2 else False)
n_st = scan("R4' issuer strip  B - y*(b_i*G) == (x+m_i*y)*A    ",
            lambda g, j, mj: eq(add(g["pair"][1], neg(mul(bG_for(g, mj), issuer.sk_y))),
                                mul(g["pair"][0], (issuer.sk_x + mj * issuer.sk_y) % ORDER)))
print(f"   -> both hold for all candidates ({n_pa} of {2*N}, {n_st} of {N*N}): uninformative, as the derived point")
print("      P = e^-1(s_m*A + s_b*G - C1) = m*A + b*G is the same for every candidate m_i.")

# R4-style credential extraction WITHOUT y: stripping needs b*Y1 = y*(b*G): CDH(b*G, Y1).
g = regs[0]; m = kyc[0]; A, B = g["pair"]
naive = PSSignature(A, add(B, neg(bG_for(g, m))))           # subtracts b*G (wrong group image)
print(f"R4 extraction without y: (A, B - b*G) verifies as a plain signature on m: {ps_verify(X, Y, naive, m)}  (expect False)")

# harvesting: attacker knows m and the public transcript, not b (nor y)
att_sk, att_r = R(), R(); att_pk = mul(G1, att_sk); att_E = elgamal_encrypt(mul(G1, m), att_pk, att_r)
pair2, pf2 = show(PSSignature(A, B), m, att_r, att_pk, att_E, 0xBAD, att_sk, R)   # treats (A,B) as a plain signature, adds own b
print(f"harvester: re-blinds public (A,B) with own b, proves with own b only: accepted = {verify(pair2, att_E, att_pk, pf2, 0xBAD)}  (expect False)")
pair3, pf3 = show(creds[0], m, att_r, att_pk, att_E, 0xBAD, att_sk, R)
print(f"legitimate holder with the raw credential registers a new account: accepted = {verify(pair3, att_E, att_pk, pf3, 0xBAD)}  (expect True)")

# cross-account: two showings by one holder are two independent uniform transcripts
pa, _ = show(creds[1], kyc[1], R(), mul(G1, 5), elgamal_encrypt(mul(G1, kyc[1]), mul(G1, 5), R()), 0xC1, 5, R)
pb, _ = show(creds[1], kyc[1], R(), mul(G1, 7), elgamal_encrypt(mul(G1, kyc[1]), mul(G1, 7), R()), 0xC2, 7, R)
print(f"cross-account: two showings of one credential share no point: {not eq(pa[0], pb[0]) and not eq(pa[1], pb[1])}")

print("\nCOST vs today's verifier: C1 replaces A_ps (same size); +1 scalar s_b (32 B) in calldata;")
print("  pairing product of 3 (today 4); +1 G1 mul (s_b*G); issuer PSPubKey gains one G1 point Y1.")
print("ASSUMPTION CHANGE: Y1 = y*G is published (PS16 s.6.1 committed-message key form). Not reviewed.")
