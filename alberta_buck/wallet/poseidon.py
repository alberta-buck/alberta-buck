"""Pure-Python Poseidon over BN254 -- matches circomlibjs's unoptimized variant.

Why this exists: the Phase 6/7/7-bis circuits (mint, mint_batch, spend) all
hash with circomlib's Poseidon, and we want Python-side witness emitters and
test oracles whose commitments / nullifiers / Merkle nodes agree exactly with
those circuits.  The circomlibjs JSON constants are vendored as package data
(``alberta_buck/wallet/poseidon_constants.json``, a byte-identical copy of
``node_modules/circomlibjs/src/poseidon_constants.json``) so they ship with a
pip install rather than requiring an npm-installed repo checkout; when running
from a checkout that has ``node_modules``, the circomlibjs original is used as
a fallback if the vendored copy is missing.

What this matches: this is the *unoptimized* Poseidon algorithm (full M matrix,
per-round full C vector).  It is mathematically equivalent to the optimized
form used inside the circuit (sparse S matrices, pre-folded P matrices), so
both produce the same output for the same inputs.  The on-chain Tornado-style
PoseidonT3 helper deployed via PoseidonT3Bytecode.sol is also produced from
the *unoptimized* form (see scripts/snark/poseidon_t3_code.js).

Usage::

    from alberta_buck.wallet.poseidon import poseidon
    h = poseidon([1, 2])               # Poseidon-T3 (2 inputs)
    cm = poseidon([flavor, v, rho, idHash, predicate])  # Poseidon-T6
    nf = poseidon([rho, idHash, 4242]) # Poseidon-T4
"""

from __future__ import annotations

import json
import os
from typing import List, Sequence

# BN254 scalar field order (= ORDER from alberta_buck.wallet.bn254).
F_R = 21888242871839275222246405745257275088548364400416034343698204186575808495617

# Standard Hades parameter set (Poseidon paper table 2 / circomlibjs).
_N_ROUNDS_F = 8
_N_ROUNDS_P = [56, 57, 56, 60, 60, 63, 64, 63, 60, 66, 60, 65, 70, 60, 64, 68]

# Lazily-loaded constants from circomlibjs's JSON file.  We read on first use
# rather than at import time so a wallet that never hashes pays no IO cost.
_C: List[List[int]] | None = None  # _C[t-2][round*t + i]   round constants
_M: List[List[List[int]]] | None = None  # _M[t-2][i][j]         MDS matrix

# Vendored package-data copy (shipped by pip; see pyproject.toml
# [tool.setuptools.package-data]), with the circomlibjs original from a
# repo checkout's node_modules as fallback.
_CONSTS_PATHS = (
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "poseidon_constants.json",
    ),
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..",
        "node_modules", "circomlibjs", "src", "poseidon_constants.json",
    ),
)


def _to_int(s) -> int:
    if isinstance(s, int):
        return s
    s = str(s)
    return int(s, 16) if s.startswith("0x") else int(s)


def _load() -> None:
    global _C, _M
    if _C is not None:
        return
    for path in _CONSTS_PATHS:
        if os.path.exists(path):
            with open(path) as fh:
                data = json.load(fh)
            break
    else:
        raise FileNotFoundError(
            f"poseidon_constants.json not found in any of: {_CONSTS_PATHS}"
        )
    _C = [[_to_int(s) for s in row] for row in data["C"]]
    _M = [[[_to_int(s) for s in row] for row in mat] for mat in data["M"]]


def _pow5(x: int) -> int:
    x2 = (x * x) % F_R
    x4 = (x2 * x2) % F_R
    return (x4 * x) % F_R


def poseidon(inputs: Sequence[int]) -> int:
    """Compute Poseidon(inputs) over BN254 with state width t = len(inputs)+1.

    Matches `circomlibjs.buildPoseidon()(inputs)` (which is the same hash
    the circomlib *circuit* Poseidon computes, just via different but
    equivalent constants).  Dispatches to the compiled buck-identity kernel
    when built (same constants, compiled in; proven bit-identical) -- the
    pure-Python rounds below remain the executable spec.
    """
    from alberta_buck.wallet._kernel import kernel as _kernel
    k = _kernel()
    if k is not None and 1 <= len(inputs) <= 16:
        return k.poseidon([_to_int(x) % F_R for x in inputs])
    _load()
    assert _C is not None and _M is not None  # for type-checkers
    n = len(inputs)
    if not (1 <= n <= 16):
        raise ValueError(f"poseidon arity must be 1..16, got {n}")
    t = n + 1
    nF = _N_ROUNDS_F
    nP = _N_ROUNDS_P[t - 2]
    C = _C[t - 2]
    M = _M[t - 2]

    state = [0] + [_to_int(x) % F_R for x in inputs]
    for r in range(nF + nP):
        # ARK: add round constants.
        for i in range(t):
            state[i] = (state[i] + C[r * t + i]) % F_R
        # SBox: full rounds S-box every cell, partial rounds S-box state[0].
        if r < nF // 2 or r >= nF // 2 + nP:
            for i in range(t):
                state[i] = _pow5(state[i])
        else:
            state[0] = _pow5(state[0])
        # MIX: state <- M * state (M is row-major; new[i] = sum_j M[i][j]*state[j]).
        new = [0] * t
        for i in range(t):
            acc = 0
            row = M[i]
            for j in range(t):
                acc = (acc + row[j] * state[j]) % F_R
            new[i] = acc
        state = new
    return state[0]


__all__ = ["poseidon", "F_R"]
