# Run: PYTHONPATH=. python scripts/review/evm_uncontrolled_register.py
"""Review finding 9 inverted: on a real EVM (in-process revm)
IdentityRegistry.register REJECTS a NIZK for a NUMS public key with no
known sk (the attacker encrypts mG to a point they do not hold).

Honest control: the same credential under a key the registrant holds
still registers (different address).
"""
from web3 import Web3

from alberta_buck.sim.pyrevm_backend import PyrevmAnvil, DEV_ACCOUNTS
from alberta_buck.sim.chain import Chain
from alberta_buck.review.examples import Account, seeded, uncontrolled_registration
from alberta_buck.wallet.bn254 import G1, mul, eq, point_to_words
from alberta_buck.wallet.ps import ps_sign, ps_present
from alberta_buck.wallet.nizk import registration_prove
from alberta_buck.wallet.elgamal import elgamal_decrypt

g1 = lambda P: tuple(point_to_words(P))
g2 = lambda P: ((int(P[0].coeffs[0]), int(P[0].coeffs[1])),
                (int(P[1].coeffs[0]), int(P[1].coeffs[1])))


with PyrevmAnvil(chain_id=1, auto_impersonate=True) as anvil:
    chain = Chain(anvil.w3, DEV_ACCOUNTS[0])
    gov = DEV_ACCOUNTS[0]
    reg = chain.deploy("IdentityRegistry", gov)

    attacker = Web3.to_checksum_address("0x0000000000000000000000000000000000000bad")
    anvil.set_balance(attacker, 10**18)
    registry = int(reg.address, 16)
    issuer, owner, pk, E, pres, proof = uncontrolled_registration(
        int(attacker, 16), registry,
    )
    assert not eq(pk, mul(G1, owner.sk))

    iss_addr = Web3.to_checksum_address("0x00000000000000000000000000000000000000aa")
    chain.send(reg.functions.trustIssuer(
        iss_addr, (g2(issuer.pk_X), g2(issuer.pk_Y), g1(issuer.pk_Y1))),
               sender=gov)
    nums_ok = True
    try:
        chain.send(reg.functions.register(
            iss_addr, g1(pk), (g1(E.R), g1(E.C)),
            (g1(pres.A), g1(pres.B)),
            (proof.e, proof.s_m, proof.s_b, proof.s_r, proof.s_sk, g1(proof.C1),
             g1(proof.T_C), g1(proof.T_R), g1(proof.T_key))),
            sender=attacker)
    except Exception:
        nums_ok = False
    assert not nums_ok
    assert not reg.functions.isVerified(attacker).call()
    print("register() rejected NUMS pk with no known sk")
    print("   isVerified(attacker) =", False)

    # Honest control on a second address.
    honest = Web3.to_checksum_address("0x000000000000000000000000000000000000a11c")
    anvil.set_balance(honest, 10**18)
    alice = Account(12345, 45678, 98765)
    rng = seeded(11)
    pres_h, _a, b_h = ps_present(ps_sign(issuer, alice.m, rng=rng), issuer.pk_Y1, rng=rng)
    pf = registration_prove(pres_h, b_h, alice.m, alice.r, alice.pk, alice.E,
                            int(honest, 16), alice.sk, rng=rng,
                            registry=registry)
    chain.send(reg.functions.register(
        iss_addr, g1(alice.pk), (g1(alice.E.R), g1(alice.E.C)),
        (g1(pres_h.A), g1(pres_h.B)),
        (pf.e, pf.s_m, pf.s_b, pf.s_r, pf.s_sk, g1(pf.C1), g1(pf.T_C), g1(pf.T_R),
         g1(pf.T_key))),
        sender=honest)
    assert reg.functions.isVerified(honest).call()
    print("HONEST register under a held key accepted =", True)

    print("\nRESULT: finding 9 inverted on EVM "
          "(NUMS registration rejected; honest register accepted).")
