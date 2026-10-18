"""The portable household's real money, credit and counterfactual books on both basket implementations."""
from dataclasses import replace

import pytest

from alberta_buck.sim import experiment as expmod, identity as idmod
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.credit_households import (CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent,
                                                HouseholdPopulationAgent)
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


def test_credit_saver_redeems_its_basket_to_repay_while_the_buck_is_cheap(household_world, monkeypatch):
    d, scenario, make           = household_world
    a                           = make(CreditSaverAgent, cash_k=30, buffer_k=10)  # it invests its spare cash too
    monkeypatch.setattr(a, "_basket_usd", lambda d: .90)
    a.act(d, scenario, 0, 0, {})
    assert a._basket_receipts and a.issued_invest > 0
    drawn                       = -d.buck.functions.signedBalanceOf(a.proxy.address).call()
    receipts, ctr               = len(a._basket_receipts), {}
    monkeypatch.setattr(a, "_basket_usd", lambda d: 1.20)       # cheap: retire at a discount
    a._surplus(d, a._state(d, 1), ctr)
    after                       = -d.buck.functions.signedBalanceOf(a.proxy.address).call()
    if str(d.basket_impl).startswith("equity"):
        assert ctr["bcdRedeemedToRetire"] == 1 and len(a._basket_receipts) == receipts - 1
        assert after < drawn and a.unwound > 0, "the redeemed BUCKs repaid the lien"
    else:
        assert after == drawn, "a pro-rata basket pays TOKENs; the saver keeps its receipts"


def test_always_invest_accumulates_fees_and_real_gate_bucks_before_saving(household_world):
    d, scenario, make           = household_world
    a                           = make(cash_k=10, switch_k=5, surplus_policy="basket", max_impact_bp=400)
    waits                       = set()
    held                        = []
    for day in range(0, 361, 30):
        a.act(d, scenario, day, 0, {})
        assert not a.last_error
        if a.joined:
            break
        waits.add(a.last_issue)
        assert a.funding_pending and a.last_surplus == "fund-issue"
        assert a.invested_usd == 0 and not a._basket_receipts
        assert a.mortgage == a.hypo_mortgage
        assert all(d.credit.functions.creditInfo(t).call()[1] == 0 for t in a._token_ids)
        cash                    = d.chain.balance_of(d.usdc, a.proxy.address)
        assert cash >= a.cash_buffer
        if a.last_issue == "gate-wait":
            held.append(d.buck.functions.signedBalanceOf(a.proxy.address).call())
            assert cash >= a.cash_buffer + 5000 * M6
    assert waits == {"cash-wait", "gate-wait"}
    assert len(held) >= 2 and 0 < held[0] < held[-1], "gate BUCKs accumulate across paydays"
    assert a.joined and a.mortgage < a.hypo_mortgage
    assert not a.funding_pending and a.last_surplus == "invest"
    assert a.invested_usd > 0 and a.switch_paid == 5000 * M6
    assert d.buck.functions.signedBalanceOf(a.proxy.address).call() < 0


def test_funding_priority_releases_cash_when_issuance_is_no_longer_worthwhile(household_world):
    d, scenario, make           = household_world
    a                           = make(cash_k=12, switch_k=5, surplus_policy="basket")
    a.act(d, scenario, 0, 0, {})
    assert a.funding_pending and a.invested_usd == 0
    a.p["theta"]                = 0
    a.act(d, scenario, 1, 0, {})
    assert a.last_issue == "declined" and not a.funding_pending
    assert a.invested_usd > 0 and not a.joined


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


def investor(make, **settings):
    return make(HouseholdInvestorAgent, **({"invest_cash_k": 50, "invest_income_k": 12, "max_impact_bp": 100,
                                             "hold_years": 10} | settings))


def test_investor_buys_and_holds_bucks_and_marks_its_books(household_world):
    d, scenario, make           = household_world
    a                           = investor(make)
    assert a._token_ids == [] and a._face0 == a.mortgage == 0
    assert d.buck.functions.creditLimit(a.proxy.address).call() == 0
    a.act(d, scenario, 0, 0, {})
    assert not a.last_error and a.last_action == "buy"
    assert a.invested_usd > 0 and not a._basket_receipts
    assert d.buck.functions.balanceOf(a.proxy.address).call() > 40_000 * M6, "the BUCKs bought are held"
    st                          = a.octl_state(d)
    assert st["role"] == "investor" and st["drawn"] == 0 and st["mortgage"] == 0
    # The counterfactual keeps the dollars; the actual books hold BUCKs, less the cost of getting in.
    assert st["hypo"] == 50_000 * M6 and .95 * st["hypo"] < st["nw"] < 1.01 * st["hypo"]
    a.act(d, scenario, 30, 0, {})
    assert a.income_total == a.hypo_cash - 50_000 * M6 == 1000 * M6, "a month's saving, in both books"


def test_investor_converts_its_bucks_into_the_basket_while_they_are_dear(household_world, monkeypatch):
    d, scenario, make           = household_world
    a                           = investor(make)
    a.act(d, scenario, 0, 0, {})
    spent, ctr                  = a.invested_usd, {}
    monkeypatch.setattr(a, "_basket_usd", lambda d: .90)
    a.act(d, scenario, 1, 0, ctr)
    assert a.last_action == "convert" and a.invested_usd == spent, "dear: it buys no more"
    assert a._basket_receipts and ctr["invInvestments"] == len(a._basket_receipts)
    assert d.buck.functions.balanceOf(a.proxy.address).call() <= M6


