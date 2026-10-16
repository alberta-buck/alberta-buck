"""The portable household's real money, credit and counterfactual books on both basket implementations."""
from dataclasses import replace

import pytest

from alberta_buck.sim import experiment as expmod, identity as idmod
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.credit_households import CreditHouseholdAgent, CreditSaverAgent
from alberta_buck.sim.deploy import deploy
from alberta_buck.sim.direct_mint import BootstrapDMAgent
from alberta_buck.sim.household_policy import issuance
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING

M6                              = 10 ** 6


@pytest.fixture(params=["prorata", "equity-ops"])
def household_world(request):
    scenario                    = replace(ROUTING, days=90, agents={})
    scenario.experiment         = expmod.load(None)
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

        def make(cls=CreditHouseholdAgent, **settings):
            agent               = cls(77)
            agent.settings      = dict(home_k=1000, ltv=.2, apr=.055, premium_bp=50, theta=3.,
                                       cash_k=100, buffer_k=10, switch_k=0, risk=0, decision_days=1,
                                       income_frac=.1, living_frac=.5) | settings
            agent.setup(d, scenario, rng)
            return agent
        yield d, scenario, make


def test_identical_cash_flow_without_buck_activity(household_world):
    d, scenario, make           = household_world
    a                           = make()
    a._next_decision            = 1000
    for day in (0, 17, 30, 60):
        a.act(d, scenario, day, 0, {})
    assert a.mortgage == a.hypo_mortgage < a._mortgage0
    assert d.chain.balance_of(d.usdc, a.proxy.address) == a.hypo_cash
    assert a.external_interest == a.hypo_interest > 0
    assert a.living_paid == a.hypo_living > 0
    assert a.ext_premium == a.hypo_premium > 0


def test_partial_refinance_pays_real_usdc_to_lender(household_world):
    d, scenario, make           = household_world
    a                           = make()
    before                      = d.chain.balance_of(d.usdc, a.SINK)
    a.act(d, scenario, 0, 0, {})
    assert not a.last_error
    assert 0 < a.mortgage < a.hypo_mortgage
    assert a._mortgage0 - a.mortgage <= d.chain.balance_of(d.usdc, a.SINK) - before
    assert a.premium_paid > 0 and a.joined
    assert d.buck.functions.signedBalanceOf(a.proxy.address).call() < 0


def test_insurance_mark_accounts_for_age_carried_on_refund(household_world):
    d, scenario, make           = household_world
    a                           = make()
    a.act(d, scenario, 0, 0, {})
    now                         = d.w3.eth.get_block("latest")["timestamp"]
    d.anvil.set_next_block_timestamp(now + 365 * 86400)
    d.anvil.mine()
    st                          = a.octl_state(d)
    raw                         = d.buck.functions.rawBalanceOf(d.pool_acct).call()
    fee                         = d.buck.functions.feeOwing(d.pool_acct).call()
    assert st["insurance_age"] == fee * st["deposit"] // raw > 0


def test_discount_repayment_preserves_reusable_coverage(household_world, monkeypatch):
    d, scenario, make           = household_world
    a                           = make()
    a.act(d, scenario, 0, 0, {})
    before                      = d.buck.functions.signedBalanceOf(a.proxy.address).call()
    coverage                    = [d.credit.functions.creditInfo(t).call()[1] for t in a._token_ids]
    monkeypatch.setattr(a, "_basket_usd", lambda d: 1.20)
    a._surplus(d, a._state(d, 0), {})
    assert d.buck.functions.signedBalanceOf(a.proxy.address).call() > before
    assert a.unwound > 0
    assert coverage == [d.credit.functions.creditInfo(t).call()[1] for t in a._token_ids]
    # Draw a small tranche from the headroom just repaid, without reactivating coverage.
    a.p["max_impact_bp"]         = 1
    state                       = a._state(d, 0)
    kind, verdict               = issuance(state, a.p)
    assert verdict.go
    repaid                      = d.buck.functions.signedBalanceOf(a.proxy.address).call()
    a._issue(d, state, kind, verdict, {})
    assert d.buck.functions.signedBalanceOf(a.proxy.address).call() < repaid
    assert coverage == [d.credit.functions.creditInfo(t).call()[1] for t in a._token_ids]


def test_premium_investment_preserves_borrower_liability(household_world, monkeypatch):
    d, scenario, make           = household_world
    a                           = make()
    a.act(d, scenario, 0, 0, {})
    before                      = d.buck.functions.signedBalanceOf(a.proxy.address).call()
    monkeypatch.setattr(a, "_basket_usd", lambda d: .90)
    a._surplus(d, a._state(d, 0), {})
    assert a._basket_receipts and a.invested_usd > 0
    assert d.buck.functions.signedBalanceOf(a.proxy.address).call() == before
    assert d.buck.functions.creditLimit(a._savings_proxy.address).call() == 0


