"""Real cryptographic identities for the simulation.

EOA agents get a *real* IdentityRegistry.register() with an
issuer-signed PS credential + a registration NIZK bound to that EOA's
address (so a proof valid for one address cannot be replayed).  Contracts
(BuckBasket, every V3 pool, the Universal Router) get a public
`bindContract` Identity.  This mirrors `alberta_buck/wallet/vectors.py`
exactly (the path the Solidity verifier already accepts).
"""

from __future__ import annotations

import random
from typing import Any, Callable

from alberta_buck.wallet.bn254 import G1, mul, point_to_words, rand_scalar
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_rerandomize
from alberta_buck.wallet.elgamal import identity_keygen, elgamal_encrypt
from alberta_buck.wallet.nizk import registration_prove

# bindContract uses the G1 generator (1,2) as a non-zero placeholder pk/E so
# isVerified() is true -- exactly what the Forge tests pass (BN254.g1()).
_G = point_to_words(G1)                       # (1, 2)
BIND_PK = _G
BIND_E = (_G, _G)                              # ElGamalCT (R, C)


def seeded_rng(seed: int) -> Callable[[], int]:
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


def make_issuer(rng: Callable[[], int]):
    """An issuer PS keypair (==> trustIssuer(addr, pspubkey_arg))."""
    return ps_keygen(rng=rng)


def _g2(P) -> tuple:
    x, y = P[0].coeffs, P[1].coeffs
    return ((int(x[0]), int(x[1])), (int(y[0]), int(y[1])))


def pspubkey_arg(issuer) -> tuple:
    """PSPubKey{ G2 X; G2 Y } for IdentityRegistry.trustIssuer."""
    return (_g2(issuer.pk_X), _g2(issuer.pk_Y))


def register_args(issuer, eoa_addr: int, fields: dict,
                   rng: Callable[[], int]) -> tuple:
    """Args for IdentityRegistry.register(issuer, pk, E, sigma, proof),
    bound to `eoa_addr` (must equal the tx sender)."""
    canonical = canonical_identity_data(fields)
    m = identity_scalar(canonical)
    sigma = ps_sign(issuer, m, rng=rng)
    sigma_p, _ = ps_rerandomize(sigma, rng=rng)
    kp = identity_keygen(rng=rng)
    r = rand_scalar(rng)
    E = elgamal_encrypt(mul(G1, m), kp.pk, r)
    pf = registration_prove(sigma_p, m, r, kp.pk, E, eoa_addr, rng=rng)

    g1 = lambda P: tuple(point_to_words(P))
    pk = g1(kp.pk)
    E_arg = (g1(E.R), g1(E.C))
    sig_arg = (g1(sigma_p.sigma_1), g1(sigma_p.sigma_2))
    proof_arg = (pf.e, pf.s_m, pf.s_r, g1(pf.A_ps), g1(pf.T_C), g1(pf.T_R))
    return pk, E_arg, sig_arg, proof_arg


def fields_for(name: str, i: int) -> dict:
    """Distinct identity fields per agent (so each m is unique)."""
    return {
        "given_name":    name,
        "family_name":   f"Agent{i:04d}",
        "jurisdiction":  "Alberta, Canada",
        "id_type":       "Alberta Identity Card",
        "id_number":     f"AIC-2026-{i:07d}",
        "date_of_birth": "1990-01-01",
        "issuer_id":     "atb-financial-ca",
        "issued_at":     "2026-01-01T00:00:00Z",
        "epoch":         42,
    }
