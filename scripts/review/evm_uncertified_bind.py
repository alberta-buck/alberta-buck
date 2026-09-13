# Run: PYTHONPATH=. python scripts/review/evm_uncertified_bind.py
"""Review finding 4 inverted, executed on a real EVM (in-process revm):
IdentityRegistry.bindContract refuses fabricated (pk, E) and unconstrained
identityLeaf.  register() refuses a caller-chosen leaf.

Honest controls: register with leaf=0; bind with a target-bound credential;
certified-operator bind of a registered binder's own (pk, E).
"""
from web3 import Web3

from alberta_buck.sim.pyrevm_backend import PyrevmAnvil, DEV_ACCOUNTS
from alberta_buck.sim.chain import Chain, Expect
from alberta_buck.review.examples import Account, seeded
from alberta_buck.wallet.bn254 import point_to_words
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_rerandomize
from alberta_buck.wallet.nizk import registration_prove, bind_contract_prove
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


def _proof_arg(pf):
    return (pf.e, pf.s_m, pf.s_r, pf.s_sk, g1(pf.A_ps), g1(pf.T_C), g1(pf.T_R),
            g1(pf.T_key))


with PyrevmAnvil(chain_id=1, auto_impersonate=True) as anvil:
    chain = Chain(anvil.w3, DEV_ACCOUNTS[0])
    gov = DEV_ACCOUNTS[0]
    reg = chain.deploy("IdentityRegistry", gov)
    poseidon = deploy_poseidon(chain)
    chain.send(reg.functions.setIdentityPoseidon(poseidon), sender=gov)

    target = Web3.to_checksum_address("0x000000000000000000000000000000000000c0de")
    anvil.set_code(target, "0x60006000fd")
    anvil.set_balance(target, 10**18)

    bogus = Account(999, 111, 222)
    junk_leaf = 12345
    assert junk_leaf != identity_leaf(bogus.M)
    bind5 = reg.get_function_by_signature(
        "bindContract(address,(uint256,uint256),"
        "((uint256,uint256),(uint256,uint256)),bool,bool)")
    bind6 = reg.get_function_by_signature(
        "bindContract(address,(uint256,uint256),"
        "((uint256,uint256),(uint256,uint256)),bool,bool,uint256)")

    chain.send(bind5(target, g1(bogus.pk), (g1(bogus.E.R), g1(bogus.E.C)),
                     True, False), sender=gov, expect=Expect.REVERT)
    assert "binder not registered" in (chain.last_revert_reason or "")
    assert not reg.functions.isVerified(target).call()
    print("5-arg bindContract from unregistered sender reverted:",
          chain.last_revert_reason)

    chain.send(bind6(target, g1(bogus.pk), (g1(bogus.E.R), g1(bogus.E.C)),
                     True, False, junk_leaf), sender=gov, expect=Expect.REVERT)
    assert "unchecked identity leaf" in (chain.last_revert_reason or "")
    assert not reg.functions.isVerified(target).call()
    assert reg.functions.identityRoot().call() == 0
    print("6-arg bindContract rejected unrelated leaf", junk_leaf)
    print("   isVerified(target) =", False, "; identityRoot still 0")

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
                            int(alice_addr, 16), alice.sk, rng=rng,
                            registry=int(reg.address, 16))
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
        _proof_arg(pf),
        fake_leaf), sender=alice_addr, expect=Expect.REVERT)
    assert "unchecked identity leaf" in (chain.last_revert_reason or "")
    assert not reg.functions.isVerified(alice_addr).call()
    print("register() rejected a real credential with leaf != Poseidon(M)")
    print("   honest leaf", honest_leaf)
    print("   refused leaf (caller-supplied)", fake_leaf)

    # Honest register with leaf=0.
    chain.send(reg6(
        iss_addr, g1(alice.pk), (g1(alice.E.R), g1(alice.E.C)),
        (g1(sigma.sigma_1), g1(sigma.sigma_2)),
        _proof_arg(pf),
        0), sender=alice_addr)
    assert reg.functions.isVerified(alice_addr).call()
    assert reg.functions.identityRoot().call() == 0
    print("HONEST register with leaf=0 accepted =", True)

    # A registered stranger still cannot claim an unrelated deployed target.
    pool = Web3.to_checksum_address("0x000000000000000000000000000000000000b00b")
    anvil.set_code(pool, "0x60006000fd")
    chain.send(bind5(pool, g1(alice.pk), (g1(alice.E.R), g1(alice.E.C)),
                     True, True), sender=alice_addr, expect=Expect.REVERT)
    assert "target did not authorize binding" in (chain.last_revert_reason or "")
    assert not reg.functions.isVerified(pool).call()
    print("REGISTERED stranger could not claim an unrelated target")

    # Honest certified-operator bind: the target explicitly authorizes alice's
    # exact identity and policy flags before alice copies her registered record.
    chain.send(reg.functions.authorizeContractBinding(
        alice_addr, g1(alice.pk), (g1(alice.E.R), g1(alice.E.C)), True, True
    ), sender=pool)
    chain.send(bind5(pool, g1(alice.pk), (g1(alice.E.R), g1(alice.E.C)),
                     True, True), sender=alice_addr)
    assert reg.functions.isVerified(pool).call()
    assert reg.functions.binderOf(pool).call() == alice_addr
    print("HONEST certified-operator bind accepted =", True)

    # Honest credential bind: independent identity, FS registrant = target.
    vault = Web3.to_checksum_address("0x00000000000000000000000000000000000000a2")
    anvil.set_code(vault, "0x60006000fd")
    vault_acct = Account(22222, 33333, 44444)
    sigma_v, _ = ps_rerandomize(ps_sign(iss, vault_acct.m, rng=rng), rng=rng)
    pf_v = bind_contract_prove(
        sigma_v, vault_acct.m, vault_acct.r, vault_acct.pk, vault_acct.E,
        int(vault, 16), vault_acct.sk, rng=rng)
    chain.send(reg.functions.authorizeContractBinding(
        alice_addr, g1(vault_acct.pk),
        (g1(vault_acct.E.R), g1(vault_acct.E.C)), False, False
    ), sender=vault)
    bind_cred = reg.get_function_by_signature(
        "bindContract(address,address,(uint256,uint256),"
        "((uint256,uint256),(uint256,uint256)),"
        "((uint256,uint256),(uint256,uint256)),"
        "(uint256,uint256,uint256,uint256,(uint256,uint256),(uint256,uint256),"
        "(uint256,uint256),(uint256,uint256)),"
        "bool,bool)")
    chain.send(bind_cred(
        vault, iss_addr, g1(vault_acct.pk),
        (g1(vault_acct.E.R), g1(vault_acct.E.C)),
        (g1(sigma_v.sigma_1), g1(sigma_v.sigma_2)),
        _proof_arg(pf_v),
        False, False), sender=alice_addr)
    assert reg.functions.isVerified(vault).call()
    print("HONEST credential bind (FS registrant = target) accepted =", True)

    print("\nRESULT: finding 4 inverted on EVM "
          "(uncertified bind + unmatched register leaf rejected; "
          "honest register/bind accepted).")
