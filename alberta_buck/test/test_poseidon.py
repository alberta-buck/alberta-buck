"""Round-trip tests for alberta_buck.wallet.poseidon against circomlibjs.

The reference vectors below were emitted by `circomlibjs.buildPoseidon()`
(BN254, unoptimized variant -- mathematically equivalent to the optimized
form used in circomlib's circuit Poseidon and to the on-chain bytecode
produced by `poseidonContract.createCode(t-1)`).  If circomlibjs ever bumps
its constants, regenerate via::

    node -e "const cjs = require('circomlibjs'); (async () => {
      const p = await cjs.buildPoseidon();
      const F = p.F;
      const cases = [[1n,2n], [1n], [1n,2n,3n], [1n,2n,3n,4n,5n]];
      for (const c of cases) console.log(JSON.stringify(c.map(String)),
        '0x' + F.toObject(p(c)).toString(16));
    })();"

Bit-level agreement with circomlibjs is the *only* invariant that lets the
Python witness emitter feed values into snarkjs prove and have the prover
accept the witness; any drift here surfaces as a circom witness mismatch.
"""

from __future__ import annotations

import pytest

from alberta_buck.wallet.poseidon import F_R, poseidon


# ---- known-answer tests (KAT) emitted by circomlibjs ---------------------

POSEIDON_VECTORS = [
    # (inputs, expected output as hex)
    ([1, 2],
     0x115cc0f5e7d690413df64c6b9662e9cf2a3617f2743245519e19607a4417189a),
    ([1],
     0x29176100eaa962bdc1fe6c654d6a3c130e96a4d1168b33848b897dc502820133),
    ([1, 2, 3],
     0x0e7732d89e6939c0ff03d5e58dab6302f3230e269dc5b968f725df34ab36d732),
    ([1, 2, 3, 4, 5],
     0x0dab9449e4a1398a15224c0b15a49d598b2174d305a316c918125f8feeb123c0),
    ([42, 0, 4242],
     0x0fe9d6416a770e90bd8455d8e6cdcbe3348dc6aa5b4f1aad25f8aa0262adbb06),
    ([123456789, 987654321],
     0x2536d01521137bf7b39e3fd26c1376f456ce46a45993a5d7c3c158a450fd7329),
]


@pytest.mark.parametrize("inputs,expected", POSEIDON_VECTORS)
def test_matches_circomlibjs(inputs, expected):
    assert poseidon(inputs) == expected


def test_output_is_in_field():
    for inputs, _ in POSEIDON_VECTORS:
        h = poseidon(inputs)
        assert 0 <= h < F_R


def test_inputs_reduced_mod_field():
    """poseidon(x) == poseidon(x + F_R) since circom signals reduce mod r."""
    base = poseidon([1, 2])
    shifted = poseidon([1 + F_R, 2])
    assert base == shifted


def test_arity_bounds():
    with pytest.raises(ValueError):
        poseidon([])  # arity 0 not supported
    with pytest.raises(ValueError):
        poseidon(list(range(17)))  # arity > 16 not supported


def test_2_3_5_arities_round_trip_themselves():
    """The three arities the BUCK Notes circuits actually use:
       - Poseidon-3 (Merkle nodes, B-shape Merkle tree)
       - Poseidon-3 (nullifiers: tag, rho, id_hash)
       - Poseidon-6 (note commitments: tag, flavor, v, rho, id_hash, predicate)
    """
    # Different inputs yield different hashes.
    assert poseidon([1, 2]) != poseidon([2, 1])
    assert poseidon([1, 2, 3]) != poseidon([3, 2, 1])
    assert poseidon([1, 2, 3, 4, 5]) != poseidon([5, 4, 3, 2, 1])
