"""A mortgage-free household's credit investment: issue, keep the BUCK, deposit them in the basket."""
import json
from pathlib import Path

import pytest
from buck_core import TransactionReceiptPending
from buck_core.session import _txhex

from alberta_buck.sim import net_household
from alberta_buck.sim.credit_households import CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent
from alberta_buck.sim.gauge import active_reserves
from alberta_buck.test.test_net_household_setup import RAW, _surplus_day, household_world  # noqa: F401 (fixture)

INVEST = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-invest.json").read_text())
FORECASTS, FLOW = INVEST["forecasts"], INVEST["flow"]


def _investor_of_credit(d, make, index, cls=CreditSaverAgent, **settings):
    """A household with dollars, bound calendar and prepared trading; nothing issued."""
    a = make(index, cls, settings=INVEST["settings"] | settings)
    for contract, method, args in ((d.usdc, "approve", [d.swap_guard.address, 2**256 - 1]),
                                   (d.buck, "approve", [d.permit2.address, 2**256 - 1]),
                                   (d.permit2, "approve", [d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1])):
        a._proxy_exec(d, contract.address, contract.encode_abi(method, args=args))
    a.advance_net_calendar(d, 0, {})
    return a


def _books(d, a):
    book = d.net_credit_ops.household(a.address, a._token_ids)
    return dict(cash=d.usdc.functions.balanceOf(a.address).call(), issued=book.issued, premium=book.premium_nominal,
                spendable=book.spendable, supply=d.buck.functions.totalIssued().call(),
                receipts=tuple(a._basket_receipts), basis=a._basket_in, shares=a._basket_shares)


def _confirmed(m, kind="invest"):
    return [e for e in m.events if e.get("kind") == kind and e["status"] == "confirmed"]


def _buy_buck_in_token_pools(d, share):
    """The seeding helper really buys BUCK with a share of every TOKEN pool's TOKEN: BUCK buys more basket."""
    for token, pool in zip(d.tokens, d.pool_buck):
        v3 = d.chain.at("UniswapV3Pool", pool)
        token0 = v3.functions.token0().call()
        token_first = token0.lower() == token.address.lower()
        amount = active_reserves(d.chain, pool, token, d.buck)[0] // share
        d.chain.send(token.functions.mint(d.simlp.address, amount))
        d.chain.send(d.simlp.functions.swap(pool, d.simlp.address, token_first, amount,
            4295128740 if token_first else 1461446703485210103287273052203988822378723970341,
            token0, v3.functions.token1().call()))


