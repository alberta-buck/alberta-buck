"""Existing household setup and SDK credit books; decisions remain gated."""
from dataclasses import replace

import pytest

from alberta_buck.sim import experiment as expmod, identity as idmod
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.credit_households import CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent
from alberta_buck.sim.deploy import deploy
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING

RAW = 10**6
Q = 10**36
YEAR = 365 * 86400 + 6 * 3600


def test_primary_household_setup_and_per_facility_credit_marks(monkeypatch):
    monkeypatch.delenv("SIM_WHEEL_ARB", raising=False)
    scenario = replace(ROUTING, tokens=ROUTING.tokens[:2], csv_files=ROUTING.csv_files[:2], agents={})
    scenario.experiment = expmod.load(None, sets=["deploy.target_buck_m=1", "deploy.target_buck_lp_m=1",
                                                 "machine.desk_capital_usd=1000"])
    rng = idmod.seeded_rng(scenario.seed)
    with PyrevmAnvil(port=0) as evm:
        chain = Chain(evm.w3, evm.w3.eth.accounts[0])
        d = deploy(chain, evm, scenario, rng, verbose=False, basket_impl="equity-ops", equity_machine=True)
        agents = []
        for i, cls in enumerate((CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent)):
            a = cls(i)
            a.settings = dict(home_k=1000, premium_bp=100)
            a.setup(d, scenario, rng)
            agents.append(a)
            assert d.reg.functions.isVerified(a.address).call() and not d.reg.functions.isCarrying(a.address).call()
            assert len(a._token_ids) == (0 if a.investor else 4)
            for tid in a._token_ids:
                assert d.credit.functions.ownerOf(tid).call() == a.address
                assert d.credit.functions.insurerOf(tid).call() == chain.deployer
                assert d.credit.functions.creditInfo(tid).call()[1] == 0
                assert d.credit.functions.issuanceOf(tid).call() == [0, 0]
            book = d.net_credit_ops.mark_household(a.address, a._token_ids)
            assert book.state.issued == book.state.spendable == book.premium_net_scaled == book.relief_scaled == 0
            assert book.credit_limit == 0
            with pytest.raises(NotImplementedError, match="decisions await"):
                a.act(d, scenario, 0, 0, {})
            assert not a._started and a.hypo_cash == 0

        debtor, saver, investor = agents
        chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi(
            "transfer", args=[debtor.address, 20 * RAW])))
        first, second = debtor._token_ids[:2]
        d.net_credit_ops.issue(debtor.address, first, 100 * RAW, 40 * RAW, 10 * RAW, d.pool_acct)
        for receiver, amount in ((saver, 5 * RAW), (investor, 7 * RAW)):
            debtor._proxy_exec(d, d.buck.address, d.buck.encode_abi("transfer", args=[receiver.address, amount]))
            received = d.net_credit_ops.mark_household(receiver.address, receiver._token_ids)
            assert received.state.spendable == amount and received.state.issued == 0
        assert d.net_credit_ops.mark_household(debtor.address, debtor._token_ids).state.issued == 50 * RAW

        # An insurer may replace its future pool; already funded facilities
        # retain their own pool. The old pool is aged, the new one fresh.
        evm.warp_to(d.w3.eth.get_block("latest")["timestamp"] + YEAR)
        new_pool = chain.deploy("SimLP")
        idmod.bind_as_operator(chain, d.reg, new_pool.address, True, True)
        chain.send(d.buck.functions.setInsurerPool(new_pool.address))
        chain.send(new_pool.functions.exec(d.buck.address, d.buck.encode_abi(
            "approve", args=[d.buck.address, 2**256 - 1])))
        d.net_credit_ops.issue(debtor.address, second, 100 * RAW, 40 * RAW, 10 * RAW, new_pool.address)
        marked = d.net_credit_ops.mark_household(debtor.address, debtor._token_ids)
        assert marked.state.issued == 100 * RAW and marked.state.premium_nominal == 20 * RAW
        pools = [d.buck.functions.facilityPool(tid).call() for tid in (first, second)]
        assert pools == [d.pool_acct, new_pool.address]
        values = [d.buck.functions.accountBalancesScaled(pool).call() for pool in pools]
        expected = sum(10 * RAW * Q * net // gross for gross, net in values)
        assert marked.premium_net_scaled == expected < 20 * RAW * Q
        assert values[0][1] < values[0][0] and values[1][1] == values[1][0]
        relief = sum(d.net_stack.fund.functions.previewRelief(tid, 50 * RAW).call()[0] for tid in (first, second))
        assert marked.relief_scaled == relief > 0
        assert marked.exceeds_liquidity_limit == (relief > marked.available_relief_scaled)
        assert marked.block_number == d.w3.eth.block_number
        with pytest.raises(ValueError, match="foreign"):
            d.net_credit_ops.mark_household(saver.address, debtor._token_ids)
        with pytest.raises(ValueError, match="duplicate"):
            d.net_credit_ops.mark_household(debtor.address, [first, first])
        deadline = d.w3.eth.get_block("latest")["timestamp"] + 3600
        d.net_credit_ops.authorize_reduction(debtor.address, first, RAW, 0, deadline)
        debtor.act(d, scenario, 0, 0, {})
        assert debtor.last_action == "repay-pending" and not debtor._started
        d.net_credit_ops.cancel_reduction(debtor.address, first)
        assert not chain.mismatches
