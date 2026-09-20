# Run: PYTHONPATH=. python -u scripts/review/issuer_linking_demo.py  -- review evidence for identity-findings-2.org (pure py_ecc, ~2 min)
"""Can the Identity Issuer recover which of its issued identities registered
each account, from public chain data alone?  Two-sided executable check.

REVIEW WARNING (2026-09-19): Part B is a rejected mitigation candidate.
Its separate A_ps and A_t commitments permit identity matching and recovery
of a reusable unblinded credential. The successful raw-pair checks and failed
naive harvester below do not establish privacy or prevent harvesting. See R4
and the full-transcript counterexample in doc/review/identity-findings-2.org.
The original experiment is retained unchanged below for reproducibility.

Part A  -- the CURRENT registration transcript (production nizk.py at HEAD):
           the issuer, holding only its KYC list {m_i} plus public calldata,
           recovers the full address -> identity map.  Both the public-key
           pairing test and the secret-key one-multiplication test are run.
           A stranger with no candidate list recovers nothing.  A
           transaction-time approval transcript on its own gives no predicate.

Part B  -- the SAME transcript with the one missing ingredient of a PS
           anonymous-credential *showing*: an additive blinding t folded into
           sigma'_2 and proven in zero knowledge.  The identical candidate
           tests then identify nobody, the registry-style verifier still
           accepts, the counterparty relation (same m in credential and
           ciphertext) is still what is proven, and a harvester who knows m
           but not t cannot produce an accepting registration.

Pure py_ecc so the result is independent of the compiled kernel's build age.
Synthetic identities only.  Nothing in production is modified.
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
from alberta_buck.wallet.nizk import registration_prove, registration_verify
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_prove
from alberta_buck.wallet.identity import identity_scalar
from alberta_buck.wallet.transcript import keccak_scalar

rng = random.Random(20260919)
R = lambda: rng.getrandbits(256) % ORDER or 1
N_IDENT, K_ACCTS = 4, 2
CHAIN, REGISTRY = 1, 0xCAFE

# --------------------------------------------------------------------------
# Issuance: the issuer certifies N synthetic records and (as issuer.py does)
# logs the canonical record and m for each.
issuer = ps_keygen(rng=R)
records = [{"name": f"Synthetic {i}", "id": f"AIC-{i:04d}", "issuer": "Review", "epoch": 1}
           for i in range(N_IDENT)]
kyc = [identity_scalar(rec) for rec in records]          # the issuer's "stock"
creds = [ps_sign(issuer, m, rng=R) for m in kyc]          # sigma_i, kept by holders

# --------------------------------------------------------------------------
# Registration: every holder derives K fresh accounts exactly as the wallet
# does (fresh t, sk, r per account), producing the public calldata tuple
# (registrant, pk, E, sigma', proof).  Shuffled so order carries no info.
regs = []
for i, (m, sigma) in enumerate(zip(kyc, creds)):
    for k in range(K_ACCTS):
        sigma_p, _t = ps_rerandomize(sigma, rng=R)
        sk, r = R(), R()
        pk = mul(G1, sk)
        E = elgamal_encrypt(mul(G1, m), pk, r)
        addr = rng.getrandbits(160)
        proof = registration_prove(sigma_p, m, r, pk, E, addr, sk, CHAIN, rng=R, registry=REGISTRY)
        regs.append(dict(truth=i, addr=addr, pk=pk, E=E, sigma=sigma_p, proof=proof))
rng.shuffle(regs)

t0 = time.time()
ok = all(registration_verify(g["sigma"], g["E"], g["pk"], issuer.pk_X, issuer.pk_Y,
                             g["proof"], g["addr"], CHAIN, REGISTRY) for g in regs)
print(f"[setup] {len(regs)} honest registrations verify under production nizk.py: {ok}  ({time.time()-t0:.0f}s)")

# --------------------------------------------------------------------------
print("\n=== PART A: current transcript, issuer's view (KYC list + public calldata only) ===")
# A1. secret-key test: sigma'_2 == (x + m_i*y) * sigma'_1  -- one G1 mul per candidate
hits, wrong = 0, 0
for g in regs:
    matches = [i for i, m in enumerate(kyc)
               if eq(g["sigma"].sigma_2, mul(g["sigma"].sigma_1, (issuer.sk_x + m*issuer.sk_y) % ORDER))]
    if matches == [g["truth"]]: hits += 1
    else: wrong += 1
print(f"A1 secret-key one-mul scan : identified {hits}/{len(regs)} registrations, {wrong} wrong/ambiguous")

# A2. PUBLIC-key pairing test (anyone with a candidate list): first 2 registrations
t0 = time.time()
pub_hits = 0
for g in regs[:2]:
    matches = [i for i, m in enumerate(kyc) if ps_verify(issuer.pk_X, issuer.pk_Y, g["sigma"], m)]
    pub_hits += (matches == [g["truth"]])
print(f"A2 public-key pairing scan : identified {pub_hits}/2 registrations using only (X,Y)  ({time.time()-t0:.0f}s)")

# A3. Linking across a holder's accounts: the issuer groups addresses by identity.
groups = {}
for g in regs:
    i = next(i for i, m in enumerate(kyc)
             if eq(g["sigma"].sigma_2, mul(g["sigma"].sigma_1, (issuer.sk_x + m*issuer.sk_y) % ORDER)))
    groups.setdefault(i, []).append(g["addr"])
print(f"A3 accounts linked per identity: {[len(v) for v in groups.values()]}  (truth: {K_ACCTS} each)")

# A4. Stranger with no candidate list: random guesses find nothing.
stranger_hits = sum(1 for g in regs[:2] for _ in range(50)
                    if eq(g["sigma"].sigma_2, mul(g["sigma"].sigma_1, (issuer.sk_x + R()*issuer.sk_y) % ORDER)))
print(f"A4 stranger, 100 random candidates: {stranger_hits} hits")

# A5. Transaction-time transcript alone: an approval carries E_A->B and a CP proof,
#     no signature.  The issuer has no predicate to evaluate there.
from alberta_buck.wallet.chaum_pedersen import CPProof
fields = list(CPProof.__dataclass_fields__)
print(f"A5 approval transcript fields: {fields}; contains a PS signature: False")
print("   -> the link to the person comes from the SENDER ADDRESS, already identified in A1/A2.")

# --------------------------------------------------------------------------
print("\n=== PART B: same transcript + additive blinding t (PS 'show'), issuer's view ===")
BLIND_DOMAIN = 0xB11D

def blinded_show(sigma, m, r, pk, E, registrant, sk, t, rng_):
    """sigma'' = (sigma'_1, sigma'_2 + t*sigma'_1), proof of (m, r, sk, t)."""
    sp, _ = ps_rerandomize(sigma, rng=rng_)
    s1, s2 = sp.sigma_1, add(sp.sigma_2, mul(sp.sigma_1, t))
    m_, r_, sk_, t_ = rng_(), rng_(), rng_(), rng_()
    A_ps, A_t = mul(s1, m_), mul(s1, t_)
    T_C, T_R, T_key = add(mul(G1, m_), mul(pk, r_)), mul(G1, r_), mul(G1, sk_)
    pts = (s1, s2, E.R, E.C, pk, A_ps, A_t, T_C, T_R, T_key)
    e = keccak_scalar(*(w for p in pts for w in point_to_words(p)), registrant, CHAIN, REGISTRY, BLIND_DOMAIN)
    resp = tuple((k_ + e*w) % ORDER for k_, w in ((m_, m), (r_, r), (sk_, sk), (t_, t)))
    return PSSignature(s1, s2), dict(e=e, s=resp, A_ps=A_ps, A_t=A_t, T_C=T_C, T_R=T_R, T_key=T_key)

def blinded_verify(sig, E, pk, X, Y, pf, registrant):
    s1, s2 = sig.sigma_1, sig.sigma_2
    e, (s_m, s_r, s_sk, s_t) = pf["e"], pf["s"]
    pts = (s1, s2, E.R, E.C, pk, pf["A_ps"], pf["A_t"], pf["T_C"], pf["T_R"], pf["T_key"])
    if e != keccak_scalar(*(w for p in pts for w in point_to_words(p)), registrant, CHAIN, REGISTRY, BLIND_DOMAIN):
        return False
    if not eq(add(mul(G1, s_m), mul(pk, s_r)), add(mul(E.C, e), pf["T_C"])): return False   # (b)
    if not eq(mul(G1, s_r), add(mul(E.R, e), pf["T_R"])): return False                        # (c)
    if not eq(mul(G1, s_sk), add(pf["T_key"], mul(pk, e))): return False                     # (k)
    # (a') THREE pairings, terms merged by G2 operand:
    #   e(s_m*s1 - A_ps, Y) * e(e*s1, X) * e(s_t*s1 - A_t - e*s2, G2) == 1
    chk = (pairing(Y, add(mul(s1, s_m), neg(pf["A_ps"])))
           * pairing(X, mul(s1, e))
           * pairing(G2, add(add(mul(s1, s_t), neg(pf["A_t"])), neg(mul(s2, e)))))
    return chk == FQ12_one()

bregs = []
for i, (m, sigma) in enumerate(zip(kyc, creds)):
    sk, r, t = R(), R(), R()
    pk = mul(G1, sk); E = elgamal_encrypt(mul(G1, m), pk, r); addr = rng.getrandbits(160)
    sig, pf = blinded_show(sigma, m, r, pk, E, addr, sk, t, R)
    bregs.append(dict(truth=i, addr=addr, pk=pk, E=E, sigma=sig, proof=pf, t=t))

t0 = time.time()
ok = all(blinded_verify(g["sigma"], g["E"], g["pk"], issuer.pk_X, issuer.pk_Y, g["proof"], g["addr"]) for g in bregs)
print(f"B0 honest blinded registrations verify (3-pairing check): {ok}  ({time.time()-t0:.0f}s)")

hits = sum(1 for g in bregs for m in kyc
           if eq(g["sigma"].sigma_2, mul(g["sigma"].sigma_1, (issuer.sk_x + m*issuer.sk_y) % ORDER)))
print(f"B1 secret-key one-mul scan : {hits} candidate matches across {len(bregs)}x{N_IDENT} tests (expect 0)")
pub = sum(1 for g in bregs[:2] for m in kyc if ps_verify(issuer.pk_X, issuer.pk_Y, g["sigma"], m))
print(f"B2 public-key pairing scan : {pub} candidate matches across 2x{N_IDENT} tests (expect 0)")
# Every candidate is equally consistent: for each m_i there exists a t_i explaining sigma''.
print("B3 for every candidate m_i some t_i explains sigma'' (t_i = log(s2/s1) - x - m_i*y): the")
print("   published pair is a uniform G1 pair independent of m; only the ZK proof relates them.")

# B4. Harvesting: attacker learns m (from a receipt) and sees sigma'' but not t.
g = bregs[0]; m = kyc[g["truth"]]
att_sk, att_r, att_t = R(), R(), R()
att_pk = mul(G1, att_sk); att_E = elgamal_encrypt(mul(G1, m), att_pk, att_r); att_addr = 0xBAD
# The attacker's best move: reuse sigma'' as if it were a plain signature and blind again.
sig2, pf2 = blinded_show(g["sigma"], m, att_r, att_pk, att_E, att_addr, att_sk, att_t, R)
print(f"B4 harvester (knows m, not t) re-blinds the public sigma'': verifier accepts = "
      f"{blinded_verify(sig2, att_E, att_pk, issuer.pk_X, issuer.pk_Y, pf2, att_addr)}  (expect False)")
# ... whereas the legitimate holder, who knows t, re-shows freely.
sig3, pf3 = blinded_show(creds[g['truth']], m, att_r, att_pk, att_E, att_addr, att_sk, att_t, R)
print(f"   legitimate holder (has the raw credential) registers a new account: "
      f"{blinded_verify(sig3, att_E, att_pk, issuer.pk_X, issuer.pk_Y, pf3, att_addr)}  (expect True)")

print("\nCOST DELTA (blinded show vs current): +1 G1 commitment (A_t), +1 scalar (s_t) in calldata;")
print("  prover +2 G1 mul; verifier pairing count 3 (current _checkPSPairing: 4); +2 G1 mul.")
