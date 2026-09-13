# Run: PYTHONPATH=. python scripts/review/evm_uncertified_bind.py
"""Review finding 4, executed on a real EVM (in-process revm):
IdentityRegistry.bindContract stores caller-supplied (pk, E, identityLeaf)
with no PS signature, no NIZK, and no check that the leaf hashes the
identity encrypted in E.  register() likewise inserts a caller-chosen leaf.

Honest control: a leaf of 0 skips the tree update.
"""
from web3 import Web3

from alberta_buck.sim.pyrevm_backend import PyrevmAnvil, DEV_ACCOUNTS
from alberta_buck.sim.chain import Chain
from alberta_buck.review.examples import Account, seeded
from alberta_buck.wallet.bn254 import point_to_words
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_rerandomize
from alberta_buck.wallet.nizk import registration_prove
from alberta_buck.registry.tree import identity_leaf

g1 = lambda P: tuple(point_to_words(P))
g2 = lambda P: ((int(P[0].coeffs[0]), int(P[0].coeffs[1])),
                (int(P[1].coeffs[0]), int(P[1].coeffs[1])))


def deploy_poseidon(chain):
    import re
    from alberta_buck.sim.chain import repo_root
    src = (repo_root() / "src" / "PoseidonT3Bytecode.sol").read_text()
    code = re.search(r'hex"([0-9a-fA-F]+)"', src).group(1)
    tx = chain.w3.eth.send_transaction({
        "from": chain.deployer, "data": "0x" + code,
        "gas": 10_000_000, "gasPrice": 0,
    })
    rcpt = chain.w3.eth.wait_for_transaction_receipt(tx)
    assert rcpt["status"] == 1
    return rcpt["contractAddress"]


with PyrevmAnvil(chain_id=1, auto_impersonate=True) as anvil:
    chain = Chain(anvil.w3, DEV_ACCOUNTS[0])
    gov = DEV_ACCOUNTS[0]
    reg = chain.deploy("IdentityRegistry", gov)
    poseidon = deploy_poseidon(chain)
    chain.send(reg.functions.setIdentityPoseidon(poseidon), sender=gov)

    # A dummy contract target (bindContract requires code).
    target = Web3.to_checksum_address("0x000000000000000000000000000000000000c0de")
    anvil.set_code(target, "0x60006000fd")
    anvil.set_balance(target, 10**18)

    bogus = Account(999, 111, 222)
    junk_leaf = 12345
    assert junk_leaf != identity_leaf(bogus.M)
    bind6 = reg.get_function_by_signature(
        "bindContract(address,(uint256,uint256),"
        "((uint256,uint256),(uint256,uint256)),bool,bool,uint256)")
    chain.send(bind6(target, g1(bogus.pk), (g1(bogus.E.R), g1(bogus.E.C)),
                     True, False, junk_leaf), sender=gov)
    assert reg.functions.isVerified(target).call()
    root_after_bind = reg.functions.identityRoot().call()
    assert root_after_bind != 0
    print("bindContract accepted fabricated (pk, E) and unrelated leaf", junk_leaf)
    print("   isVerified(target) =", True, "; identityRoot updated =", hex(root_after_bind))

    # register() with a mismatched leaf: real PS credential, junk leaf.
    rng = seeded(9)
    iss = ps_keygen(rng=rng)
    iss_addr = Web3.to_checksum_address("0x00000000000000000000000000000000000000aa")
    chain.send(reg.functions.trustIssuer(iss_addr, (g2(iss.pk_X), g2(iss.pk_Y))),
               sender=gov)
    alice_addr = Web3.to_checksum_address("0x000000000000000000000000000000000000a11c")
    anvil.set_balance(alice_addr, 10**18)
    alice = Account(12345, 45678, 98765)
    sigma, _ = ps_rerandomize(ps_sign(iss, alice.m, rng=rng), rng=rng)
    pf = registration_prove(sigma, alice.m, alice.r, alice.pk, alice.E,
                            int(alice_addr, 16), alice.sk, rng=rng)
    honest_leaf = identity_leaf(alice.M)
    fake_leaf = honest_leaf ^ 1
    reg6 = reg.get_function_by_signature(
        "register(address,(uint256,uint256),((uint256,uint256),(uint256,uint256)),"
        "((uint256,uint256),(uint256,uint256)),"
        "(uint256,uint256,uint256,uint256,(uint256,uint256),(uint256,uint256),"
        "(uint256,uint256),(uint256,uint256)),"
        "uint256)")
    chain.send(reg6(
        iss_addr, g1(alice.pk), (g1(alice.E.R), g1(alice.E.C)),
        (g1(sigma.sigma_1), g1(sigma.sigma_2)),
        (pf.e, pf.s_m, pf.s_r, pf.s_sk, g1(pf.A_ps), g1(pf.T_C), g1(pf.T_R),
         g1(pf.T_key)),
        fake_leaf), sender=alice_addr)
    assert reg.functions.isVerified(alice_addr).call()
    print("register() accepted a real credential with leaf != Poseidon(M)")
    print("   honest leaf", honest_leaf)
    print("   stored leaf (caller-supplied)", fake_leaf)
    print("   identityRoot moved again =",
          hex(reg.functions.identityRoot().call()) != hex(root_after_bind))

    print("\nRESULT: finding 4 reproduced on EVM "
          "(uncertified bind + unmatched register leaf).")
