# SPDX-License-Identifier: GPL-3.0-or-later
"""Holder authorization for an exact IdentityRegistry contract binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, eq, is_inf, mul, point_to_words, rand_scalar,
)
from alberta_buck.wallet.transcript import keccak_raw, keccak_scalar


from alberta_buck.wallet.domains import FS_CONTRACT_BINDING, word as _word
CONTRACT_BINDING_DOMAIN = _word(FS_CONTRACT_BINDING)


@dataclass(frozen=True)
class ContractBindingProof:
    e: int
    s: int
    T: Tuple


def _challenge(
    target: int,
    binder: int,
    registry: int,
    pk,
    T,
    is_public_identity: bool,
    is_carrying: bool,
    chainid: int,
) -> int:
    pkx, pky = point_to_words(pk)
    tx, ty = point_to_words(T)
    return keccak_scalar(
        pkx, pky, tx, ty,
        CONTRACT_BINDING_DOMAIN, registry, chainid, target, binder,
        int(is_public_identity), int(is_carrying),
    )


def contract_binding_prove(
    sk: int,
    pk,
    target: int,
    binder: int,
    registry: int,
    is_public_identity: bool,
    is_carrying: bool,
    chainid: int = 1,
    rng=None,
) -> ContractBindingProof:
    """Authorize one exact target/binder/policy tuple under ``pk = sk*G``."""
    if is_inf(pk) or not eq(pk, mul(G1, sk % ORDER)):
        raise ValueError("pk does not match sk")
    k = rand_scalar(rng)
    T = mul(G1, k)
    e = _challenge(
        target, binder, registry, pk, T,
        is_public_identity, is_carrying, chainid,
    )
    return ContractBindingProof(e=e, s=(k + e * sk) % ORDER, T=T)


def contract_binding_verify(
    pk,
    target: int,
    binder: int,
    registry: int,
    proof: ContractBindingProof,
    is_public_identity: bool,
    is_carrying: bool,
    chainid: int = 1,
) -> bool:
    if is_inf(pk) or is_inf(proof.T):
        return False
    if not (0 <= proof.e < ORDER and 0 <= proof.s < ORDER):
        return False
    e = _challenge(
        target, binder, registry, pk, proof.T,
        is_public_identity, is_carrying, chainid,
    )
    return proof.e == e and eq(mul(G1, proof.s), add(proof.T, mul(pk, proof.e)))
