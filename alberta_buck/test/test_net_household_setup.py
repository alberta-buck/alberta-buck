"""Existing household setup, credit/portfolio books and the remaining decision gate."""
from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
import random
from types import SimpleNamespace

import pytest
from web3.logs import DISCARD
from buck_core import TransactionReceiptPending, TransactionReverted, observe_net_market

from alberta_buck.sim import experiment as expmod, identity as idmod
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.credit_households import CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent
from alberta_buck.sim.deploy import deploy
from alberta_buck.sim.net_household import RefinancePlan
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING

RAW = 10**6
Q = 10**36
YEAR = 365 * 86400 + 6 * 3600
REFINANCE = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-refinance.json").read_text())
CALENDAR = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-calendar.json").read_text())["flow"]


@pytest.fixture
def household_world(monkeypatch):
    monkeypatch.delenv("SIM_WHEEL_ARB", raising=False)
    scenario = replace(ROUTING, tokens=ROUTING.tokens[:2], csv_files=ROUTING.csv_files[:2], agents={})
    scenario.experiment = expmod.load(None, sets=["deploy.target_buck_m=1", "deploy.target_buck_lp_m=1",
                                                 "machine.desk_capital_usd=1000"])
    rng = idmod.seeded_rng(scenario.seed)
    with PyrevmAnvil(port=0) as evm:
        chain = Chain(evm.w3, evm.w3.eth.accounts[0])
        d = deploy(chain, evm, scenario, rng, verbose=False, basket_impl="equity-ops", equity_machine=True)
        def make(i, cls, *, assets=1, settings=None):
            agent = cls(i)
            agent.settings = dict(home_k=1000, premium_bp=100, asset_count=assets) | (settings or {})
            agent.setup(d, scenario, rng)
            return agent
        yield d, scenario, make


def test_primary_household_setup_and_per_facility_credit_marks(household_world):
    d, scenario, make = household_world
    chain, evm = d.chain, d.anvil
    agents = []
    for i, cls in enumerate((CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent)):
        a = make(i, cls, assets=4)  # explicit multiple-asset/pool bookkeeping fixture
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


def test_household_defaults_use_one_facility_per_insured_asset(household_world):
    d, _, make = household_world
    for i, cls in enumerate((CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent)):
        a = make(60 + i, cls)
        assert len(a._token_ids) == a.p["asset_count"] == (0 if a.investor else 1)
        if not a.investor:
            assert d.credit.functions.creditInfo(a._token_ids[0]).call()[0] == a._face0
        assert d.net_credit_ops.household(a.address, a._token_ids).issued == 0


def test_primary_proxy_market_uses_net_guard_bounded_sales_and_exact_payments(household_world):
    d, _, make = household_world
    a = make(19, CreditHouseholdAgent)
    owner, chain = a.address, d.chain
    d.anvil.warp_to(d.w3.eth.get_block("latest")["timestamp"] + YEAR)
    a._proxy_exec(d, d.usdc.address, d.usdc.encode_abi("approve", args=[d.swap_guard.address, 2**256 - 1]))
    a._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.permit2.address, 2**256 - 1]))
    a._proxy_exec(d, d.permit2.address, d.permit2.encode_abi(
        "approve", args=[d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1]))
    chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[owner, 200 * RAW])))
    candidate = d.net_credit_ops.assess_issue_candidate(owner, a._token_ids[0], 1000 * RAW, 500 * RAW, 100 * RAW)
    assert candidate.reasons == ()
    issued = d.net_credit_ops.issue_candidate(owner, candidate)
    assert issued.principal == 600 * RAW and issued.premium == 100 * RAW
    initial_mortgage, initial_paid = a.mortgage, a.external_paid
    supply = d.buck.functions.totalIssued().call()
    held = d.buck.functions.balanceOf(owner).call()
    cash = d.usdc.functions.balanceOf(owner).call()
    quote = observe_net_market(chain, d.buck, d.usdc, d.pool_ub, d.fee_ub)
    assert quote.net_scaled < quote.gross_scaled
    sale = d.net_market_ops.sell_exact_input(owner, d.pool_ub, 100 * RAW, 90 * RAW, owner, d.fee_ub)
    assert abs(sale.received - quote.sell_expected(100 * RAW)) <= 4
    assert sale.nominal_paid == 100 * RAW
    assert d.usdc.functions.balanceOf(owner).call() - cash == sale.received >= 90 * RAW
    assert held - d.buck.functions.balanceOf(owner).call() == sale.paid
    assert d.net_credit_ops.household(owner, a._token_ids).issued == issued.principal
    assert d.buck.functions.totalIssued().call() == supply
    assert (a.mortgage, a.external_paid) == (initial_mortgage, initial_paid), "export is not bank payment"

    receiver = a._new_proxy(d)
    quote = observe_net_market(chain, d.buck, d.usdc, d.pool_ub, d.fee_ub)
    purchase = d.net_market_ops.buy_exact_input(owner, d.pool_ub, 10 * RAW, 9 * RAW, receiver.address)
    assert abs(purchase.received - quote.buy_expected(10 * RAW)) <= 4
    assert purchase.nominal_received > purchase.received >= 9 * RAW
    assert d.buck.functions.netBalanceScaled(receiver.address).call() == purchase.net_scaled
    assert d.buck.functions.balanceOf(d.router.address).call() == 0
    assert d.buck.functions.balanceOf(d.swap_guard.address).call() == 0
    assert d.usdc.functions.balanceOf(d.swap_guard.address).call() == 0
    assert not d.reg.functions.isVerified(d.permit2.address).call()
    assert not d.reg.functions.isVerified(d.swap_guard.address).call()
    quote = observe_net_market(chain, d.buck, d.usdc, d.pool_ub, d.fee_ub)
    budget = quote.buy_cost(RAW) * 101 // 100
    exact = d.net_market_ops.buy_exact_output(owner, d.pool_ub, RAW, budget, receiver.address)
    assert exact.received >= RAW and exact.paid <= budget
    with pytest.raises(ValueError, match="fee mismatch"):
        observe_net_market(chain, d.buck, d.usdc, d.pool_ub, 3000)

    cash = d.usdc.functions.balanceOf(owner).call()
    with pytest.raises(TransactionReverted):
        d.net_market_ops.buy_exact_input(owner, d.pool_ub, RAW, 2 * RAW, owner)
    assert d.usdc.functions.balanceOf(owner).call() == cash
    assert d.buck.functions.totalIssued().call() == supply
    with pytest.raises(ValueError, match="bounds"):
        d.net_market_ops.sell_exact_input(owner, d.pool_ub, RAW, 0, owner, d.fee_ub)

    # A lost proxy reply preserves the real sale hash for reconciliation;
    # neither a sale nor its recovered receipt applies a mortgage payment.
    dispatch = d.net_market_ops.execute
    def lose_reply(*args):
        receipt = dispatch(*args)
        raise TransactionReceiptPending(d.w3.to_hex(receipt["transactionHash"]))
    d.net_market_ops.execute = lose_reply
    with pytest.raises(TransactionReceiptPending) as lost:
        d.net_market_ops.sell_exact_input(owner, d.pool_ub, RAW, 1, receiver.address, d.fee_ub)
    receipt = d.w3.eth.get_transaction_receipt(lost.value.transaction_hash)
    recovered = d.net_market_ops.sale_from_receipt(owner, d.pool_ub, receiver.address, RAW, 1,
                                                  receipt, expected_hash=lost.value.transaction_hash)
    assert recovered.received == d.usdc.functions.balanceOf(receiver.address).call() > 0
    with pytest.raises(ValueError, match="missing or ambiguous"):
        d.net_market_ops.sale_from_receipt(owner, d.pool_ub, owner, RAW, 1, receipt,
                                           expected_hash=lost.value.transaction_hash)
    assert (a.mortgage, a.external_paid) == (initial_mortgage, initial_paid)
    payment = d.net_settlement_ops.pay_exact(owner, a.SINK, 20 * RAW)
    assert payment.amount == 20 * RAW
    assert d.usdc.functions.balanceOf(a.SINK).call() == payment.amount
    assert d.buck.functions.totalIssued().call() == supply