def test_household_credit_investment_pays_its_closing_once_and_deposits_each_bounded_bite(household_world):
    d, _, make = household_world
    a = _investor_of_credit(d, make, 90)
    m, ctr, spec = a.refinance_machine, {}, INVEST["lifecycle"]
    start, fee = _books(d, a), int(spec["switch_usdc"])
    block = d.w3.eth.block_number
    quote = a.assess_net_investment(d, 0, **FORECASTS)
    assert d.w3.eth.block_number == block and _books(d, a) == start, "an assessment sends nothing"
    plan, context = quote["plan"], quote["context"]
    # One bite cannot repay the closing fee alone; the whole carry line, quoted now, can.
    assert context["reason"] == "worthwhile-staged-investment" and context["basis"] == "carry"
    assert plan.switch_cost == fee and context["programme"]["fixed_cost_paid_now"] == fee
    assert 0 < context["programme"]["fixed_cost_allocated"] < fee / RAW / 10 and context["programme"]["bites"] > 10
    assert quote["verdict"].go and quote["verdict"].value - fee / RAW + context["programme"]["fixed_cost_allocated"] < 0
    impact = d.buck.functions.balanceOf(d.pool_ub).call()  # the bite is bounded by the dollar pool, though nothing is sold
    assert RAW <= plan.candidate.requested_spendable == context["want"] < impact // 100
    assert plan.reserve_buy_net == plan.candidate.reserve_shortfall > 0 and plan.reserve_floor > 0
    assert 0 < plan.deposit_buck <= plan.candidate.requested_spendable and plan.position_id == 0

    principal, deposited = 0, 0
    for day in range(spec["days"]):
        if day:
            _surplus_day(d, a, day)
        before, seen = _books(d, a), len(m.events)
        assert a.advance_net_investment(d, day, ctr, forecasts=FORECASTS) is True
        assert a.last_action == "invest" and m.flow["kind"] == "invest" and m.flow["status"] == "complete", a.last_error
        after, done = _books(d, a), [e for e in m.events[seen:] if e["status"] == "confirmed"]
        assert [e["operation"] for e in done] == spec["confirmed_first" if day == 0 else "confirmed_later"]
        issue = next(e for e in done if e["operation"] == "issue")["amounts"]
        deposit = next(e for e in done if e["operation"] == "deposit")["amounts"]
        # New BUCK exist only as this facility's principal: the spendable issue and its premium deposit.
        assert int(issue["principal"]) == int(issue["spendable"]) + int(issue["premium"])
        assert after["issued"] - before["issued"] == after["supply"] - before["supply"] == int(issue["principal"])
        assert after["premium"] - before["premium"] == int(issue["premium"]) > 0
        assert 0 < int(deposit["buck"]) <= int(issue["spendable"]) and after["basis"] - before["basis"] == int(deposit["buck"])
        assert after["spendable"] >= a._net_credit_reserve == m.flow["plan"].reserve_floor > 0, "the reserve is not deposited"
        assert after["cash"] >= a.cash_buffer and (before["cash"] - after["cash"] >= fee) == (day == 0)
        principal, deposited = principal + int(issue["principal"]), deposited + int(deposit["buck"])
        assert a.advance_net_investment(d, day, ctr, forecasts=FORECASTS) is False, "one decision a day"
    assert a.refinance_closed and a.joined and a.switch_paid == fee and a.deploys == spec["days"]
    assert a.issued_invest == deposited == a._basket_in and ctr["bcdInvestments"] == spec["days"]
    assert len(a._basket_receipts) == 1, "later bites top up the same receipt"
    rid = a._basket_receipts[0]
    shares, basis = d.basket.functions.holdings(rid).call()
    assert (basis, shares) == a._receipt_book[rid] == (a._basket_in, a._basket_shares)
    assert d.net_credit_ops.household(a.address, a._token_ids).issued == principal
    assert a.mortgage == a.external_paid == 0 and a._savings_proxy is None
    assert [e["seq"] for e in m.events] == list(range(1, len(m.events) + 1))
    final = [e for e in m.events if e.get("kind") == "invest" and e["status"] == "complete"][-1]
    assert final["outcome"] == "invest" and final["accounts"]["issued"] == str(principal)
    assert final["basket"]["basis"] == str(deposited) and m.status()["kind"] == "invest"
    assert not d.chain.mismatches


def test_household_credit_investment_holds_until_the_buck_is_dear_against_the_basket(household_world):
    d, _, make = household_world
    spec = INVEST["premium"]
    a = _investor_of_credit(d, make, 91, **spec["settings"])
    m, ctr = a.refinance_machine, {}
    start = _books(d, a)
    assert a.advance_net_investment(d, 0, ctr, forecasts=FORECASTS) is True
    held = m.events[-1]
    assert a.last_action == "invest-hold" and held["status"] == "declined" and held["reason"] == "hold"
    assert held["kind"] == "invest" and "k_wad" in held["market"] and _books(d, a) == start
    # A debtor's decisions belong to refinancing until its mortgage is paid; an investor has no asset.
    debtor = _investor_of_credit(d, make, 92, CreditHouseholdAgent, ltv=.4)
    seen = len(debtor.refinance_machine.events)
    assert debtor.mortgage > 0 and debtor.advance_net_investment(d, 0, ctr, forecasts=FORECASTS) is False
    assert len(debtor.refinance_machine.events) == seen
    with pytest.raises(ValueError, match="mortgage-free"):
        debtor.assess_net_investment(d, 0, **FORECASTS)
    with pytest.raises(ValueError, match="insured household"):
        make(93, HouseholdInvestorAgent).advance_net_investment(d, 0, ctr)

    bvib = d.basket.functions.basketValueInBuck().call()
    _buy_buck_in_token_pools(d, spec["token_pool_share"])
    _surplus_day(d, a, 1)
    dear = d.basket.functions.basketValueInBuck().call()
    assert dear * 100 < bvib * spec["bvib_below_percent"], "the basket's TWAP carries the move"
    quote = a.assess_net_investment(d, 1, **FORECASTS)
    assert quote["context"]["basis"] == "premium" and quote["context"]["reason"] == "worthwhile-investment", quote["context"]
    assert quote["plan"].switch_cost == 0 and "programme" not in quote["context"]
    # Kept, the new BUCK are worth their basket value; sold they would fetch more: the premium is the gain.
    assert quote["verdict"].parts["execution"] > 0 and quote["verdict"].parts["fixed"] == 0
    assert a.advance_net_investment(d, 1, ctr, forecasts=FORECASTS) is True
    assert a.last_action == "invest" and [e["operation"] for e in _confirmed(m)] == ["fund", "issue", "deposit"], a.last_error
    after = _books(d, a)
    assert after["issued"] > 0 and after["basis"] > 0 and after["cash"] >= a.cash_buffer and a.switch_paid == 0
    assert not d.chain.mismatches