def test_credit_saver_issues_at_premium_and_invests_proceeds(household_world, monkeypatch):
    d, scenario, make           = household_world
    a                           = make(CreditSaverAgent)
    monkeypatch.setattr(a, "_basket_usd", lambda d: .90)
    a.act(d, scenario, 0, 0, {})
    assert not a.last_error
    assert a.mortgage == a.hypo_mortgage == 0
    assert a.issued_invest > 0 and a._basket_receipts
    assert d.buck.functions.signedBalanceOf(a.proxy.address).call() < 0
    assert a.invested_usd > 0


def test_arrival_has_no_earlier_income_or_market_activity(household_world):
    d, scenario, make           = household_world
    a                           = make()
    a.arrive_day = a._last_day   = 45
    a._next_pay                 = 75
    a._next_decision            = 45
    a.act(d, scenario, 44, 0, {})
    assert d.chain.balance_of(d.usdc, a.proxy.address) == 0
    assert a.mortgage == a._mortgage0 and not a._started
    a.act(d, scenario, 45, 0, {})
    assert a._started and a.income_total == 0


def test_unpaid_expenses_remain_in_both_books(household_world):
    d, scenario, make           = household_world
    a                           = make(cash_k=0, income_frac=0)
    a._next_decision            = 1000
    a.act(d, scenario, 0, 0, {})
    a.act(d, scenario, 60, 0, {})
    st                          = a.octl_state(d)
    assert st["arrears"] == st["hypo_arrears"] > 0
    assert st["nw"] == st["hypo"] == -a.mortgage - st["arrears"]


def test_rejected_deposit_keeps_assets_and_retries_without_new_spending(household_world, monkeypatch):
    d, scenario, make           = household_world
    a                           = make()
    a._next_decision            = 1000
    a.act(d, scenario, 0, 0, {})
    original                    = a._proxy_exec

    def reject(d, target, data, **kw):
        if target == d.basket.address:
            raise RuntimeError("deposit guard")
        return original(d, target, data, **kw)

    monkeypatch.setattr(a, "_proxy_exec", reject)
    a._invest_cash(d, 20000 * M6, {})
    assert "deposit guard" in a.last_error
    spent                       = a.invested_usd
    assert spent > 0 and a.octl_state(d)["pending_usd"] > 0 and not a._basket_receipts
    monkeypatch.setattr(a, "_proxy_exec", original)
    a._invest_cash(d, 0, {})
    assert a._basket_receipts and a.invested_usd == spent
    assert a.octl_state(d)["pending_usd"] == 0


def test_arrived_population_summary_excludes_future_books(household_world):
    from alberta_buck.sim.snapshot import household_summary
    d, scenario, make           = household_world
    a                           = make()
    before                      = household_summary([a.octl_state(d)])
    assert before["scheduled"] == 1 and before["active"] == before["mortgage"] == 0
    a.act(d, scenario, 0, 0, {})
    after                       = household_summary([a.octl_state(d)])
    assert after["active"] == 1 and after["mortgage"] == a.mortgage
    assert after["hypo_mortgage"] == a.hypo_mortgage


def test_cash_flow_ledger_matches_javascript(household_world):
    import json
    from pathlib import Path
    import subprocess

    d, scenario, make           = household_world
    a                           = make(cash_k=0, income_frac=.01)
    a._next_decision            = 10000
    days                        = [0, 17, 30, 60, 90, 365]
    actual                      = []
    for day in days:
        a.act(d, scenario, day, 0, {})
        actual.append([a.mortgage, a.hypo_mortgage, a.external_interest, a.hypo_interest,
                       a.living_paid, a.hypo_living, a.external_paid, a.hypo_paid,
                       a.premium_arrears, a.hypo_premium_arrears,
                       d.chain.balance_of(d.usdc, a.proxy.address), a.hypo_cash])
    script                      = """
import { CreditHouseholdAgent } from './core/js/src/agents/credit-household.js';
let cash = 0n;
const p = JSON.parse(process.argv[1]);
const a = new CreditHouseholdAgent({account: {}, settings: p});
const world = {fiatIn: async (_, n) => {cash += n;}, households: {
 pay: async (_, n) => {const paid = n < cash ? n : cash; cash -= paid; return paid;},
}};
const rows = [];
for (const day of [0, 17, 30, 60, 90, 365]) {
 await a.advance(world, day);
 rows.push([a.mortgage, a.hypoMortgage, a.externalInterest, a.hypoInterest,
 a.livingPaid, a.hypoLiving, a.externalPaid, a.hypoPaid, a.premiumArrears, a.hypoPremiumArrears, cash, a.hypoCash]);
}
console.log(JSON.stringify(rows, (_, v) => typeof v === 'bigint' ? Number(v) : v));
"""
    root                        = Path(__file__).resolve().parents[2]
    js                          = json.loads(subprocess.run(["node", "--input-type=module", "-e", script,
                                                            json.dumps(a.p)], cwd=root, check=True,
                                                           text=True, capture_output=True).stdout)
    for py_row, js_row in zip(actual, js):
        assert py_row == pytest.approx(js_row, rel=0, abs=1)  # one micro-dollar rounding tolerance