@pytest.mark.parametrize("case", REFINANCE["cases"], ids=lambda c: c["name"])
def test_refinance_phases_apply_actual_receipts_once(household_world, monkeypatch, case):
    from alberta_buck.sim.snapshot import Snapshotter
    from buck_core.session import _txhex
    d, scenario, make = household_world
    a = make(23, CreditHouseholdAgent)
    b = {key: int(value) for key, value in REFINANCE["base"].items()}
    seed_usdc = int(case.get("seed_usdc", b["seed_usdc"]))
    seed_buck = int(case.get("seed_buck", b["seed_buck"]))
    reserve_buy_net, reserve_buy_max = (int(case.get(key, 0)) for key in ("reserve_buy_net", "reserve_buy_max_usdc"))
    d.chain.send(d.usdc.functions.mint(a.address, seed_usdc))
    if seed_buck:
        d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, seed_buck])))
    a._proxy_exec(d, d.usdc.address, d.usdc.encode_abi("approve", args=[d.swap_guard.address, 2**256 - 1]))
    a._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.permit2.address, 2**256 - 1]))
    a._proxy_exec(d, d.permit2.address, d.permit2.encode_abi(
        "approve", args=[d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1]))
    c = d.net_credit_ops.assess_issue_candidate(a.address, a._token_ids[0], b["coverage"], b["new_spendable"], b["premium"])
    assert c.reasons == (("reserve-shortfall",) if reserve_buy_net else ())
    minimum = 5000 * RAW if case["fault"] == "revert" and case["phase"] == "export" else b["minimum_usdc"]
    plan = RefinancePlan(c, b["export_buck"], minimum, b["mortgage_limit"], b["cash_buffer"], b["reserve_floor"],
                         b["switch_cost"], b["penalty_numerator"], b["penalty_denominator"], reserve_buy_net, reserve_buy_max)
    machine, calls = a.refinance_machine, {}
    before = (a.mortgage, a.hypo_mortgage, a.hypo_cash, d.buck.functions.totalIssued().call())
    def wrap(dispatch):
        def execute(owner, target, method, args):
            phase = machine.flow["phase"]
            calls[phase] = calls.get(phase, 0) + 1
            if phase == case["phase"] and case["fault"] == "unknown":
                raise RuntimeError("submission outcome unknown")
            if phase == case["phase"] and case["fault"] == "revert" and phase == "mortgage":
                args = [a.SINK, 2**256 - 1]
            receipt = dispatch(owner, target, method, args)
            if phase == case["phase"] and case["fault"] == "lost":
                raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
            return receipt
        return execute
    for ops in (d.net_credit_ops, d.net_market_ops, d.net_settlement_ops):
        monkeypatch.setattr(ops, "execute", wrap(ops.execute))
    assess, read_failures = d.net_credit_ops.assess_issue_candidate, []
    def reassess(*args, **kwargs):
        if case["fault"] == "quote-unavailable" and not read_failures:
            read_failures.append(True)
            raise RuntimeError("quote endpoint unavailable after funded purchase")
        fresh = assess(*args, **kwargs)
        return replace(fresh, premium=fresh.premium + 1) if case["fault"] == "quote-changed" else fresh
    monkeypatch.setattr(d.net_credit_ops, "assess_issue_candidate", reassess)
    ctr = {"directTrades": 0, "cycleTrades": 0}
    a.start_net_refinance(d, plan, 0, ctr, context={"reason": "shared execution vector", "wide": 2**100 + 7})
    if case["fault"] == "lost":
        assert machine.status()["status"] == "pending" and machine.status()["phase"] == case["phase"]
        assert a.external_paid == a.switch_paid == a.penalty_paid == 0
        supply, sent = d.buck.functions.totalIssued().call(), dict(calls)
        actual_receipt = d.w3.eth.get_transaction_receipt
        with monkeypatch.context() as patch:
            patch.setattr(d.w3.eth, "get_transaction_receipt", lambda h: {**actual_receipt(h), "transactionHash": bytes(32)})
            a.act(d, scenario, 1, 0, ctr)
        assert machine.status()["status"] == "pending" and calls == sent
        assert d.buck.functions.totalIssued().call() == supply and a.mortgage == before[0]
        a.act(d, scenario, 2, 0, ctr)
    elif case["fault"] == "unknown":
        sent = dict(calls)
        a.act(d, scenario, 1, 0, ctr)
        a.act(d, scenario, 2, 0, ctr)
        assert calls == sent and "hash unavailable" in a.last_error
    elif case["fault"] == "quote-unavailable":
        assert machine.status()["phase"] == "issue" and machine.status()["status"] == "running"
        assert calls == {"fund": 1} and a.external_paid == a.premium_paid == 0
        a.act(d, scenario, 1, 0, ctr)
    assert machine.status()["status"] == case["final"]
    assert all(n == 1 for n in calls.values()), "a recovered receipt must not send again"
    completed = case["final"] == "complete"
    principal = b["mortgage_limit"] if completed else 0
    expected_operations = case.get("confirmed", ["issue", "export", "mortgage"] if completed else
        [] if case["fault"] == "unknown" else ["issue"] if case["phase"] == "export" else ["issue", "export"])
    premium = b["premium"] if "issue" in expected_operations else 0
    assert a.mortgage == before[0] - principal and a.external_paid == principal
    assert a.hypo_mortgage == before[1] and a.hypo_cash == before[2]
    penalty = (principal * b["penalty_numerator"] + b["penalty_denominator"] - 1) // b["penalty_denominator"]
    assert a.premium_paid == premium and a.penalty_paid == penalty
    assert a.switch_paid == (b["switch_cost"] if completed else 0)
    assert d.usdc.functions.balanceOf(a.SINK).call() == principal + a.switch_paid + penalty
    assert a.refis == (1 if completed else 0)
    issued = b["new_spendable"] + b["premium"] if "issue" in expected_operations else 0
    assert d.net_credit_ops.household(a.address, a._token_ids).issued == issued
    assert d.buck.functions.totalIssued().call() == before[3] + issued
    assert d.buck.functions.balanceOf(a.address).call() >= (b["reserve_floor"] if seed_buck or "fund" in expected_operations else 0)
    events = json.loads(json.dumps(machine.events))
    assert events[0]["context"]["wide"] == str(2**100 + 7)
    assert events[0]["limits"]["theta"] == a.p["theta"]
    assert events[0]["plan"]["export_buck"] == str(b["export_buck"])
    assert int(events[0]["market"]["k_wad"]) > 0
    assert int(events[0]["market"]["nominal_usdc_per_buck"]["denominator"]) > 0
    assert events[0]["accounts"]["ratios"]["reserve_cover"] == pytest.approx(seed_buck / b["reserve_floor"])
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["accounts"]["usdc"].isdigit() for event in events)
    confirmed = [event for event in events if event["status"] == "confirmed"]
    assert [e["operation"] for e in confirmed] == expected_operations
    if "fund" in expected_operations:
        payment = next(e for e in confirmed if e["operation"] == "fund")["amounts"]
        assert 0 < int(payment["usdc"]) <= reserve_buy_max
        assert int(payment["net_buck_scaled"]) >= reserve_buy_net * Q
        bank = sum(int(e["amounts"]["usdc"]) for e in confirmed if e["operation"] == "mortgage")
        proceeds = sum(int(e["amounts"]["usdc"]) for e in confirmed if e["operation"] == "export")
        assert d.usdc.functions.balanceOf(a.address).call() == seed_usdc - int(payment["usdc"]) + proceeds - bank
    if not machine.active:
        count = len(events)
        assert machine.advance(d, 3, ctr) is False and len(machine.events) == count
    if case["name"] == "confirmed":
        # Mandatory public transitions survive failed capture and SIM_ACTS=none,
        # then stream once through the existing JSON/full/lite frame paths.
        from alberta_buck.sim import acts, net_snapshot
        from alberta_buck.sim.server import Session
        monkeypatch.setattr(acts, "MODE", "none")
        snap = Snapshotter(d, scenario)
        with monkeypatch.context() as patch:
            def unavailable(*_):
                raise ValueError("required observation unavailable")
            patch.setattr(net_snapshot, "observe", unavailable)
            with pytest.raises(ValueError, match="required observation"):
                snap.capture(0, ctr, [a], 0)
        assert not snap.frames
        snap.capture(0, ctr, [a], 0)
        assert len(snap.frames[0]["household_events"]) == len(events)
        import time
        session = Session.__new__(Session)
        session._last_day_ts, session.sid, session.paused, session._sv = time.monotonic(), "flow-vector", True, {}
        full, lite = (json.loads(payload) for payload in session._payloads(snap.frames[0]))
        assert full["household_events"] == lite["household_events"]
        assert "ag" not in lite
        snap.capture(1, ctr, [a], 0)
        assert "household_events" not in snap.frames[1]