@pytest.mark.parametrize("case", FLOW["cases"], ids=lambda c: c["name"])
def test_household_credit_investment_applies_known_outcomes_once_and_stops_on_unknown(household_world, monkeypatch, case):
    d, _, make = household_world
    a = _investor_of_credit(d, make, 94)
    m, ctr, day = a.refinance_machine, {}, 0
    fee = int(INVEST["lifecycle"]["switch_usdc"])
    before = _books(d, a)
    phase = case.get("lost") or case.get("unknown") or case.get("refused") or case.get("view")
    order = INVEST["lifecycle"]["confirmed_first"]
    sends, fault, index = [], [True], FLOW["refuse_argument"]

    def wrap(dispatch):
        def send(owner, target, method, args):
            current = m.flow["phase"]
            sends.append((current, method))
            hit = fault[0] and current in (case.get("lost"), case.get("unknown"), case.get("refused"))
            if hit and case.get("unknown"):
                fault[0] = False
                raise RuntimeError("submission outcome unknown")
            if hit and case.get("refused"):
                fault[0] = False
                args = list(args)
                args[index[method]] = 2**200  # the real contract refuses its own unreachable bound
            receipt = dispatch(owner, target, method, args)
            if hit and case.get("lost"):
                fault[0] = False
                raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
            return receipt
        return send
    for ops in (d.net_market_ops, d.net_credit_ops, d.net_settlement_ops):
        monkeypatch.setattr(ops, "execute", wrap(ops.execute))
    sent = lambda p: [method for at, method in sends if at == p]
    if case.get("view") == "issue":
        real, failed = d.net_credit_ops.assess_issue_candidate, []
        def unavailable(*args, **kwargs):
            if m.flow and m.flow.get("kind") == "invest" and m.flow["phase"] == "issue" and not failed:
                failed.append(True)
                raise ConnectionError("simulated credit read failure")
            return real(*args, **kwargs)
        monkeypatch.setattr(d.net_credit_ops, "assess_issue_candidate", unavailable)
    if case.get("view") == "deposit":
        ops, failed = net_household._basket_ops, []
        def unavailable(d_):
            real = ops(d_)
            if not failed:
                failed.append(True)
                def refuse(*_args, **_kwargs):
                    raise ConnectionError("simulated basket read failure")
                real.prepare_deposit = refuse
            return real
        monkeypatch.setattr(net_household, "_basket_ops", unavailable)

    assert a.advance_net_investment(d, day, ctr, forecasts=FORECASTS) is True
    earlier = order[:order.index(phase)]
    assert [e["operation"] for e in _confirmed(m)] == earlier, "every earlier phase was real and stays applied"

    if case.get("unknown"):
        # No hash: nothing is booked for this phase, nothing is sent again and later income also waits.
        assert m.active and m.flow["status"] == "pending" and m.flow["phase"] == phase and m.flow["hash"] is None
        assert a.last_action == "invest-pending" and m.status()["kind"] == "invest"
        count, books = len(sends), _books(d, a)
        for later in (day, day + 1, 30):  # the first payday itself; the calendar observes each one
            d.anvil.warp_to(a.calendar_machine.epoch + later * 86400)
            assert a.advance_net_investment(d, later, ctr, forecasts=FORECASTS) is True
            assert "invest hash unavailable" in a.last_error and len(sends) == count
        assert m.flow["phase"] == phase and a.income_total == 0 and not a._basket_receipts
        assert (books["issued"], books["receipts"]) == (_books(d, a)["issued"], ())
        return

    if case.get("lost"):
        # The transaction mined. Its saved hash is authenticated and applied exactly once.
        assert m.active and m.flow["status"] == "pending" and m.flow["phase"] == phase and m.flow["hash"]
        assert a.last_action == "invest-pending" and len(sent(phase)) == 1
        lost_hash = m.flow["hash"]
        assert a.advance_net_investment(d, day, ctr, forecasts=FORECASTS) is True
        applied = [e for e in _confirmed(m) if e["operation"] == phase]
        assert len(applied) == 1 and applied[0]["transaction_hash"] == lost_hash and len(sent(phase)) == 1
    elif case.get("view") == "issue":
        # A failed read before the send is not a refusal: the flow waits where it is and retries the read.
        waiting = m.events[-1]
        assert m.active and m.flow["status"] == "running" and m.flow["phase"] == "issue" and not sent("issue")
        assert waiting["status"] == "waiting" and "observation unavailable" in waiting["reason"]
        assert a.advance_net_investment(d, day, ctr, forecasts=FORECASTS) is True
    else:
        refusal = [e for e in m.events if e["status"] == "refused"][-1]
        assert a.last_action == "invest-refused" and not m.active and m.flow["phase"] == phase
        assert bool(refusal["transaction_hash"]) == bool(case.get("refused")), "a view failure sent nothing"
        if case.get("refused"):
            assert d.w3.eth.get_transaction_receipt(refusal["transaction_hash"])["status"] == 0
        now = _books(d, a)
        assert m.surplus_due == (phase == "deposit")
        assert a.refinance_closed == (phase != "closing") and a.switch_paid == (0 if phase == "closing" else fee)
        assert (now["issued"] > 0) == (phase == "deposit") and now["receipts"] == ()
        if phase == "closing":
            assert now == before, "a proved refusal changed nothing"
        if phase == "deposit":
            issued = int([e for e in _confirmed(m) if e["operation"] == "issue"][0]["amounts"]["spendable"])
            assert now["spendable"] >= issued, "the issued BUCK stay owned"
        assert a.advance_net_investment(d, day, ctr, forecasts=FORECASTS) is False, "one attempt a day"
        # A known refusal never blocks the next day's work or decision.
        day += 1
        _surplus_day(d, a, day)
        assert a.advance_net_investment(d, day, ctr, forecasts=FORECASTS) is True
        resumed = [e["status"] for e in m.events].count("resumed")
        assert resumed == (1 if phase == "deposit" else 0)

    assert not m.active and m.flow["status"] == "complete" and a.last_action == "invest", a.last_error
    done = [e["operation"] for e in _confirmed(m)]
    # Each phase is confirmed exactly once in the end: what a refusal left done is never repeated.
    assert sorted(done) == sorted(order) and len(set(e["transaction_hash"] for e in _confirmed(m))) == len(done)
    for step in order:
        repeats = 2 if case.get("refused") == step else 1
        assert len(sent(step)) == repeats, (step, sends)
    after = _books(d, a)
    assert a.switch_paid == fee and a.refinance_closed and len(a._basket_receipts) == 1
    issue = [e for e in _confirmed(m) if e["operation"] == "issue"][0]["amounts"]
    assert after["issued"] == after["supply"] - before["supply"] == int(issue["principal"])
    shares, basis = d.basket.functions.holdings(a._basket_receipts[0]).call()
    assert (basis, shares) == a._receipt_book[a._basket_receipts[0]] and basis == a.issued_invest > 0
    assert after["spendable"] >= a._net_credit_reserve > 0 and after["cash"] >= a.cash_buffer
    assert [e["seq"] for e in m.events] == list(range(1, len(m.events) + 1))
    # Credit dispatch expects success, so the journal notes the one issuance this case made revert.
    assert len(d.chain.mismatches) == (1 if case.get("refused") == "issue" else 0)