def test_refused_investor_conversion_is_retried_not_an_error(household_world, monkeypatch):
    d, scenario, make           = household_world
    a                           = investor(make)
    a.act(d, scenario, 0, 0, {})
    held                        = d.buck.functions.balanceOf(a.proxy.address).call()
    original                    = a._proxy_exec

    def refuse(d, target, data, **kw):
        if target == d.basket.address:
            raise RuntimeError("execution reverted: Slippage()")
        return original(d, target, data, **kw)

    monkeypatch.setattr(a, "_basket_usd", lambda d: .90)
    monkeypatch.setattr(a, "_proxy_exec", refuse)
    a.act(d, scenario, 1, 0, {})
    assert a.refusals == 4 and not a.last_error and not a._basket_receipts, "offered half, four times"
    assert d.buck.functions.balanceOf(a.proxy.address).call() == held
    monkeypatch.setattr(a, "_proxy_exec", original)
    a.act(d, scenario, 2, 0, {})
    assert a._basket_receipts and d.buck.functions.balanceOf(a.proxy.address).call() <= M6


def test_investor_withdraws_sells_and_leaves(household_world, monkeypatch):
    d, scenario, make           = household_world
    a                           = investor(make, max_impact_bp=2000)
    a.act(d, scenario, 0, 0, {})
    with monkeypatch.context() as m:
        m.setattr(a, "_basket_usd", lambda d: .90)
        a.act(d, scenario, 1, 0, {})                         # part of its wealth goes into the basket
    a.act(d, scenario, 2, 0, {})                             # and it buys BUCKs again with its income
    assert a._basket_receipts
    a._exit_day                 = 3
    for day in range(3, 40):
        a.act(d, scenario, day, 0, {})
        assert not a.last_error
        if a.exited:
            break
    assert a.exited and not a._basket_receipts and a._basket_in == a._basket_shares == 0
    assert a.withdrawn_usd > 0 and d.buck.functions.balanceOf(a.proxy.address).call() <= M6
    assert all(d.chain.balance_of(t, a.proxy.address) <= 1 for t in d.tokens)
    cash                        = d.chain.balance_of(d.usdc, a.proxy.address)
    a.act(d, scenario, 90, 0, {})
    assert d.chain.balance_of(d.usdc, a.proxy.address) == cash, "an investor that has left does nothing more"
    st                          = a.octl_state(d)
    assert st["exited"] and .85 * st["hypo"] < st["nw"] <= st["hypo"] * 1.05


def test_population_adds_arrivals_to_the_live_lists(household_world):
    from alberta_buck.sim import household_policy as policy
    d, scenario, make           = household_world
    pop                         = HouseholdPopulationAgent(50)
    pop.setup(d, scenario, None)
    pop.pop                     = policy.population(dict(initial_debtors=1, initial_savers=1, initial_investors=2,
                                                         debtor_rate=0, saver_rate=0, investor_rate=200))
    agents, arbs, ctr           = [pop], [pop], {}
    pop.day_start(d, scenario, 0, ctr, agents, arbs, None)
    roles                       = [a.p["role"] for a in agents[1:]]
    assert roles[:4] == ["debtor", "saver", "investor", "investor"] and arbs == agents
    assert [a.member for a in agents[1:3]] == ["0/debtor/0", "0/saver/0"]
    assert len({a.idx for a in agents}) == len(agents)
    for day in range(1, 8):
        pop.day_start(d, scenario, day, ctr, agents, arbs, None)
    later                       = [a for a in agents[1:] if a.arrive_day > 0]
    assert later and all(a.p["role"] == "investor" for a in later)
    assert later[0].member == "0/investor/2"
    assert ctr["pop"]["investor"]["arrived"] == len(agents) - 3
    for a in agents[1:]:
        a.act(d, scenario, 7, 0, ctr)
        assert not a.last_error


def test_population_disturbances_move_rates_incomes_and_exits(household_world):
    from alberta_buck.sim import household_policy as policy
    d, scenario, make           = household_world
    pop                         = HouseholdPopulationAgent(50)
    pop.setup(d, scenario, None)
    pop.pop                     = policy.population(dict(initial_debtors=1, initial_savers=0, initial_investors=4,
                                                         debtor_rate=0, saver_rate=0, investor_rate=0))
    agents, arbs, ctr           = [pop], [pop], {}
    pop.day_start(d, scenario, 0, ctr, agents, arbs, None)
    for a in agents[1:]:
        a.act(d, scenario, 0, 0, ctr)
    debtor, investors           = agents[1], agents[2:]
    apr, income                 = debtor.apr, investors[0].income_annual
    note                        = pop.disturb(dict(investor_rate=24, apr_shift=.02, income_scale=.5, days=30,
                                                   exit_now=.5), 5)
    assert "investor_rate 24" in note and "2 of 4 investors" in note
    assert pop.pop["investor_rate"] == 24 and debtor.apr == debtor.p["apr"] == pytest.approx(apr + .02)
    assert pop.settings["apr"][0] == pytest.approx(policy.DEFAULTS["apr"][0] + .02), "arrivals face it too"
    assert investors[0].income_annual == income // 2
    assert [a._exit_day for a in investors[:2]] == [5, 5] and investors[2]._exit_day > 5
    pop.day_start(d, scenario, 35, ctr, agents, arbs, None)
    assert investors[0].income_annual == income, "a temporary income shock ends"
    pop.disturb(dict(rate_scale=.5), 40)
    assert pop.pop["investor_rate"] == 12 and pop.pop["debtor_rate"] == 0


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