@pytest.mark.parametrize("cash_k,will_issue", [(40, True), (10, False)])
@pytest.mark.parametrize("forecast_mode", ["explicit", "observed"])
def test_refinance_assessment_chooses_actual_affordable_issuance_without_transactions(household_world, cash_k, will_issue, forecast_mode):
    d, _, make = household_world
    a = make(33, CreditHouseholdAgent, settings=dict(apr=.06, ltv=.4, theta=3, risk=0,
                                                   switch_k=0, max_impact_bp=100, divest_rate=0))
    d.chain.send(d.usdc.functions.mint(a.address, cash_k * 1000 * RAW))
    a._proxy_exec(d, d.usdc.address, d.usdc.encode_abi("approve", args=[d.swap_guard.address, 2**256 - 1]))
    a._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.permit2.address, 2**256 - 1]))
    a._proxy_exec(d, d.permit2.address, d.permit2.encode_abi(
        "approve", args=[d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1]))
    before = (d.w3.eth.block_number, a.mortgage, a.hypo_mortgage, a.hypo_cash, d.buck.functions.totalIssued().call())
    forecasts = (dict(expected_relief_rate=.02, expected_refund_growth=-.02, expected_wallet_growth=-.02)
                 if forecast_mode == "explicit" else {})
    assessed = a.assess_net_refinance(d, 1, **forecasts)
    assert (d.w3.eth.block_number, a.mortgage, a.hypo_mortgage, a.hypo_cash, d.buck.functions.totalIssued().call()) == before
    assert a.refinance_machine.events == [] and not a.funding_pending
    plan, verdict, context = (assessed[k] for k in ("plan", "verdict", "context"))
    if forecast_mode == "observed":
        from buck_core import buck_math
        observed = d.net_credit_ops.quote_global_relief_rate()
        assert context["forecasts"]["source"]["block_number"] == before[0]
        assert context["forecasts"]["relief_rate"] == observed.annual_rate_wad / 10**18
        assert context["forecasts"]["basis"] == "marked-fund/idle-cash"
        assert abs(context["forecasts"]["wallet_growth"] - (buck_math.net_decay(Q, YEAR) / Q - 1)) < 1e-10
        assert context["forecasts"]["refund_growth"] == context["forecasts"]["wallet_growth"]
    assert (plan is not None) == will_issue and verdict.go == will_issue
    if not will_issue:
        assert context["rejections"]["cash-buffer"] > 0 and context["reason"] == "no-worthwhile-refinance"
        assert d.credit.functions.issuanceOf(a._token_ids[0]).call() == [0, 0]
        return
    assert plan.candidate is not None and plan.reserve_buy_net > 0
    assert set(plan.candidate.reasons) <= {"reserve-shortfall"}
    expected_saving = (a._face0 * a.premium_rate // 10_000) * plan.candidate.coverage // a._face0
    assert verdict.parts["premium"] == pytest.approx(expected_saving / RAW)
    assert context["feasible"] > 1 and context["selected"]["economics"]["new_spendable"] > 0
    assert plan.mortgage_limit == plan.minimum_usdc <= a.mortgage
    a.start_net_refinance(d, plan, 1, {}, context=context)
    assert a.refinance_machine.status()["status"] == "complete"
    assert [e["operation"] for e in a.refinance_machine.events if e["status"] == "confirmed"] == ["fund", "issue", "export", "mortgage"]
    assert a.mortgage == before[1] - plan.mortgage_limit and a.hypo_mortgage == before[2] and a.hypo_cash == before[3]
    assert d.usdc.functions.balanceOf(a.address).call() >= a.cash_buffer
    assert d.buck.functions.balanceOf(a.address).call() >= plan.reserve_floor


@pytest.mark.parametrize("keep_reserve", [True, False])
def test_refinance_assessment_uses_existing_room_without_redundant_reserve_buys(household_world, keep_reserve):
    d, _, make = household_world
    a = make(34, CreditHouseholdAgent, settings=dict(apr=.06, ltv=.4, theta=3, risk=0,
        switch_k=0, max_impact_bp=100, penalty_months=0, utilization=.0001, divest_rate=0))
    d.chain.send(d.usdc.functions.mint(a.address, a.cash_buffer))
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, 20 * RAW])))
    tid = a._token_ids[0]
    d.net_credit_ops.issue(a.address, tid, 100 * RAW, 40 * RAW, 10 * RAW, d.pool_acct)
    a.joined = True
    if not keep_reserve:
        held = d.buck.functions.balanceOf(a.address).call()
        a._proxy_exec(d, d.buck.address, d.buck.encode_abi("transfer", args=[d.simlp.address, held]))
    old_reserve = d.buck.functions.balanceOf(a.address).call()
    d.anvil.warp_to(d.w3.eth.get_block("latest")["timestamp"] + YEAR)
    d.chain.send(d.kctrl.functions.compute())  # keeper/control work precedes the read-only decision
    actual_reserve = d.buck.functions.balanceOf(a.address).call()
    assert actual_reserve < old_reserve if keep_reserve else actual_reserve == old_reserve == 0
    a._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.permit2.address, 2**256 - 1]))
    a._proxy_exec(d, d.permit2.address, d.permit2.encode_abi(
        "approve", args=[d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1]))
    book = d.net_credit_ops.household(a.address, a._token_ids)
    assessed = a.assess_net_refinance(d, 1, expected_relief_rate=.02, expected_refund_growth=-.02,
                                     expected_wallet_growth=-.02, reserve_floor=old_reserve)
    plan = assessed["plan"]
    assert plan is not None and plan.candidate.coverage == plan.candidate.premium == 0
    assert plan.reserve_buy_net == plan.reserve_buy_max_usdc == 0
    assert plan.reserve_floor == actual_reserve and plan.export_buck > 0
    a.start_net_refinance(d, plan, 1, {}, context=assessed["context"])
    assert a.refinance_machine.status()["status"] == "complete"
    assert d.net_credit_ops.household(a.address, a._token_ids).issued == book.issued + plan.export_buck
    assert d.buck.functions.balanceOf(a.address).call() >= actual_reserve


