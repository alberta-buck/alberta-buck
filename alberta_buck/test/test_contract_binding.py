# SPDX-License-Identifier: GPL-3.0-or-later

import random

from alberta_buck.wallet.bn254 import G1, mul
from alberta_buck.wallet.contract_binding import (
    contract_binding_prove,
    contract_binding_verify,
)


def test_contract_binding_authorization_pins_context_and_policy():
    sk = 0x12345
    pk = mul(G1, sk)
    rng = random.Random(7)
    proof = contract_binding_prove(
        sk, pk, target=0xCAFE, binder=0xB1AD, registry=0xAE61,
        is_public_identity=True, is_carrying=True, chainid=31337,
        rng=lambda: rng.getrandbits(256),
    )

    verify = lambda **changes: contract_binding_verify(
        pk,
        target=changes.get("target", 0xCAFE),
        binder=changes.get("binder", 0xB1AD),
        registry=changes.get("registry", 0xAE61),
        proof=proof,
        is_public_identity=changes.get("is_public_identity", True),
        is_carrying=changes.get("is_carrying", True),
        chainid=changes.get("chainid", 31337),
    )
    assert verify()
    assert not verify(target=0xBAD)
    assert not verify(binder=0xBAD)
    assert not verify(registry=0xBAD)
    assert not verify(is_public_identity=False)
    assert not verify(is_carrying=False)
    assert not verify(chainid=1)
