"""Real-contract checks of carrying-cost refinancing and the two post-refinance household policies."""

from dataclasses import replace

import pytest

from alberta_buck.sim import experiment as expmod, identity as idmod
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.deploy import deploy
from alberta_buck.sim.direct_mint import BootstrapDMAgent
from alberta_buck.sim.equilibrium_agents import BuckCreditDebtorAgent
from alberta_buck.sim.household_agents import ExternalDebtRetireeAgent
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING

M6                              = 10 ** 6


@pytest.fixture(params=["prorata", "equity-ops"])
def households(request):
    scenario                    = replace(ROUTING, days=90, agents={"BuckCreditDebtorAgent": 2})
    scenario.experiment         = expmod.load(None, sets=[
        f"agents.BuckCreditDebtorAgent.{key}={value}" for key, value in dict(
            arrive_frac=0., refi_mode="carry", open="placed", face_k=2000, mortgage_k=800,
            premium_bp=100, pattern="salary", income_k=260, buffer_k=20, theta=3,
            surplus_policy="mixed").items()])
    rng                         = idmod.seeded_rng(scenario.seed)
    with PyrevmAnvil(port=0) as evm:
        chain                   = Chain(evm.w3, evm.w3.eth.accounts[0])
        d                       = deploy(chain, evm, scenario, rng, verbose=False, basket_impl=request.param)
        BootstrapDMAgent._counter = 0
        for i in range(len(d.tokens)):
            seeder              = BootstrapDMAgent(100 + i)
            seeder.setup(d, scenario, rng)
            seeder.bootstrap(d, scenario, {})
        if d.wheel is not None:
            from alberta_buck.sim.loop import turn_wheel
            turn_wheel(d, rounds=2)
        BuckCreditDebtorAgent._arrival_seq = 0
        agents                  = [BuckCreditDebtorAgent(i) for i in range(2)]
        for agent in agents:
            agent.setup(d, scenario, rng)
            agent.bootstrap(d, scenario, {})
            assert agent.mortgage <= M6 and agent.refis == 1
        yield d, agents


def test_even_split_and_no_amortization_without_a_discount(households, monkeypatch):
    d, (retirer, investor)       = households
    assert (retirer.surplus_policy, investor.surplus_policy) == ("discount", "basket")
    # Matching the old counterfactual would demand repayment of the whole liability.
    retirer.hypo_mortgage        = 0
    monkeypatch.setattr(retirer, "_basket_usd", lambda d: .90)
    before                      = d.buck.functions.signedBalanceOf(retirer.proxy.address).call()
    cash                        = d.chain.balance_of(d.usdc, retirer.proxy.address)
    retirer._carry_tail(d, -before, 0, 0, {})
    assert d.buck.functions.signedBalanceOf(retirer.proxy.address).call() == before
    assert d.chain.balance_of(d.usdc, retirer.proxy.address) == cash
    assert retirer.unwound == 0


def test_discount_retirement_reduces_lien_without_deactivating_credit(households, monkeypatch):
    d, (agent, _)               = households
    monkeypatch.setattr(agent, "_basket_usd", lambda d: 1.20)
    before                      = d.buck.functions.signedBalanceOf(agent.proxy.address).call()
    coverage                    = [d.credit.functions.creditInfo(t).call()[1] for t in agent._token_ids]
    ctr                         = {}
    agent._carry_tail(d, 0, 0, 0, ctr)  # stale opening reads must not suppress the new lien's retirement
    assert d.buck.functions.signedBalanceOf(agent.proxy.address).call() > before
    assert agent.unwound > 0 and ctr["bcdRetired"] > 0
    assert [d.credit.functions.creditInfo(t).call()[1] for t in agent._token_ids] == coverage
    assert d.chain.balance_of(d.usdc, agent.proxy.address) >= agent.cash_buffer - 2


def test_investment_repeats_and_does_not_pay_down_the_buck_liability(households):
    d, (_, agent)               = households
    before                      = d.buck.functions.signedBalanceOf(agent.proxy.address).call()
    agent.hypo_mortgage          = 0
    ctr                         = {}
    for _ in range(2):
        d.chain.send(d.usdc.functions.mint(agent.proxy.address, 50_000 * M6))
        agent._carry_tail(d, -before, 0, 0, ctr)
    assert not ctr.get("bcd_invest_err"), ctr
    assert len(agent._basket_receipts) == 2 and ctr["bcdInvestments"] == 2
    assert agent._basket_in > 0 and agent.unwound == 0
    assert d.buck.functions.signedBalanceOf(agent.proxy.address).call() == before
    st                          = agent.octl_state(d)
    assert st["basket"] == agent._basket_in and st["basket_receipts"] == 2
    assert d.buck.functions.creditLimit(agent._savings_proxy.address).call() == 0
    if d.basket_impl.startswith("equity"):
        assert st["basket_mark"] == sum(d.basket.functions.valueOf(r).call() for r in agent._basket_receipts)


def test_carry_refinances_part_of_a_mortgage_that_exceeds_available_credit(households):
    d, (agent, _)               = households
    agent.mortgage              = 5_000_000 * M6
    agent.theta                 = 3.
    agent._otc                  = True  # isolate the credit-capacity decision from market depth
    signed                      = d.buck.functions.signedBalanceOf(agent.proxy.address).call()
    limit                       = d.buck.functions.creditLimit(agent.proxy.address).call()
    unactivated                 = sum(max(0, f - a) for f, a, _ in (
        d.credit.functions.creditInfo(t).call() for t in agent._token_ids))
    before                      = agent.mortgage
    agent._decide(d, d.kctrl.functions.fundingFactor().call(), d.kctrl.functions.buckK().call(),
                  limit, max(0, -signed), unactivated, {})
    assert 0 < agent.mortgage < before
    assert agent.refis == 2 and agent._last_verdict["value"] > 0


def test_savings_retiree_keeps_range_valued_cost_settings(households):
    d, _                        = households
    scenario                    = expmod.build(expmod.load("alberta_buck/sim/experiments/demo-savings.toml"))
    agent                       = ExternalDebtRetireeAgent(70)
    agent.setup(d, scenario, idmod.seeded_rng(scenario.seed))
    assert agent._refi_carry
    assert 1_000 * M6 <= agent.switch_cost <= 3_000 * M6
    assert 0 <= agent.risk <= .01
    assert agent.penalty_months == 3.