def test_external_insurance_tracks_real_activation_and_retirement(household_world, monkeypatch):
    d, _, make = household_world
    a = make(32, CreditHouseholdAgent)
    full = a._face0 * a.premium_rate // 10_000 // 12
    assert a._external_premium_due(d, full) == full
    d.chain.send(d.usdc.functions.mint(a.address, 3000 * RAW))
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, 20 * RAW])))
    tid = a._token_ids[0]
    d.net_credit_ops.issue(a.address, tid, 100 * RAW, 40 * RAW, 10 * RAW, d.pool_acct)
    a.joined = True
    active = 100 * RAW
    expected = (full * (a._face0 - active) + a._face0 - 1) // a._face0
    assert full > a._external_premium_due(d, full) == expected > 0
    # Isolate this calendar component from the as-yet unported working wallet.
    a.income_annual, a.payment, a.hypo_cash = 0, 0, 2 * full
    a.p["living_frac"] = 0
    monkeypatch.setattr(a, "_tend_wallet", lambda _: None)
    a._monthly(d)
    assert a.ext_premium == expected and a.hypo_premium == full
    deadline = d.w3.eth.get_block("latest")["timestamp"] + 3600
    plan = d.net_credit_ops.plan_reduction(a.address, tid, 25 * RAW)
    assert plan.reason == "ready" and plan.principal == 25 * RAW
    d.net_credit_ops.settle_plan(a.address, plan, deadline)
    assert d.credit.functions.creditInfo(tid).call()[1] == 50 * RAW
    restored = a._external_premium_due(d, full)
    assert restored > expected
    a._monthly(d)
    assert a.ext_premium == expected + restored and a.hypo_premium == 2 * full


