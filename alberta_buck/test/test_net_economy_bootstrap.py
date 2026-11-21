"""Existing Python builder and bootstrap agents on canonical Buck.

This checks deployment, real V3 opening and selected premium-bearing credit;
it does not remove the household lifecycle's explicit migration gates.
"""
from dataclasses import replace
import json

import pytest

from web3 import Web3

from alberta_buck.sim import experiment as expmod, identity as idmod
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.deploy import deploy, equity_arb_on
from alberta_buck.sim.direct_mint import BootstrapDMAgent
from alberta_buck.sim.loop import turn_wheel
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING
from buck_core import buck_math
from buck_core.session import Journal

Q = 10**36
RAW = 10**6


def test_existing_python_machine_bootstraps_actual_cash_principal_and_premium(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_WHEEL_ARB", "0")
    BootstrapDMAgent._counter = 0
    scenario = replace(ROUTING, tokens=[(*ROUTING.tokens[0][:3], 7500), (*ROUTING.tokens[1][:3], 2500)],
                       csv_files=ROUTING.csv_files[:2], days=1, agents={"BootstrapDMAgent": 2})
    scenario.experiment = expmod.load(None, sets=["deploy.target_buck_m=1", "deploy.target_buck_lp_m=1",
                                                 "machine.desk_capital_usd=1000"])
    rng = idmod.seeded_rng(scenario.seed)
    with PyrevmAnvil(port=0) as evm:
        journal_path = tmp_path / "bootstrap.jsonl"
        chain = Chain(evm.w3, evm.w3.eth.accounts[0], journal=Journal(journal_path))
        d = deploy(chain, evm, scenario, rng, verbose=False, basket_impl="equity-ops", equity_machine=True)
        buck, credit, basket, fund, wheel = d.buck, d.credit, d.basket, d.net_stack.fund, d.wheel
        assert d.buck_accounting == "net"
        assert buck.functions.ACCOUNTING_MODEL().call() == Web3.keccak(
            text="Buck/compounded-cash/per-facility-jubilee/v1")
        assert d.net_stack.token.address == buck.address and d.net_basket.basket.address == basket.address
        assert fund.functions.basket().call() == basket.address and buck.functions.jubilee().call() == fund.address
        assert fund.functions.valuationWheel().call() == fund.functions.investmentKeeper().call() == wheel.address
        assert d.director is None and d.equity_director is None
        assert d.equity_upkeep is not None and d.pair_policy is not None
        assert d.desk.functions.netAccounting().call()
        assert d.desk.functions.executionGuard().call() == d.monetary_policy.address
        assert d.reg.functions.isCarrying(wheel.address).call()
        assert d.reg.functions.carryingFrozen(wheel.address).call()
        assert buck.functions.allowance(chain.deployer, wheel.address).call() == 0
        assert buck.functions.receiptFragment(chain.deployer, wheel.address).call() != bytes(32)
        equity_arb_on(d)
        assert wheel.functions.triangleCount().call() == 0
        monkeypatch.setenv("SIM_WHEEL_ARB", "1")
        equity_arb_on(d)
        assert wheel.functions.triangleCount().call() == 2
        assert wheel.functions.arbRouteCount().call() == 3
        assert d.reg.functions.isCarrying(basket.address).call()
        assert buck.functions.approvedCarryingIssuer(basket.address).call()

        seeders = [BootstrapDMAgent(i) for i in range(2)]
        counters = {}
        reference_slots = [chain.at("UniswapV3Pool", p).functions.slot0().call()[:2] for p in d.pool_usdc]
        for agent in seeders:
            agent.setup(d, scenario, rng)
            agent.bootstrap(d, scenario, counters)
            assert agent._receipt_id is not None
        assert [chain.at("UniswapV3Pool", p).functions.slot0().call()[:2] for p in d.pool_usdc] == reference_slots
        for _ in range(8):
            evm.warp_to(d.w3.eth.get_block("latest")["timestamp"] + 1801)
            turn_wheel(d, rounds=1)
            if all(basket.functions.liquidityOf(i).call() for i in range(2)):
                break
        assert all(basket.functions.liquidityOf(i).call() > 0 for i in range(2))
        assert abs(basket.functions.responseWeightsBp().call()[0] - 7500) < 300
        assert basket.functions.lien().call() > 0
        total = sum(credit.functions.issuanceOf(i).call()[0] for i in range(credit.functions.totalSupply().call()))
        assert buck.functions.totalIssued().call() == total
        assert len(d.bootstrap_pool_meta) == 2
        deployed = [row["tag"] for row in map(json.loads, journal_path.read_text().splitlines())
                    if row["op"] == "deploy"]
        assert all(deployed.count(f"deploy:{name}") == 1 for name in
                   ("Buck", "JubileeFund", "BuckBasketEquity", "BasketMachineWheel"))
        assert not set(deployed) & {f"deploy:{name}" for name in
                                    ("PairsRebalanceDirector", "EquityTurnDirector", "BuckBasketEquityWheel", "BasketWheel")}

        # The actual insurer's pool accepts a full-value ten-BUCK premium.
        proxy = chain.deploy("SimLP")
        idmod.bind_as_operator(chain, d.reg, proxy.address, True, False)
        chain.send(proxy.functions.exec(credit.address, credit.encode_abi(
            "setCreditIssuer", args=[chain.deployer, True])))
        token_id = credit.functions.totalSupply().call()
        chain.send(credit.functions.createCredit(proxy.address, 0, 100 * RAW, 0, 0, 0,
                                                 d.w3.eth.get_block("latest")["timestamp"], 100))
        assert credit.functions.insurerOf(token_id).call() == chain.deployer
        chain.send(d.simlp.functions.exec(buck.address, buck.encode_abi(
            "transfer(address,uint256)", args=[proxy.address, 20 * RAW])))
        before_net = buck.functions.accountBalancesScaled(proxy.address).call()[1]
        before_time = d.w3.eth.get_block("latest")["timestamp"]
        supply = buck.functions.totalIssued().call()
        evm.warp_to(before_time + 86400)
        issued = d.net_credit_ops.issue(proxy.address, token_id, 100 * RAW, 40 * RAW, 10 * RAW, d.pool_acct)
        after = d.net_credit_ops.household(proxy.address, [token_id])
        issue_time = d.w3.eth.get_block(issued["blockNumber"])["timestamp"]
        expected_net = buck_math.net_decay(before_net, issue_time - before_time) + 40 * RAW * Q
        assert buck.functions.accountBalancesScaled(proxy.address).call()[1] == expected_net
        assert after.spendable == expected_net // Q and after.issued == 50 * RAW
        assert after.premium_nominal == 10 * RAW
        assert buck.functions.totalIssued().call() == supply + 50 * RAW
        assert buck.functions.facilityPool(token_id).call() == d.pool_acct

        # Use real invested capital, then the same BUCK sale/USDC withdrawal
        # as a refinancing household. The registered machine must execute
        # actual funded arb through bounded transactions, not an event stub.
        capital = 20_000 * RAW
        chain.send(d.simlp.functions.exec(buck.address, buck.encode_abi(
            "approve(address,uint256)", args=[basket.address, capital])))
        chain.send(d.simlp.functions.exec(basket.address, basket.encode_abi(
            "deposit", args=[buck.address, capital, 1])))
        ub = chain.at("UniswapV3Pool", d.pool_ub)
        b0 = ub.functions.token0().call() == buck.address
        chain.send(d.simlp.functions.swap(d.pool_ub, d.simlp.address, b0, 10_000 * RAW,
            4295128740 if b0 else 1461446703485210103287273052203988822378723970341,
            ub.functions.token0().call(), ub.functions.token1().call()))
        before_price = ub.functions.slot0().call()[0]
        counters = {}
        turn_wheel(d, rounds=2, ctr=counters)
        assert int(counters.get("wh_arb_cycles", 0)) > 0
        assert int(counters["wh_arb_gain_net_raw"]) > 0
        after_price = ub.functions.slot0().call()[0]
        assert after_price > before_price if b0 else after_price < before_price
        assert len(d.last_wheel_receipts) > 1
        from web3.logs import DISCARD
        for receipt in d.last_wheel_receipts:
            assert int(receipt["gasUsed"]) < 12_000_000
            tick = wheel.events.Ticked().process_receipt(receipt, errors=DISCARD)[0]
            assert tick["args"]["work"] <= 1


@pytest.mark.parametrize("basket_impl", ["equity", "equity-ops"])
def test_component_comparisons_keep_their_own_wheel_on_canonical_money(basket_impl, monkeypatch):
    monkeypatch.setenv("SIM_EQUITY_MACHINE", "0")     # the machine is the default; the earlier upkeep goes in R4
    monkeypatch.delenv("SIM_WHEEL_ARB", raising=False)
    scenario = replace(ROUTING, tokens=ROUTING.tokens[:2], csv_files=ROUTING.csv_files[:2], days=1, agents={})
    rng = idmod.seeded_rng(scenario.seed)
    with PyrevmAnvil(port=0) as evm:
        chain = Chain(evm.w3, evm.w3.eth.accounts[0])
        d = deploy(chain, evm, scenario, rng, verbose=False, basket_impl=basket_impl)
        assert d.buck_accounting == "net" and not d.equity_machine
        assert d.basket.functions.netAccounting().call()
        assert not d.wheel.functions.responseMode().call()
        assert d.net_stack.fund.functions.valuationWheel().call() == d.wheel.address
        assert d.credit.functions.issuanceOf(d.basket.functions.creditId().call()).call()[0] == 0
        assert d.equity_upkeep is not None and d.equity_director is not None
        assert bool(d.desk) == (basket_impl == "equity-ops")
        if d.desk is not None:
            assert d.desk.functions.netAccounting().call()
