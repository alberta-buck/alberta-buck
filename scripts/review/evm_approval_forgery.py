# Run: PYTHONPATH=. python scripts/review/evm_approval_forgery.py  -- review evidence; see doc/review/identity-findings.md
"""Review finding 3, executed on a real EVM (in-process revm via PyrevmAnvil):
the deployed IdentityRegistry._verifyApprove accepts a Chaum-Pedersen approval
whose witness is NOT the sender's registered account key, so Bob decrypts the
approval to a THIRD party's identity, not the sender's.

Honest control included.  No production source is modified; the deployed
bytecode is the committed out/IdentityRegistry.sol artifact.
"""
import sys

from alberta_buck.sim.pyrevm_backend import PyrevmAnvil, DEV_ACCOUNTS
from alberta_buck.sim.chain import Chain
from alberta_buck.review.examples import Account, false_identity_approval, seeded
from alberta_buck.wallet.bn254 import G1, mul, point_to_words
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_rerandomize
from alberta_buck.wallet.nizk import registration_prove
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_prove
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.bn254 import eq

g1 = lambda P: tuple(point_to_words(P))
g2 = lambda P: ((int(P[0].coeffs[0]), int(P[0].coeffs[1])),
                (int(P[1].coeffs[0]), int(P[1].coeffs[1])))


def register(chain, reg, issuer_addr, acct, eoa, rng):
    sigma = ps_sign(ISS, acct.m, rng=rng)
    sig_p, _ = ps_rerandomize(sigma, rng=rng)
    pf = registration_prove(sig_p, acct.m, acct.r, acct.pk, acct.E, int(eoa, 16), rng=rng)
    fn = reg.functions.register(
        issuer_addr, g1(acct.pk), (g1(acct.E.R), g1(acct.E.C)),
        (g1(sig_p.sigma_1), g1(sig_p.sigma_2)),
        (pf.e, pf.s_m, pf.s_r, g1(pf.A_ps), g1(pf.T_C), g1(pf.T_R)))
    chain.send(fn, sender=eoa)


with PyrevmAnvil(chain_id=1, auto_impersonate=True) as anvil:
    chain = Chain(anvil.w3, DEV_ACCOUNTS[0])
    gov = DEV_ACCOUNTS[0]
    reg = chain.deploy("IdentityRegistry", gov)

    rng = seeded(999)
    global ISS
    ISS = ps_keygen(rng=rng)
    from web3 import Web3 as _W3
    iss_addr = _W3.to_checksum_address("0x00000000000000000000000000000000000000aa")
    chain.send(reg.functions.trustIssuer(iss_addr, (g2(ISS.pk_X), g2(ISS.pk_Y))), sender=gov)

    # Rebuild the counterexample bound to THIS chain's ids/addresses.
    alice_addr = _W3.to_checksum_address("0x000000000000000000000000000000000000a11c")
    bob_addr   = _W3.to_checksum_address("0x000000000000000000000000000000000000b0b0")
    A, B = int(alice_addr, 16), int(bob_addr, 16)
    alice, bob, victim, fake_sk, rp, forged, forged_cp = false_identity_approval(A, B, 1)

    register(chain, reg, iss_addr, alice, alice_addr, seeded(1))
    register(chain, reg, iss_addr, bob,   bob_addr,   seeded(2))
    assert reg.functions.isVerified(alice_addr).call()
    assert reg.functions.isVerified(bob_addr).call()
    print("registered alice, bob with a real PS-signed KYC credential")

    e_t = (g1(forged.R), g1(forged.C))
    cp_t = (forged_cp.e, forged_cp.s1, forged_cp.s2,
            g1(forged_cp.T1), g1(forged_cp.T2), g1(forged_cp.T3))
    accepted = reg.functions.verifyApprove(alice_addr, bob_addr, e_t, cp_t).call()
    print("FORGED   approval accepted by on-chain _verifyApprove :", accepted)
    print("   fake witness s*G == alice.pk ?                    :", eq(mul(G1, fake_sk), alice.pk))
    print("   Bob decrypts forged approval to victim_m*G ?      :",
          eq(elgamal_decrypt(forged, bob.sk), mul(G1, victim)))
    print("   ... and that is NOT alice's identity M ?          :",
          not eq(elgamal_decrypt(forged, bob.sk), alice.M))

    # Honest control: alice re-encrypts her REAL M to bob with her real sk.
    honest_ct = elgamal_encrypt(alice.M, bob.pk, rp)
    hp = chaum_pedersen_prove(alice.E, honest_ct, alice.pk, bob.pk,
                              alice.sk, rp, A, B, 1, rng=seeded(7))
    he_t = (g1(honest_ct.R), g1(honest_ct.C))
    hcp_t = (hp.e, hp.s1, hp.s2, g1(hp.T1), g1(hp.T2), g1(hp.T3))
    honest_ok = reg.functions.verifyApprove(alice_addr, bob_addr, he_t, hcp_t).call()
    print("HONEST   approval accepted                           :", honest_ok,
          "; decrypts to alice.M :", eq(elgamal_decrypt(honest_ct, bob.sk), alice.M))

    assert accepted and honest_ok, "finding 3 not reproduced"
    print("\nRESULT: the deployed verifier accepts a false-identity approval "
          "(finding 3 reproduced on EVM).")