def test_refinance_rejects_reserve_buffer_and_pending_overlap_before_sends(household_world):
    from dataclasses import replace
    d, _, make = household_world
    a = make(31, CreditHouseholdAgent)
    b = {key: int(value) for key, value in REFINANCE["base"].items()}
    d.chain.send(d.usdc.functions.mint(a.address, b["seed_usdc"]))
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, b["seed_buck"]])))
    candidate = d.net_credit_ops.assess_issue_candidate(a.address, a._token_ids[0], b["coverage"], b["new_spendable"], b["premium"])
    plan = RefinancePlan(candidate, b["export_buck"], b["minimum_usdc"], b["mortgage_limit"], b["cash_buffer"],
                         b["reserve_floor"], b["switch_cost"], b["penalty_numerator"], b["penalty_denominator"])
    head = d.w3.eth.block_number
    for changed, error in ((replace(plan, reserve_floor=0), "quoted BUCK reserve"),
                            (replace(plan, cash_buffer=0), "bounds"),
                            (replace(plan, export_buck=100 * RAW), "consume the BUCK reserve"),
                            (replace(plan, mortgage_limit=100 * RAW), "bounds"),
                            (replace(plan, reserve_buy_net=RAW), "reserve funding"),
                            (replace(plan, reserve_buy_max_usdc=RAW), "reserve funding"),
                            (replace(plan, candidate=None, switch_cost=0, reserve_buy_net=RAW,
                                     reserve_buy_max_usdc=RAW), "reserve funding"),
                            (replace(plan, reserve_buy_net=RAW, reserve_buy_max_usdc=b["seed_usdc"]), "USDC buffer"),
                            (replace(plan, penalty_numerator=2**256 - 1, penalty_denominator=1), "overflows uint256")):
        with pytest.raises(ValueError, match=error):
            a.start_net_refinance(d, changed, 0, {})
    a._net_repayment_target, a._net_repayment_reserve = RAW, RAW
    with pytest.raises(ValueError, match="buyback must finish"):
        a.start_net_refinance(d, plan, 0, {})
    assert d.w3.eth.block_number == head
    assert a.refinance_machine.events == [] and a.premium_paid == a.external_paid == 0


