# Run: PYTHONPATH=. python scripts/review/silmarils_model.py  -- review evidence; see doc/review/identity-findings.md
"""Executable model of two-party SILMARILS (the SILMARILS paper's Algorithms
KeyGen/Sign/Verify, eqs. silmarils_eqs / silmarils_ver).

Purpose: check, by computation rather than reading, three questions raised by
the review and by doc/SILMARILS-evaluation.org:

  Q1  Does the verifier use the signer's long-term key K at all?
  Q2  Does anyone holding r = H(M, HMAC_{k_sig}(M)) forge without K?  (paper's
      own Remark after Lemma `algcore`; this is what makes it a DV/MAC scheme)
  Q3  Does an "SSS commitment" bind anything for a PUBLIC verifier (the
      evaluation doc's proposal to replace EC Okamoto sigmas)?

No production code is touched.  Field: p = 2^255 - 19 (prime).
"""
import hashlib, hmac, secrets

p = 2**255 - 19


def rnd_nz():
    while True:
        x = secrets.randbelow(p)
        if x:
            return x


def inv(x):
    return pow(x, p - 2, p)


def H(*parts):
    h = hashlib.sha256()
    for q in parts:
        h.update(str(q).encode() + b"|")
    return int.from_bytes(h.digest(), "big") % p


def HMAC(key, M):
    return int.from_bytes(hmac.new(str(key).encode(), str(M).encode(),
                                   hashlib.sha256).digest(), "big") % p


# --- 2-of-2 Shamir over F_p with public weights w0, w1 -----------------------
w0, w1 = rnd_nz(), rnd_nz()
assert w0 != w1


def sss(s):
    a = secrets.randbelow(p)
    return (s + a * w0) % p, (s + a * w1) % p


def sss_inv(s0, s1):
    return (w0 * s1 - w1 * s0) * inv((w0 - w1) % p) % p


# --- KeyGen / Sign / Verify exactly as in the paper --------------------------
K = rnd_nz()            # signer's long-term secret key
k_sig = rnd_nz()        # secret shared by signer and DESIGNATED verifier


def sign(M, K, k_sig):
    n = HMAC(k_sig, M)
    r = H(M, n)
    alpha, beta, b, d = rnd_nz(), rnd_nz(), rnd_nz(), rnd_nz()
    eps = alpha * beta % p
    e0, e1 = sss(eps)
    Kp = HMAC(K, M)
    K0, K1 = sss(Kp)
    ie = inv(eps)
    s1 = b * (Kp - r) % p
    s2 = d * inv(b) % p
    s3 = K1 * d % p
    s4 = d * ie * e1 % p
    s5 = d * (K0 - r * ie * e0) % p
    return (s1, s2, s3, s4, s5)


def verify_with_r(M, sig, r):
    s1, s2, s3, s4, s5 = sig
    if s4 == 0:
        return False
    V0 = (s1 * s2 - s5) % p
    V1 = (s1 * s2 - s3 + r * s4) % p
    return sss_inv(V0, V1) == 0


def verify(M, sig, k_sig):
    return verify_with_r(M, sig, H(M, HMAC(k_sig, M)))


M = "pay 100 BUCK to Bob"
sig = sign(M, K, k_sig)
assert verify(M, sig, k_sig), "honest signature must verify"

# Q1: the verifier never touches K.  Sign with a DIFFERENT K': still verifies.
sig_otherK = sign(M, rnd_nz(), k_sig)
assert verify(M, sig_otherK, k_sig)
print("Q1  signature made with a different signer key K verifies:", True)

# Q2: forgery from r alone (paper's Remark): choose s1..s4, solve for s5.
r = H(M, HMAC(k_sig, M))
s1, s2, s3, s4 = rnd_nz(), rnd_nz(), rnd_nz(), rnd_nz()
V1 = (s1 * s2 - s3 + r * s4) % p
s5 = (s1 * s2 - w0 * inv(w1) * V1) % p
forged = (s1, s2, s3, s4, s5)
assert verify(M, forged, k_sig)
print("Q2  forgery with r only (no K, no k_sig beyond r) verifies:", True)

# Q2b: without r, a random tuple verifies with probability ~1/p.
bad = 0
for _ in range(200):
    r_guess = secrets.randbelow(p)
    if verify_with_r(M, forged, r_guess):
        bad += 1
print("Q2b random-r acceptances out of 200 (expect 0):", bad)

# Q2c: a published receipt (M, sig, r) lets ANY third party mint a fresh,
# different accepting transcript for the same M -- so the receipt is not
# evidence of who signed.
s1, s2, s3, s4 = rnd_nz(), rnd_nz(), rnd_nz(), rnd_nz()
V1 = (s1 * s2 - s3 + r * s4) % p
third_party = (s1, s2, s3, s4, (s1 * s2 - w0 * inv(w1) * V1) % p)
assert third_party != sig and verify_with_r(M, third_party, r)
print("Q2c third party with receipt r forges a distinct transcript:", True)

# Q3: an F_p "commitment" c = x*g + k*h with PUBLIC g, h is openable to any x'.
g, h = rnd_nz(), rnd_nz()
x, k = rnd_nz(), rnd_nz()
c = (x * g + k * h) % p
x_prime = rnd_nz()
k_prime = (k + (x - x_prime) * g * inv(h)) % p
assert (x_prime * g + k_prime * h) % p == c
print("Q3  F_p 'commitment' opened to a different witness:", True)

# Q3b: a public-coefficient linear check "s*g == A + e*pk" (the E4 relation
# of the evaluation doc, with EC scalar-mult replaced by field mult) is
# satisfied without knowing sk: choose s, solve for A.
pk = rnd_nz()                     # "pk = sk*g", sk unknown to the forger
e = H("challenge")
s = rnd_nz()
A = (s * g - e * pk) % p          # forger picks A after choosing s
assert (s * g) % p == (A + e * pk) % p
# Even worse: sk itself is just division in F_p.
sk_recovered = pk * inv(g) % p
assert sk_recovered * g % p == pk
print("Q3b field-linear 'sigma' forged AND witness recovered by division:", True)
