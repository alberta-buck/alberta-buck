"""Real cryptographic identities for the simulation.

EOA agents get a *real* IdentityRegistry.register() with an
issuer-signed PS credential + a registration NIZK bound to that EOA's
address (so a proof valid for one address cannot be replayed).  Contracts
(BuckBasket, every V3 pool, the Universal Router) get a public
`bindContract` Identity.  This mirrors `alberta_buck/wallet/vectors.py`
exactly (the path the Solidity verifier already accepts).

Deterministic EOA + registration-args cache (``test/vectors/identity-cache.json``):
the first run with a given seed incurs the full NIZK-prove cost; subsequent
runs hit the cache (~instant).  Cache key = (seed_hex, class_name, agent_idx).
"""

from __future__ import annotations

import json, os, random
from pathlib import Path
from typing import Any, Callable

from eth_account import Account
from eth_account.signers.local import LocalAccount

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

_CACHE_PATH = Path(__file__).resolve().parents[2] / "test" / "vectors" / "identity-cache.json"


def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache))


def _cache_key(seed: int, class_name: str, idx: int) -> str:
    return f"{seed:08x}:{class_name}:{idx}"


def _deterministic_key(seed: int, class_name: str, idx: int) -> bytes:
    """Derive a reproducible EOA private key from seed + class + idx."""
    import hashlib
    material = f"{seed}:{class_name}:{idx}".encode()
    h = hashlib.sha256(material).digest()
    # Expand to 32 bytes via another round.
    h = hashlib.sha256(h + b"eoa").digest()
    return h


def _to_serializable(args: tuple) -> list:
    """Convert register_args return value to JSON-serialisable form."""
    pk, E_arg, sig_arg, proof_arg = args
    def tups(x):
        if isinstance(x, tuple):
            return [tups(v) for v in x]
        return x
    return [tups(pk), tups(E_arg), tups(sig_arg), tups(proof_arg)]


def _from_serializable(data: list) -> tuple:
    """Reconstruct register_args from JSON."""
    def tup(x):
        if isinstance(x, list):
            return tuple(tup(v) for v in x)
        return x
    return tuple(tup(v) for v in data)


def cached_eoa_setup(seed: int, class_name: str, idx: int, issuer,
                     rng: Callable[[], int]) -> tuple[LocalAccount, tuple]:
    """Return (account, register_args) for an agent, using disk cache.

    The EOA private key is deterministic (seed + class + idx), so the
    address is stable across runs.  Registration args are cached per key;
    only the first run pays the NIZK-prove cost.
    """
    pk_bytes = _deterministic_key(seed, class_name, idx)
    account = Account.from_key(pk_bytes)
    addr_int = int(account.address, 16)

    cache = _load_cache()
    key = _cache_key(seed, class_name, idx)
    if key in cache:
        return account, _from_serializable(cache[key])

    # Generate fresh registration args and cache them.
    fields = fields_for(class_name, idx)
    args = register_args(issuer, addr_int, fields, rng)
    cache[key] = _to_serializable(args)
    _save_cache(cache)
    return account, args


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