def test_personal_portfolio_marks_use_actual_cash_debt_receipts_and_all_custody(household_world):
    d, scenario, make = household_world
    debtor, saver, investor = [make(i, cls) for i, cls in enumerate(
        (CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent))]
    chain, buck = d.chain, d.buck

    def transfer(proxy, to, amount):
        chain.send(proxy.functions.exec(buck.address, buck.encode_abi("transfer", args=[to, amount])))

    transfer(d.simlp, debtor.address, 20 * RAW)
    d.net_credit_ops.issue(debtor.address, debtor._token_ids[0], 100 * RAW, 40 * RAW, 10 * RAW, d.pool_acct)
    d.anvil.warp_to(d.w3.eth.get_block("latest")["timestamp"] + YEAR)
    # A payment elsewhere is not a principal retirement. Leave a negative
    # BUCK financial book to exercise signed USD conversion correctly.
    transfer(debtor.proxy, d.simlp.address, 50 * RAW)
    debtor._savings_proxy, debtor._wallet = debtor._new_proxy(d), debtor._new_proxy(d)
    transfer(d.simlp, debtor._savings_proxy.address, 20 * RAW)
    transfer(d.simlp, debtor._wallet.address, 5 * RAW)
    transfer(d.simlp, investor.address, 7 * RAW)
    debtor._deposit_bucks(d, {}, proxy=debtor._savings_proxy)
    investor._deposit_bucks(d, {})
    assert debtor._basket_receipts and investor._basket_receipts
    custody = [debtor.address, debtor._savings_proxy.address, debtor._wallet.address]
    for address, cash in zip(custody, (3, 13 * RAW, 17 * RAW)):
        chain.send(d.usdc.functions.mint(address, cash))
    for i, address, whole in ((0, custody[0], 1), (0, custody[1], 2), (1, custody[2], 3)):
        chain.send(d.tokens[i].functions.mint(address, whole * 10**d.dec[i]))
    debtor.living_arrears, debtor.premium_arrears = 19, 23
    debtor.hypo_living_arrears, debtor.hypo_premium_arrears = 3, 4
    for agent in (debtor, saver, investor):
        agent._started = True  # component observations; the decision loop remains gated

    def ratio(pool_address, token):
        pool = chain.at("UniswapV3Pool", pool_address)
        price = Fraction(pool.functions.slot0().call()[0], 1 << 96) ** 2
        return price if pool.functions.token0().call().lower() == token.address.lower() else 1 / price

    def check(agent):
        addresses = [agent.address] + [p.address for p in (agent._savings_proxy, agent._wallet) if p is not None]
        book = d.net_credit_ops.mark_household(agent.address, agent._token_ids)
        basket = sum(d.basket.functions.valueOf(rid).call() for rid in agent._basket_receipts)
        basis = sum(d.basket.functions.holdings(rid).call()[1] for rid in agent._basket_receipts)
        dollars = sum(d.usdc.functions.balanceOf(a).call() for a in addresses)
        pending = sum(int(sum(token.functions.balanceOf(a).call() for a in addresses) * ratio(pool, token))
                      for token, pool in zip(d.tokens, d.pool_usdc))
        scaled = sum(buck.functions.netBalanceScaled(a).call() for a in addresses)
        valued = int((Fraction(scaled + book.premium_net_scaled + book.relief_scaled, Q)
                      + basket - book.state.issued) * ratio(d.pool_ub, buck))
        block = d.w3.eth.block_number
        counters = (agent.external_paid, agent.hypo_paid, agent._basket_in,
                    agent._basket_shares, dict(agent._receipt_book))
        state = agent.octl_state(d)
        assert d.w3.eth.block_number == block == state["mark_block"]
        assert state["nw"] == dollars + pending + valued - agent.mortgage - agent.living_arrears - agent.premium_arrears
        assert state["drawn"] == book.state.issued and state["held_buck"] == book.state.spendable
        assert state["premium_net"] == book.premium_net_scaled // Q
        assert int(state["relief_scaled"]) == book.relief_scaled
        assert state["basket_at_risk"] == basis and state["basket_mark"] == basket
        assert state["basket_roi"] == ((basket - basis) / basis if basis else None)
        assert state["pending_usd"] == pending
        assert state["hypo"] == agent.hypo_cash - agent.hypo_mortgage - agent.hypo_living_arrears - agent.hypo_premium_arrears
        assert counters == (agent.external_paid, agent.hypo_paid, agent._basket_in,
                            agent._basket_shares, dict(agent._receipt_book))
        return state

    before = check(debtor)
    assert before["aux_cash"] == 30 * RAW and before["insurance_age"] > 0
    assert before["nw"] < 0 and before["pending_usd"] > 0
    assert check(saver)["basket_roi"] is None and check(investor)["basket_roi"] is not None
    rid = debtor._basket_receipts[0]
    shares = d.basket.functions.holdings(rid).call()[0]
    redeemed = debtor._proxy_exec(d, d.basket.address, d.basket.encode_abi(
        "redeemShares", args=[rid, shares // 2, 0]), proxy=debtor._savings_proxy)
    after = check(debtor)
    assert after["basket_at_risk"] < debtor._basket_in  # deliberately stale in-memory trade book
    cut = d.basket.events.Redeemed().process_receipt(redeemed, errors=DISCARD)[0]["args"]["treasuryBuck"]
    assert cut > 0
    # Mark-to-market precedes the gain fee AND the existing BUCK conversion
    # charge. Reconcile the realized loss to the remaining receipt plus cash
    # actually returned, rather than assuming a cost-free redemption.
    loss = before["nw"] - after["nw"]
    assert loss >= int(cut * ratio(d.pool_ub, buck))
    assert loss == int((before["basket_mark"] + before["savings_held"]
                        - after["basket_mark"] - after["savings_held"]) * ratio(d.pool_ub, buck))

    # Full-width money is never narrowed through floats in a financial mark.
    huge = 2**100 + 123
    chain.send(d.usdc.functions.mint(debtor.address, huge))
    assert check(debtor)["nw"] == after["nw"] + huge
    source_id = d.credit.functions.totalSupply().call()
    chain.send(d.credit.functions.createCredit(d.simlp.address, 0, 2**62, 0, 0, 0, 0, 0))
    d.net_credit_ops.issue(d.simlp.address, source_id, 2**62, 2**60 + 17, 0, "0x" + "00" * 20)
    transfer(d.simlp, debtor.address, 2**60 + 17)
    assert check(debtor)["held_buck"] > 2**53

    # A report cannot silently replace a missing guarded claim mark with zero.
    fund = d.net_stack.fund
    nominal = buck.functions.fundNominalForNet(10 * RAW).call()
    chain.send(fund.functions.invest(10 * RAW, nominal, 1, 1), sender=d.gov)
    with pytest.raises(Exception, match="MarkUnavailable|0x417c0238"):
        debtor.octl_state(d)
    from alberta_buck.sim.basket_machine import publish_jubilee_valuation
    assert publish_jubilee_valuation(d)["status"] == 1
    check(debtor)

    # The existing snapshot must retain the exact mark, including an investor
    # whose receipts belong to its main account rather than a savings proxy.
    from alberta_buck.sim.snapshot import Snapshotter
    snap = Snapshotter(d, scenario)
    snap.capture(0, {"directTrades": 0, "cycleTrades": 0}, [debtor, saver, investor], 0)
    assert snap.frames[0]["octl"][0]["basket_at_risk"] == check(debtor)["basket_at_risk"]
    assert len(snap.frames[0]["basket_positions"]) == 2
    assert all(a["mark_block"] == d.w3.eth.block_number for a in snap.frames[0]["octl"])
    wallet = debtor._wallet
    debtor._wallet = debtor._savings_proxy
    with pytest.raises(ValueError, match="distinct"):
        debtor.octl_state(d)
    debtor._wallet = wallet
    assert not chain.mismatches


def test_readable_books_do_not_allow_unimplemented_interactive_economy_to_finish(household_world):
    from alberta_buck.sim.driver import SimDriver
    d, scenario, make = household_world
    agent = make(0, CreditHouseholdAgent)
    assert agent.octl_state(d)["mark_block"] == d.w3.eth.block_number
    driver = SimDriver.__new__(SimDriver)
    driver.d, driver.scenario, driver.anvil = d, scenario, d.anvil
    driver.day, driver.tick, driver.done = 0, 0, False
    driver.ts, driver.tick_secs = d.w3.eth.get_block("latest")["timestamp"], 1
    driver.ctr = {"directTrades": 0, "cycleTrades": 0}
    driver._begin_day = lambda: None
    driver._whale_tick, driver.whales, driver.arbs = -1, [], [agent]
    driver._prng = random.Random(1)
    driver.snap = SimpleNamespace(meta={"run": {"completed": False}})
    with pytest.raises(NotImplementedError, match="decisions await"):
        driver.step_tick()
    assert driver.snap.meta["run"]["completed"] is False and not driver.done and driver.tick == 0
    assert not agent._started and agent.income_total == agent.external_paid == 0


@pytest.mark.parametrize("case", CALENDAR["cases"], ids=lambda c: c["name"])
def test_calendar_cash_flows_recover_once_without_stopping_the_shadow(household_world, monkeypatch, case):
    from alberta_buck.sim.household_calendar import MortgageBook
    from buck_core.session import _txhex
    d, scenario, make = household_world
    a = make(71, CreditHouseholdAgent, settings=CALENDAR["settings"])
    m, ctr = a.calendar_machine, {}
    opening, income, living, premium = (int(CALENDAR[k]) for k in ("opening", "income", "living", "full_premium"))
    initial_debt = a.mortgage
    assert a.advance_net_calendar(d, 0, ctr) is False
    assert a._started and a.hypo_cash == opening == d.usdc.functions.balanceOf(a.address).call()
    assert a.income_total == a.external_paid == 0
    assert not m.active and m.income_due == 0
    calls, failed, receipts = {}, [], {}
    mint, execute, record = m._mint, d.net_settlement_ops.execute, m.record_mortgage

    def submit(phase, dispatch, *args):
        calls[phase] = calls.get(phase, 0) + 1
        if case["phase"] == phase and case["fault"] == "unknown":
            raise RuntimeError("submission outcome unknown")
        if phase == "income" and case["fault"] == "bad-mint-evidence":
            args = (args[0], args[1] + 1)
        if phase == "income" and case["fault"] == "revert" and not failed:
            from buck_core.session import Expect
            failed.append(True)
            return d.chain.send(d.usdc.functions.transfer(a.SINK, 2**256 - 1), expect=Expect.EITHER)
        receipt = dispatch(*args)
        receipts[phase] = receipt
        if case["phase"] == phase and case["fault"] == "lost" and not failed:
            failed.append(True)
            raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
        return receipt

    monkeypatch.setattr(m, "_mint", lambda d, amount: submit("income", mint, d, amount))
    monkeypatch.setattr(d.net_settlement_ops, "execute", lambda *args: submit(m.flow["phase"], execute, *args))
    def dated(*args):
        if case["fault"] == "date-unavailable" and not failed:
            failed.append(True)
            raise RuntimeError("receipt block temporarily unavailable")
        return record(*args)
    monkeypatch.setattr(m, "record_mortgage", dated)

    d.anvil.warp_to(m.epoch + 30 * 86400)
    pending = a.advance_net_calendar(d, 30, ctr)
    assert pending == (case["fault"] != "none")
    assert a.hypo_paid == a.payment and a.hypo_living == living and a.hypo_premium == premium
    assert a.hypo_cash == opening + income - living - premium - a.payment
    sent = calls.copy()
    if pending:
        assert m.status()["status"] == ("refused" if case["fault"] == "revert" else "pending")
        assert m.flow["phase"] == case["phase"]
        with pytest.raises(ValueError, match="calendar must finish"):
            a.start_net_refinance(d, None, 30, ctr)
        if case["fault"] in ("unknown", "bad-mint-evidence"):
            assert m.income_due == income and a.income_total == 0
            d.anvil.warp_to(m.epoch + 60 * 86400)
            a.act(d, scenario, 60, 0, ctr)
            assert calls == sent and a.income_total == a.external_paid == 0
            assert d.usdc.functions.balanceOf(a.address).call() == opening + (income + 1 if case["fault"] == "bad-mint-evidence" else 0)
            assert m.income_due == 2 * income and len(m.queue) == 2
            assert a.hypo_paid == 2 * a.payment and a.hypo_interest > 0
            assert a.living_arrears == 2 * living and a.premium_arrears == 2 * premium
            assert ("hash unavailable" if case["fault"] == "unknown" else "does not match terms") in a.last_error
            return
        assert m.flow["hash"]
        if case["phase"] == "mortgage":
            assert a.external_paid == 0 and m.status()["mortgage_provisional"]
        get_receipt = d.w3.eth.get_transaction_receipt
        if case["fault"] != "revert":
            with monkeypatch.context() as patch:
                patch.setattr(d.w3.eth, "get_transaction_receipt", lambda h: {**get_receipt(h), "transactionHash": bytes(32)})
                assert a.advance_net_calendar(d, 30, ctr)
        assert calls == sent
        d.anvil.warp_to(m.epoch + 35 * 86400)
        a.act(d, scenario, 35, 0, ctr)
    expected_calls = {phase: 1 for phase in ("income", "living", "insurance", "mortgage")}
    expected_calls["income"] += case["fault"] == "revert"
    assert calls == expected_calls
    assert a.external_paid == a.hypo_paid == a.payment
    assert a.income_total == income and a.living_paid == living and a.ext_premium == premium
    assert a.living_arrears == a.premium_arrears == m.income_due == 0
    assert d.usdc.functions.balanceOf(a.SINK).call() == living + premium + a.payment
    assert d.usdc.functions.balanceOf(a.address).call() == opening + income - living - premium - a.payment
    bank_day = 35 if pending and case["phase"] != "mortgage" else 30
    actual = MortgageBook(initial_debt, 60_000_000)
    actual.advance(bank_day)
    actual.record_payment("bank", bank_day, a.payment)
    actual.advance(m.day)
    assert m.actual.state == actual.state
    shadow = MortgageBook(initial_debt, 60_000_000)
    shadow.advance(30)
    shadow.record_payment("shadow", 30, a.payment)
    shadow.advance(m.day)
    assert m.shadow.state == shadow.state
    assert m.status()["mortgage_provisional"] is False
    events, count = json.loads(json.dumps(a.refinance_machine.events)), len(a.refinance_machine.events)
    assert all(e["kind"] == "calendar" for e in events)
    assert all("counterfactual_mortgage" in e["accounts"] for e in events)
    assert int(next(e for e in events if e.get("operation") == "mortgage" and e["status"] == "confirmed")["effective_day"]) == bank_day
    assert a.advance_net_calendar(d, m.day, ctr) is False
    assert calls == expected_calls
    assert len(a.refinance_machine.events) == count
    d.anvil.warp_to(m.epoch + 60 * 86400)
    assert a.advance_net_calendar(d, 60, ctr) is False
    assert a.external_paid == a.hypo_paid == 2 * a.payment
    assert a.income_total == 2 * income
    assert calls == {phase: count + 1 for phase, count in expected_calls.items()}
    assert not d.chain.mismatches


def test_calendar_charges_only_unactivated_external_insurance(household_world):
    d, _, make = household_world
    a = make(72, CreditHouseholdAgent, settings=CALENDAR["settings"])
    a.advance_net_calendar(d, 0, {})
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, 20 * RAW])))
    d.net_credit_ops.issue(a.address, a._token_ids[0], 100 * RAW, 40 * RAW, 10 * RAW, d.pool_acct)
    full = int(CALENDAR["full_premium"])
    expected = (full * (a._face0 - 100 * RAW) + a._face0 - 1) // a._face0
    d.anvil.warp_to(a.calendar_machine.epoch + 30 * 86400)
    assert a.advance_net_calendar(d, 30, {}) is False
    assert a.ext_premium == expected < a.hypo_premium == full
    assert a.mortgage == a.hypo_mortgage


def test_calendar_rejects_skipped_paydays_and_a_label_without_elapsed_chain_time(household_world):
    d, _, make = household_world
    a = make(74, CreditHouseholdAgent, settings=CALENDAR["settings"])
    a.advance_net_calendar(d, 0, {})
    m = a.calendar_machine
    before = (a.mortgage, a.hypo_cash, a.hypo_paid, a.living_arrears, len(a.refinance_machine.events))
    with pytest.raises(ValueError, match="chain clock"):
        a.advance_net_calendar(d, 30, {})
    assert before == (a.mortgage, a.hypo_cash, a.hypo_paid, a.living_arrears, len(a.refinance_machine.events))
    d.anvil.warp_to(m.epoch + 31 * 86400)
    with pytest.raises(ValueError, match="each payday"):
        a.advance_net_calendar(d, 31, {})
    assert before == (a.mortgage, a.hypo_cash, a.hypo_paid, a.living_arrears, len(a.refinance_machine.events))


@pytest.mark.parametrize("fault", ["lost", "date-unavailable"])
def test_calendar_dates_refinance_receipts_and_keeps_its_paydays(household_world, monkeypatch, fault):
    from alberta_buck.sim.household_calendar import MortgageBook
    from buck_core.session import _txhex
    d, scenario, make = household_world
    a = make(73, CreditHouseholdAgent, settings=CALENDAR["settings"])
    m, ctr = a.calendar_machine, {}
    a.advance_net_calendar(d, 0, ctr)
    b = {k: int(v) for k, v in REFINANCE["base"].items()}
    initial = a.mortgage
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, b["seed_buck"]])))
    a._proxy_exec(d, d.usdc.address, d.usdc.encode_abi("approve", args=[d.swap_guard.address, 2**256 - 1]))
    a._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.permit2.address, 2**256 - 1]))
    a._proxy_exec(d, d.permit2.address, d.permit2.encode_abi("approve", args=[d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1]))
    c = d.net_credit_ops.assess_issue_candidate(a.address, a._token_ids[0], b["coverage"], b["new_spendable"], b["premium"])
    plan = RefinancePlan(c, *(b[k] for k in ("export_buck", "minimum_usdc", "mortgage_limit", "cash_buffer",
                                           "reserve_floor", "switch_cost", "penalty_numerator", "penalty_denominator")))
    execute, record, sends, dates = d.net_settlement_ops.execute, m.record_mortgage, [], []
    def bank(*args):
        receipt = execute(*args)
        sends.append(receipt)
        if fault == "lost" and len(sends) == 1:
            raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
        return receipt
    def dated(*args):
        dates.append(True)
        if fault == "date-unavailable" and len(dates) == 1:
            raise RuntimeError("receipt block temporarily unavailable")
        return record(*args)
    monkeypatch.setattr(d.net_settlement_ops, "execute", bank)
    monkeypatch.setattr(m, "record_mortgage", dated)
    a.start_net_refinance(d, plan, 0, ctr)
    assert a.refinance_machine.status()["status"] == "pending"
    assert a.refinance_machine.flow["hash"] and m.status()["mortgage_provisional"]
    assert a.external_paid == 0
    payment_receipt = sends[0]
    payment_block = d.w3.eth.get_block(payment_receipt["blockNumber"])
    d.anvil.warp_to(m.epoch + 30 * 86400)
    # Recover the bank first, while calendar preparation independently pays
    # the shadow and schedules real salary/bills. No owner salary is sent yet.
    a.act(d, scenario, 30, 0, ctr)
    assert d.w3.eth.get_block(payment_receipt["blockNumber"])["timestamp"] == m.epoch
    assert d.w3.eth.get_block(payment_block["hash"])["timestamp"] == m.epoch
    assert a.external_paid == b["mortgage_limit"] and len(sends) == 1
    assert a.refinance_machine.status()["status"] == "complete"
    assert a.income_total == 0 and m.income_due == int(CALENDAR["income"])
    assert a.hypo_paid == a.payment
    expected = MortgageBook(initial, 60_000_000)
    expected.record_payment("refinance", 0, b["mortgage_limit"])
    expected.advance(30)
    assert m.actual.state == expected.state
    a.act(d, scenario, 30, 0, ctr)
    assert a.external_paid == b["mortgage_limit"] + a.payment and len(sends) == 4
    assert a.switch_paid == b["switch_cost"] and a.refis == 1
    assert not m.active and m.income_due == 0
