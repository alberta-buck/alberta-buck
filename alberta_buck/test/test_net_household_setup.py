"""Existing household setup, credit/portfolio books and the remaining decision gate."""
from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
import random
from types import SimpleNamespace

import pytest
from web3.logs import DISCARD
from buck_core import ReleasePending, TransactionReceiptPending, TransactionReverted, observe_net_market, NetBasketOps
from buck_core.session import _txhex

from alberta_buck.sim import experiment as expmod, household_policy as policy, identity as idmod
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.credit_households import CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent
from alberta_buck.sim.deploy import deploy
from alberta_buck.sim.gauge import active_reserves
from alberta_buck.sim.net_household import RefinancePlan
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING

RAW = 10**6
Q = 10**36
YEAR = 365 * 86400 + 6 * 3600
REFINANCE = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-refinance.json").read_text())
SURPLUS_FILE = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-surplus.json").read_text())
SURPLUS, SURPLUS_FLOW = SURPLUS_FILE["lifecycle"], SURPLUS_FILE["flow"]
WALLET = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-wallet.json").read_text())
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
        def make(i, cls, *, assets=1, settings=None, arrive_day=None, member=None):
            agent = cls(i)
            if member is not None:
                agent.member = member
            if arrive_day is not None:
                agent.arrive_day = arrive_day
            # Fixture households are paid on the day of the month they arrive, so a
            # first payday is thirty days later unless a case pins another day.
            agent.settings = dict(home_k=1000, premium_bp=100, asset_count=assets,
                                  pay_day=(arrive_day or 0) % 30) | (settings or {})
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


def test_release_frees_coverage_no_principal_needs_and_refunds_its_deposit_share(household_world, monkeypatch):
    """burn(0) through the SDK: quote as the owner, release, authenticate; principal never moves."""
    d, scenario, make = household_world
    chain = d.chain
    saver, other = make(0, CreditSaverAgent, assets=2), make(1, CreditSaverAgent)
    first, second = saver._token_ids
    ops = d.net_credit_ops
    chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[saver.address, 20 * RAW])))
    ops.issue(saver.address, first, 100 * RAW, 40 * RAW, 10 * RAW, d.pool_acct)
    ops.issue(saver.address, second, 30 * RAW, 0, 3 * RAW, d.pool_acct)        # only its deposit is drawn
    k = d.kctrl.functions.currentBuckK().call()
    before = ops.household(saver.address, saver._token_ids)
    assert before.issued == 53 * RAW and before.premium_nominal == 13 * RAW
    held, pool, supply = (d.buck.functions.balanceOf(saver.address).call(), d.buck.functions.balanceOf(d.pool_acct).call(),
                          d.buck.functions.totalIssued().call())
    seconds = d.buck.functions.claimSeconds().call()

    with pytest.raises(ValueError, match="duplicate"):
        ops.quote_release(saver.address, [first, first])
    with pytest.raises(ValueError, match="foreign"):
        ops.release(other.address, [first])
    quote = ops.quote_release(saver.address, [second, first])
    keep = lambda principal: -(-principal * 10**18 // k)                       # ceil(principal / K): assets marked at face
    assert quote.coverage == (30 * RAW - keep(3 * RAW)) + (100 * RAW - keep(50 * RAW)) > 0
    assert d.w3.eth.block_number == quote.block_number                          # a quote sends nothing

    # A lost reply is an uncertain outcome with its hash; the receipt authenticates the same release.
    real, lost = ops.execute, {}
    def lose(owner, target, method, args):
        lost["receipt"] = real(owner, target, method, args)
        raise TransactionReceiptPending(_txhex(lost["receipt"]["transactionHash"]))
    monkeypatch.setattr(ops, "execute", lose)
    with pytest.raises(ReleasePending) as pending:
        ops.release(saver.address, [second, first])
    monkeypatch.setattr(ops, "execute", real)
    assert pending.value.transaction_hash and not pending.value.reverted
    with pytest.raises(ValueError, match="selection"):
        ops.release_from_receipt(saver.address, [first], lost["receipt"], expected_hash=pending.value.transaction_hash)
    with pytest.raises(ValueError, match="hash"):
        ops.release_from_receipt(saver.address, [second, first], lost["receipt"], expected_hash="0x" + "11" * 32)
    done = ops.release_from_receipt(saver.address, [second, first], lost["receipt"],
                                    expected_hash=pending.value.transaction_hash)
    assert [r[0] for r in done.released] == [second, first] and d.kctrl.functions.currentBuckK().call() == k
    assert (done.coverage, done.refund) == (quote.coverage, quote.refund)
    assert done.refund == sum(premium * coverage // active for (_, coverage, _), premium, active in
                              zip(done.released, (3 * RAW, 10 * RAW), (30 * RAW, 100 * RAW)))

    after = ops.household(saver.address, saver._token_ids)
    assert after.issued == before.issued and d.buck.functions.totalIssued().call() == supply
    assert d.buck.functions.claimSeconds().call() == seconds
    assert after.premium_nominal == before.premium_nominal - done.refund
    assert d.buck.functions.balanceOf(saver.address).call() == held + done.refund
    assert d.buck.functions.balanceOf(d.pool_acct).call() == pool - done.refund
    for token_id, coverage, _ in done.released:
        face, active, _ = d.credit.functions.creditInfo(token_id).call()
        principal = d.credit.functions.issuanceOf(token_id).call()[0]
        assert active == (30 * RAW if token_id == second else 100 * RAW) - coverage <= face
        assert d.credit.functions.currentValue(token_id).call() * k // 10**18 >= principal   # still fully backed
    # Nothing is left to release: a second call succeeds and releases nothing.
    again = ops.release(saver.address, saver._token_ids)
    assert again.released == () and again.coverage == again.refund == 0
    assert ops.quote_release(saver.address, saver._token_ids).coverage == 0
    assert not chain.mismatches


def test_primary_basket_ops_authenticate_topups_and_partial_net_redemptions(household_world, monkeypatch):
    d, _, make = household_world
    a, stranger = make(90, CreditHouseholdAgent), make(91, CreditHouseholdAgent)
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, 200 * RAW])))
    a._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.basket.address, 2**256 - 1]))
    ops = NetBasketOps(d.chain, d.basket, d.buck, d.net_market_ops.execute, d.net_simulate)
    supply = d.buck.functions.totalIssued().call()
    def minimum(amount):
        return max(1, amount * 10**18 // d.basket.functions.sharePrice().call() * 9900 // 10_000)
    first_plan = ops.prepare_deposit(a.address, 20 * RAW, minimum(20 * RAW))
    first = ops.deposit(first_plan)
    assert first.received == first.basis == 20 * RAW and first.shares_added == first.shares
    token_id = first.token_id
    again = ops.deposit_from_receipt(first_plan, first.receipt, expected_hash=_txhex(first.receipt["transactionHash"]))
    assert again == first  # authenticating evidence never submits or books another deposit
    with pytest.raises(ValueError, match="hash"):
        ops.deposit_from_receipt(first_plan, first.receipt, expected_hash="0x" + "00" * 32)
    with pytest.raises(ValueError, match="foreign"):
        ops.prepare_deposit(stranger.address, RAW, 1, token_id=token_id)
    with pytest.raises(ValueError, match="positive"):
        ops.prepare_deposit(a.address, RAW, 0)
    topup = ops.prepare_deposit(a.address, 10 * RAW, minimum(10 * RAW), token_id=token_id)
    execute, sent = ops.execute, []
    def lost(*args):
        receipt = execute(*args)
        sent.append(receipt)
        raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
    with monkeypatch.context() as patch:
        patch.setattr(ops, "execute", lost)
        with pytest.raises(TransactionReceiptPending) as pending:
            ops.deposit(topup)
    updated = ops.deposit_from_receipt(topup, sent[0], expected_hash=pending.value.transaction_hash)
    assert updated.token_id == token_id and updated.basis == 30 * RAW
    assert updated.shares == first.shares + updated.shares_added
    assert d.buck.functions.totalIssued().call() == supply
    refused = replace(topup, prior_shares=updated.shares, prior_basis=updated.basis, minimum_shares=2**120)
    with pytest.raises(TransactionReverted):
        ops.deposit(refused)
    assert d.basket.functions.holdings(token_id).call() == [updated.shares, updated.basis]
    d.anvil.warp_to(d.w3.eth.get_block("latest")["timestamp"] + 30 * 86400)
    d.chain.send(d.kctrl.functions.compute())
    shares = updated.shares // 4
    worth = d.basket.functions.valueOf(token_id).call() * shares // updated.shares
    redemption = ops.prepare_redemption(a.address, token_id, shares, worth * 9900 // 10_000)
    # This backend refuses an eth_call whose sender has code before the exit
    # runs. That is not the basket's guard; the proxy's own dispatch is used.
    with pytest.raises(Exception, match="RejectCallerWithCode"):
        NetBasketOps(d.chain, d.basket, d.buck, d.net_market_ops.execute).preview_redemption(a.address, token_id, shares)
    def observed():
        return (d.w3.eth.block_number, d.basket.functions.holdings(token_id).call(), d.basket.functions.totalShares().call(),
                d.buck.functions.netBalanceScaled(a.address).call(), d.buck.functions.netBalanceScaled(d.basket.address).call(),
                d.buck.functions.totalIssued().call(), d.kctrl.functions.currentBuckK().call())
    before = observed()
    preview = ops.preview_redemption(a.address, token_id, shares)
    assert preview["paid"] >= redemption.minimum_buck and preview["shares_retired"] == shares
    assert preview["position_id"] == token_id and preview["shares_remaining"] == updated.shares - shares
    assert observed() == before, "a simulated exit changes neither holdings, cash, K nor chain height"
    with pytest.raises(ValueError, match="foreign"):
        ops.preview_redemption(stranger.address, token_id, shares)
    with pytest.raises(ValueError, match="exceeds"):
        ops.preview_redemption(a.address, token_id, updated.shares + 1)
    with monkeypatch.context() as patch:
        patch.setattr(ops, "execute", lost)
        with pytest.raises(TransactionReceiptPending) as pending:
            ops.redeem(redemption)
    reduced = ops.redemption_from_receipt(redemption, sent[-1], expected_hash=pending.value.transaction_hash)
    assert reduced.paid >= redemption.minimum_buck and reduced.shares_retired == shares
    assert reduced.paid == preview["paid"], "the preview ran the same exit the next block then executed"
    assert reduced.shares_remaining == updated.shares - shares and 0 < reduced.basis_remaining < updated.basis
    assert reduced.in_kind == ()
    transfers = d.buck.events.NetValueTransfer().process_receipt(reduced.receipt, errors=DISCARD)
    nominal = sum(e["args"]["nominal"] for e in transfers if e["args"]["to"].lower() == a.address.lower())
    assert nominal > reduced.paid, "aged Carrying basket grosses up the actual net cash payout"
    assert d.buck.functions.totalIssued().call() == supply
    assert d.net_credit_ops.household(a.address, a._token_ids).issued == 0
    assert not d.chain.mismatches


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
    original_bank_execute = d.net_settlement_ops.execute
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
    assert a.refinance_closed is completed
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
    if case.get("retry_payment"):
        assert a.joined and not a.refinance_closed and machine.payment_due
        before_supply = d.buck.functions.totalIssued().call()
        before_calls = dict(calls)
        with pytest.raises(ValueError, match="bank payment due"):
            a.start_net_refinance(d, plan, 3, ctr)
        attempts = []
        def lose_retry(*args):
            receipt = original_bank_execute(*args)
            attempts.append(receipt)
            raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
        monkeypatch.setattr(d.net_settlement_ops, "execute", lose_retry)
        a.retry_net_refinance_payment(d, 3, ctr)
        assert machine.active and not machine.payment_due and not a.refinance_closed
        a.act(d, scenario, 4, 0, ctr)
        assert machine.status()["status"] == "complete" and a.refinance_closed
        assert calls == before_calls and len(attempts) == 1
        assert d.buck.functions.totalIssued().call() == before_supply
        assert a.switch_paid == b["switch_cost"] and a.refis == 1
        assert a.external_paid == b["mortgage_limit"]
        assert ctr["rtrRefis"] == 1 and "rtrTopUps" not in ctr
        with pytest.raises(ValueError, match="already paid"):
            a.start_net_refinance(d, plan, 4, ctr)
        with pytest.raises(ValueError, match="no refused bank payment"):
            a.retry_net_refinance_payment(d, 4, ctr)
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


@pytest.mark.parametrize("case", REFINANCE["resumption"]["cases"], ids=lambda c: c["name"])
def test_refinance_remainder_keeps_confirmed_steps_and_reprices_only_cash(household_world, monkeypatch, case):
    from alberta_buck.sim import net_household
    d, scenario, make = household_world
    a = make(51, CreditHouseholdAgent, settings=REFINANCE["resumption"]["settings"])
    a.advance_net_calendar(d, 0, {})
    b = {k: int(v) for k, v in REFINANCE["base"].items()}
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, b["seed_buck"]])))
    a._proxy_exec(d, d.usdc.address, d.usdc.encode_abi("approve", args=[d.swap_guard.address, 2**256 - 1]))
    a._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.permit2.address, 2**256 - 1]))
    a._proxy_exec(d, d.permit2.address, d.permit2.encode_abi(
        "approve", args=[d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1]))
    c = d.net_credit_ops.assess_issue_candidate(a.address, a._token_ids[0], b["coverage"], b["new_spendable"], b["premium"])
    initial = RefinancePlan(c, b["export_buck"], 5000 * RAW if case["phase"] == "export" else b["minimum_usdc"],
        b["mortgage_limit"], b["cash_buffer"], b["reserve_floor"], int(case.get("switch_cost", b["switch_cost"])),
        b["penalty_numerator"], b["penalty_denominator"])
    bank_send = d.net_settlement_ops.execute
    with monkeypatch.context() as patch:
        if case["phase"] == "mortgage":
            patch.setattr(d.net_settlement_ops, "execute", lambda owner, target, name, args:
                          bank_send(owner, target, name, [a.SINK, 2**256 - 1]))
        a.start_net_refinance(d, initial, 0, {})
    m, ctr = a.refinance_machine, {}
    assert m.settlement_due and m.flow["phase"] == case["phase"]
    assert a.joined and not a.refinance_closed and a.refis == 0
    principal, supply = d.credit.functions.issuanceOf(c.token_id).call()[0], d.buck.functions.totalIssued().call()
    assert principal == b["new_spendable"] + b["premium"]
    if case.get("outside_principal"):
        amount = int(case["outside_principal"])
        paid = d.net_settlement_ops.pay_exact(a.address, a.SINK, amount)
        a.calendar_machine.record_mortgage(d, paid.receipt, amount)
    if case.get("held_buck"):
        held = d.buck.functions.balanceOf(a.address).call()
        a._proxy_exec(d, d.buck.address, d.buck.encode_abi("transfer", args=[d.simlp.address, held - int(case["held_buck"])]))
    if case.get("wait") == "cash" or "leave_cash" in case:
        penalty = (initial.mortgage_limit * initial.penalty_numerator + initial.penalty_denominator - 1) // initial.penalty_denominator
        leave = int(case.get("leave_cash", initial.cash_buffer + initial.mortgage_limit + initial.switch_cost + penalty - 1))
        cash = d.usdc.functions.balanceOf(a.address).call()
        a._proxy_exec(d, d.usdc.address, d.usdc.encode_abi("transfer", args=[d.simlp.address, cash - leave]))

    def unavailable(*_args, **_kwargs):
        raise AssertionError("resumption must not quote or issue new credit")
    for name in ("issue", "issue_candidate", "assess_issue_candidate", "issue_coverage_menu", "quote_global_relief_rate"):
        monkeypatch.setattr(d.net_credit_ops, name, unavailable)
    if case["phase"] == "mortgage":
        monkeypatch.setattr(net_household, "observe_net_market", unavailable)
    options = {"expected_wallet_growth": .5} if case.get("wait") == "economics" else {}
    before = d.w3.eth.block_number, len(m.events), m.flow["plan"], a.mortgage
    quote = a.assess_net_refinance_remainder(d, 0, **options)
    assert before == (d.w3.eth.block_number, len(m.events), m.flow["plan"], a.mortgage)
    assert quote["context"]["new_credit"] is False
    assert m.flow["origin"] is initial and m.status()["revision"] == 0
    if case.get("wait"):
        assert quote["plan"] is None
        block = d.w3.eth.block_number
        a.resume_net_refinance(d, 0, ctr, **options)
        assert m.settlement_due and a.funding_pending and a.last_action == "refinance-wait"
        assert d.w3.eth.block_number == block and m.status()["revision"] == 0
    day = case.get("days", 0)
    if day:
        d.anvil.warp_to(a.calendar_machine.epoch + day * 86400)
        a.advance_net_calendar(d, day, ctr)
        assert a.income_total > 0 and a.hypo_paid > 0, "known waiting intentions must not freeze wages/bills"
    quote = a.assess_net_refinance_remainder(d, day)
    plan = quote["plan"]
    assert plan is not None, quote["context"]
    assert plan.candidate is None and plan.reserve_buy_net == plan.reserve_buy_max_usdc == 0
    assert plan.mortgage_limit <= min(initial.mortgage_limit, a.mortgage)
    assert plan.switch_cost == initial.switch_cost and plan.cash_buffer >= initial.cash_buffer
    assert plan.reserve_floor == initial.reserve_floor
    if case["phase"] == "export" and plan.mortgage_limit:
        assert 0 < plan.export_buck < initial.export_buck
        assert quote["context"]["score"]["parts"]["relief"] == quote["context"]["score"]["parts"]["premium"] == 0
    if case.get("held_buck"):
        assert plan.export_buck < int(case["held_buck"]) - initial.reserve_floor
    bank_before, debt_before, paid_before = d.usdc.functions.balanceOf(a.SINK).call(), a.mortgage, a.external_paid
    events_before = len(m.events)
    market_send, bank_send = d.net_market_ops.execute, d.net_settlement_ops.execute
    sends = []
    def intercept(dispatch):
        def send(*args):
            phase = m.flow["phase"]
            sends.append(phase)
            if case.get("unknown") == phase:
                raise RuntimeError("submission outcome unknown")
            receipt = dispatch(*args)
            if case.get("lost") == phase:
                raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
            return receipt
        return send
    monkeypatch.setattr(d.net_market_ops, "execute", intercept(market_send))
    monkeypatch.setattr(d.net_settlement_ops, "execute", intercept(bank_send))
    a.resume_net_refinance(d, day, ctr)
    if case.get("lost") or case.get("unknown"):
        assert m.active and m.flow["phase"] == case.get("lost", case.get("unknown"))
        assert a.calendar_machine.status()["mortgage_provisional"] == bool(m.flow["phase"] == "mortgage" and plan.mortgage_limit)
        with pytest.raises(ValueError, match="refused settlement"):
            a.resume_net_refinance(d, day, ctr)
        before_sends = list(sends)
        a.act(d, scenario, day, 0, ctr)
        if case.get("unknown"):
            assert m.active and m.flow["hash"] is None and sends == before_sends
            assert "hash unavailable" in a.last_error
            assert a.mortgage == debt_before and a.external_paid == paid_before and a.switch_paid == 0
            return
    assert m.status()["status"] == "complete" and not m.settlement_due
    assert m.status()["revision"] == 1 and m.flow["origin"] is initial
    assert sends.count("export") <= 1 and sends.count("mortgage") <= 1
    assert d.buck.functions.totalIssued().call() == supply
    assert d.credit.functions.issuanceOf(c.token_id).call()[0] == principal
    penalty = (plan.mortgage_limit * plan.penalty_numerator + plan.penalty_denominator - 1) // plan.penalty_denominator
    assert d.usdc.functions.balanceOf(a.SINK).call() - bank_before == plan.mortgage_limit + plan.switch_cost + penalty
    assert a.mortgage == debt_before - plan.mortgage_limit and a.external_paid == paid_before + plan.mortgage_limit
    assert a.switch_paid == plan.switch_cost and a.penalty_paid == penalty
    assert a.refis == (1 if plan.mortgage_limit else 0)
    assert a.refinance_closed is bool(plan.mortgage_limit or plan.switch_cost)
    confirmed = [e["operation"] for e in m.events[events_before:] if e["status"] == "confirmed"]
    assert confirmed == sends
    if not plan.mortgage_limit and not plan.switch_cost:
        assert not sends and a.last_action == "refinance-satisfied"
    with pytest.raises(ValueError, match="refused settlement"):
        a.resume_net_refinance(d, day, ctr)


def test_staged_refinance_pays_one_full_closing_and_only_issues_each_bounded_leg(household_world):
    d, _, make = household_world
    spec = REFINANCE["programme"]
    a = make(50, CreditHouseholdAgent, settings=spec["settings"])
    d.chain.send(d.usdc.functions.mint(a.address, int(spec["seed_usdc"])))
    a._proxy_exec(d, d.usdc.address, d.usdc.encode_abi("approve", args=[d.swap_guard.address, 2**256 - 1]))
    a._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.permit2.address, 2**256 - 1]))
    a._proxy_exec(d, d.permit2.address, d.permit2.encode_abi(
        "approve", args=[d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1]))
    block, mortgage, supply = d.w3.eth.block_number, a.mortgage, d.buck.functions.totalIssued().call()
    a.p["utilization"] = spec["insufficient_utilization"]
    declined = a.assess_net_refinance(d, 1, **spec["forecasts"])
    assert declined["plan"] is None, "future K/capacity must not finance the programme"
    a.p["utilization"] = spec["settings"]["utilization"]
    result = a.assess_net_refinance(d, 1, **spec["forecasts"])
    plan, v, ctx = result["plan"], result["verdict"], result["context"]
    assert d.w3.eth.block_number == block and a.mortgage == mortgage
    assert plan is not None, ctx
    assert ctx["reason"] == "worthwhile-staged-refinance"
    assert v.go and ctx["programme"]["score"]["go"]
    assert ctx["programme"]["tranches"] > 1
    fixed = spec["settings"]["switch_k"] * 1000
    assert plan.switch_cost == fixed * RAW
    allocated = ctx["programme"]["fixed_cost_allocated"]
    assert 0 < allocated < fixed
    assert v.value - fixed + allocated < 0, "a standalone first tranche would decline the full startup fee"
    assert -ctx["programme"]["score"]["parts"]["fixed"] == fixed
    assert ctx["programme"]["economics"]["new_spendable"] > ctx["selected"]["economics"]["new_spendable"]
    a.start_net_refinance(d, plan, 1, {}, context=ctx)
    assert a.refinance_machine.status()["status"] == "complete", a.last_error
    assert a.refinance_closed and a.switch_paid == fixed * RAW
    assert a.mortgage == mortgage - plan.mortgage_limit
    assert a.hypo_mortgage == mortgage
    assert d.buck.functions.totalIssued().call() == supply + plan.candidate.requested_spendable + plan.candidate.premium
    second = a.assess_net_refinance(d, 1, **spec["forecasts"])
    assert second["plan"] is not None, second["context"]
    assert second["plan"].switch_cost == second["verdict"].parts["fixed"] == 0
    a.start_net_refinance(d, second["plan"], 1, {}, context=second["context"])
    assert a.refinance_machine.status()["status"] == "complete", a.last_error
    assert a.refis == 2 and a.switch_paid == fixed * RAW
    assert a.external_paid == plan.mortgage_limit + second["plan"].mortgage_limit
    assert d.usdc.functions.balanceOf(a.SINK).call() == a.external_paid + a.switch_paid + a.penalty_paid
    third = a.assess_net_refinance(d, 1, **spec["forecasts"])
    bad = replace(third["plan"], minimum_usdc=third["plan"].export_buck * 2)
    a.start_net_refinance(d, bad, 1, {}, context=third["context"])
    assert a.refinance_machine.status()["phase"] == "export"
    assert a.refinance_machine.settlement_due and not a.refinance_machine.payment_due
    retained = d.buck.functions.totalIssued().call()
    with pytest.raises(ValueError, match="idle canonical"):
        a.assess_net_refinance(d, 1, **spec["forecasts"])
    with pytest.raises(ValueError, match="unsettled export"):
        a.start_net_refinance(d, third["plan"], 1, {})
    assert d.buck.functions.totalIssued().call() == retained
    assert a.refis == 2 and a.switch_paid == fixed * RAW


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
    from buck_core import buck_math
    issue, sale = (next(e for e in a.refinance_machine.events if e.get("operation") == op and e["status"] == "confirmed")
                   for op in ("issue", "export"))
    issue_time, sale_time = (d.w3.eth.get_block(e["block"])["timestamp"] for e in (issue, sale))
    before_sale = int(issue["accounts"]["buck_scaled"])
    assert before_sale >= (plan.export_buck + plan.reserve_floor) * Q
    after_sale = buck_math.net_decay(before_sale, sale_time - issue_time) - plan.export_buck * Q
    assert int(sale["accounts"]["buck_scaled"]) == after_sale
    now = d.w3.eth.get_block("latest")["timestamp"]
    assert d.buck.functions.netBalanceScaled(a.address).call() == buck_math.net_decay(after_sale, now - sale_time)
    assert d.buck.functions.balanceOf(a.address).call() >= plan.candidate.reserve_required


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
    assert d.net_credit_ops.household(a.address, a._token_ids).issued == book.issued + plan.candidate.requested_spendable
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
    driver.timeline_epoch = driver.ts + driver.tick_secs
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


def test_calendar_services_bills_after_buyback_receipt_recovery_with_a_remaining_target(household_world, monkeypatch):
    from buck_core.session import _txhex
    d, scenario, make = household_world
    a = make(75, CreditHouseholdAgent, settings=CALENDAR["settings"])
    a.advance_net_calendar(d, 0, {})
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, 20 * RAW])))
    d.net_credit_ops.issue(a.address, a._token_ids[0], 100 * RAW, 40 * RAW, 10 * RAW, d.pool_acct)
    a._proxy_exec(d, d.usdc.address, d.usdc.encode_abi("approve", args=[d.swap_guard.address, 2**256 - 1]))
    before_buck = d.buck.functions.balanceOf(a.address).call()
    sends, execute = [], d.net_market_ops.execute
    def lost(*args):
        receipt = execute(*args)
        sends.append(receipt)
        raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
    with monkeypatch.context() as patch:
        patch.setattr(d.net_market_ops, "execute", lost)
        with pytest.raises(TransactionReceiptPending):
            a._buy_cash(d, 2 * RAW, RAW, for_repayment=True, repayment_target=25 * RAW)
    assert a._net_trade_uncertain and a._net_pending_buy_hash
    supply, reserve = d.buck.functions.totalIssued().call(), a._net_repayment_reserve
    d.anvil.warp_to(a.calendar_machine.epoch + 30 * 86400)
    a.act(d, scenario, 30, 0, {})  # reconcile earlier buy; do not send new salary yet
    assert not a._net_trade_uncertain and a._net_repayment_target == 25 * RAW
    assert a.income_total == 0 and a.calendar_machine.active
    a.act(d, scenario, 30, 0, {})  # due bills precede retrying a known remaining target
    assert a.income_total == int(CALENDAR["income"]) and a.external_paid == a.payment
    assert not a.calendar_machine.active and a._net_repayment_target == 25 * RAW
    assert a._net_repayment_reserve == reserve and len(sends) == 1
    assert d.buck.functions.totalIssued().call() == supply
    assert d.buck.functions.balanceOf(a.address).call() > before_buck - RAW
    bank = next(e for e in a.refinance_machine.events if e.get("operation") == "mortgage" and e["status"] == "confirmed")
    assert bank["amounts"]["usdc"] == str(a.payment) and int(bank["effective_day"]) == 30


def test_runner_epoch_dates_different_household_arrivals_consistently(household_world):
    d, _, make = household_world
    a = make(76, CreditHouseholdAgent, settings=CALENDAR["settings"])
    b = make(77, CreditHouseholdAgent, settings=CALENDAR["settings"], arrive_day=1)
    d.household_epoch = d.w3.eth.get_block("latest")["timestamp"] + 1234
    with pytest.raises(ValueError, match="chain clock"):
        a.advance_net_calendar(d, 0, {})
    assert not a.calendar_machine.bound and not a.calendar_machine.queue and a.hypo_cash == 0
    for day in (0, 1, 29, 30, 31):
        d.anvil.warp_to(d.household_epoch + day * 86400)
        a.advance_net_calendar(d, day, {})
        d.anvil.warp_to(d.household_epoch + day * 86400 + 7)  # work before the other household's turn
        if day >= b.arrive_day:
            b.advance_net_calendar(d, day, {})
    assert a.calendar_machine.epoch == b.calendar_machine.epoch == d.household_epoch
    for agent, paid_day in ((a, 30), (b, 31)):
        assert agent.income_total == int(CALENDAR["income"])
        assert agent.external_paid == agent.hypo_paid == agent.payment
        assert [at for at, amount in agent.calendar_machine.actual.payments.values()] == [paid_day]
    assert a.external_interest > b.external_interest


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


def _surplus_saver(d, make, index):
    """A mortgage-free saver with owned BUCK, spare dollars and prepared trading."""
    a = make(index, CreditSaverAgent, settings=SURPLUS["settings"] | dict(surplus_policy="basket"))
    for contract, method, args in ((d.usdc, "approve", [d.swap_guard.address, 2**256 - 1]),
                                   (d.buck, "approve", [d.permit2.address, 2**256 - 1]),
                                   (d.permit2, "approve", [d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1])):
        a._proxy_exec(d, contract.address, contract.encode_abi(method, args=args))
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, int(SURPLUS["seed_buck"])])))
    a.advance_net_calendar(d, 0, {})
    return a


def _surplus_day(d, a, day):
    d.anvil.warp_to(a.calendar_machine.epoch + day * 86400)
    d.chain.send(d.kctrl.functions.compute())
    a.calendar_machine.prepare(d, day)


def _sell_dollar_pool(d):
    """The seeding helper really sells a twentieth of the dollar pool's BUCK reserve."""
    q = observe_net_market(d.chain, d.buck, d.usdc, d.pool_ub, d.fee_ub)
    amount = q.reserve_buck // 20
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("approve", args=[d.permit2.address, 2**256 - 1])))
    d.chain.send(d.simlp.functions.exec(d.permit2.address, d.permit2.encode_abi(
        "approve", args=[d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1])))
    d.net_market_ops.sell_exact_input(d.simlp.address, d.pool_ub, amount, 1, d.simlp.address, d.fee_ub)
    return q


def _sell_token_pools(d):
    """...and the same share of every TOKEN pool's: BUCK buys less of the basket."""
    for token, pool in zip(d.tokens, d.pool_buck):
        v3 = d.chain.at("UniswapV3Pool", pool)
        token0 = v3.functions.token0().call()
        first = token0.lower() == d.buck.address.lower()
        amount = active_reserves(d.chain, pool, d.buck, token)[0] // 20
        d.chain.send(d.simlp.functions.swap(pool, d.simlp.address, first, amount,
            4295128740 if first else 1461446703485210103287273052203988822378723970341, token0, v3.functions.token1().call()))


@pytest.mark.parametrize("source", ["cash", "basket"])
def test_household_surplus_saves_in_place_then_retires_actual_discounted_liability(household_world, source):
    d, _, make = household_world
    a = _surplus_saver(d, make, 84)
    ctr, supply = {}, d.buck.functions.totalIssued().call()
    if source == "basket":
        for day in (0, 1):
            _surplus_day(d, a, day)
            before = d.w3.eth.block_number
            quote = a.assess_net_surplus(d, day)
            assert d.w3.eth.block_number == before and quote["plan"].action == "save"
            assert quote["plan"].cash_buffer == a.cash_buffer
            a.advance_net_surplus(d, day, ctr)
            assert a.last_action == "save" and not a.refinance_machine.active, a.last_error
            assert len(a._basket_receipts) == 1 and a._savings_proxy is None
            assert d.buck.functions.balanceOf(a.address).call() >= quote["plan"].reserve_floor
            assert d.usdc.functions.balanceOf(a.address).call() >= a.cash_buffer
            assert d.buck.functions.totalIssued().call() == supply
        rid = a._basket_receipts[0]
        shares, basis = d.basket.functions.holdings(rid).call()
        assert shares == a._basket_shares and basis == a._basket_in == a._receipt_book[rid][0]
        assert sum(e["status"] == "confirmed" and e.get("operation") == "deposit" for e in a.refinance_machine.events) == 2
        assert ctr["bcdInvestments"] == 2
    current = d.net_credit_ops.assess_issue_candidate(a.address, a._token_ids[0], int(SURPLUS["coverage"]),
        int(SURPLUS["spendable"]), int(SURPLUS["premium"]))
    d.net_credit_ops.issue_candidate(a.address, current)
    total_principal = d.net_credit_ops.household(a.address, a._token_ids).issued
    # Real BUCK sales move real pools. Nothing fabricates a price, a mark or a burn.
    q = _sell_dollar_pool(d)
    a.p["surplus_policy"] = "adaptive"
    def decide(day):
        _surplus_day(d, a, day)
        block = d.w3.eth.block_number
        result = a.assess_net_surplus(d, day)
        assert d.w3.eth.block_number == block
        return result
    # The dollar pool alone is now 9% cheaper, but fair value is the basket's
    # BUCK price times that same pool: one dislocated route is not a discount
    # in basket purchasing power, and is left to route arbitrage.
    lone = decide(2)
    spot = observe_net_market(d.chain, d.buck, d.usdc, d.pool_ub, d.fee_ub)
    assert spot.price_num * q.price_den * 100 < 92 * q.price_num * spot.price_den
    assert lone["context"]["choice"]["action"] == "save" and lone["context"]["choice"]["target_principal"] == 0
    assert abs(lone["context"]["choice"]["marginal_ratio_wad"] - 10**18) < 2 * 10**15, lone["context"]
    assert (lone["plan"] is None) == (source == "basket"), "the basket arm has already saved all its spare dollars"
    # The same holder sells the same share of each TOKEN pool: BUCK now buys
    # less of the basket itself, and its TWAP carries that into fair value.
    bvib = d.basket.functions.basketValueInBuck().call()
    _sell_token_pools(d)
    day = 3
    quote = decide(day)
    assert d.basket.functions.basketValueInBuck().call() * 100 > bvib * 108
    assert quote["plan"] is not None and quote["plan"].action == "retire", quote["context"]
    assert quote["context"]["choice"]["marginal_ratio_wad"] < 93 * 10**16
    assert bool(quote["plan"].redeem_shares) == (source == "basket")
    if source == "basket":
        assert quote["plan"].redeem_shares < shares
        assert quote["context"]["exit"]["paid"] >= quote["plan"].minimum_buck
    a.advance_net_surplus(d, day, ctr)
    assert a.last_action != "surplus-unavailable", a.last_error
    for _ in range(5):
        if a._net_repayment_target is None:
            break
        a._recover_pending_operations(d, ctr, day)
    book = d.net_credit_ops.household(a.address, a._token_ids)
    assert book.issued == 0 and a.unwound == total_principal, (a.last_error, book)
    assert d.buck.functions.totalIssued().call() == supply
    assert book.spendable >= quote["plan"].reserve_floor
    assert a.mortgage == a.external_paid == a.switch_paid == 0
    # Public history: a funded target retires nothing; only the selected
    # facility's own authenticated receipts are confirmed reductions.
    m = a.refinance_machine
    funded = [e for e in m.events
              if e.get("kind") == "surplus" and e["status"] == "complete" and e.get("outcome") == "retirement-funded"]
    assert len(funded) == 1 and funded[0]["accounts"]["issued"] == str(total_principal)
    assert funded[0]["retirement"]["target"] == str(quote["plan"].target_principal) and not funded[0]["retirement"]["uncertain"]
    assert int(funded[0]["retirement"]["reserve"]) > quote["plan"].reserve_floor - RAW
    reductions = [e for e in m.events if e.get("kind") == "retirement" and e["status"] == "confirmed"]
    assert sum(int(e["amounts"]["principal"]) for e in reductions) == total_principal
    for e in reductions:
        assert e["seq"] > funded[0]["seq"] and e["phase"] == "reduction" and e["operation"] == "reduce" and not e["recovered"]
        receipt = d.w3.eth.get_transaction_receipt(e["transaction_hash"])
        logs = d.buck.events.FacilityReduced().process_receipt(receipt, errors=DISCARD)
        assert [(x["args"]["tokenId"], x["args"]["principal"], x["args"]["reliefScaled"]) for x in logs] == [
            (int(e["amounts"]["token_id"]), int(e["amounts"]["principal"]), int(e["amounts"]["relief_scaled"]))]
    assert int(reductions[-1]["accounts"]["issued"]) == 0 and int(reductions[0]["accounts"]["issued"]) < total_principal
    assert (m.events[-1]["kind"], m.events[-1]["status"]) == ("retirement", "complete")
    assert m.status()["retirement"] is None and m.status()["kind"] == "surplus" and m.status()["status"] == "complete"
    assert [e["seq"] for e in m.events] == list(range(1, len(m.events) + 1))
    if source == "basket":
        assert len(a._basket_receipts) == 1
        assert 0 < a._basket_in < basis and 0 < a._basket_shares < shares
        assert a._receipt_book[rid] == tuple(reversed(d.basket.functions.holdings(rid).call()))
    assert not d.chain.mismatches


@pytest.mark.parametrize("case", SURPLUS_FLOW["cases"], ids=lambda c: c["name"])
def test_household_surplus_applies_known_outcomes_once_and_stops_on_unknown(household_world, monkeypatch, case):
    from alberta_buck.sim import net_household
    from buck_core import ReductionPlan
    d, scenario, make = household_world
    a = _surplus_saver(d, make, 86)
    m, ctr, retire = a.refinance_machine, {}, case["action"] == "retire"
    day = 0
    if retire:
        # One real saving, real credit and a coherent real sell-off first.
        _surplus_day(d, a, 0)
        a.advance_net_surplus(d, 0, ctr)
        assert a.last_action == "save" and len(a._basket_receipts) == 1, a.last_error
        c = d.net_credit_ops.assess_issue_candidate(a.address, a._token_ids[0], int(SURPLUS["coverage"]),
                                                    int(SURPLUS["spendable"]), int(SURPLUS["premium"]))
        d.net_credit_ops.issue_candidate(a.address, c)
        _sell_dollar_pool(d)
        _sell_token_pools(d)
        a.p["surplus_policy"], day = "adaptive", 1
    _surplus_day(d, a, day)
    rid = a._basket_receipts[0] if retire else None
    books = lambda: (d.usdc.functions.balanceOf(a.address).call(), d.net_credit_ops.household(a.address, a._token_ids).issued,
                     d.buck.functions.totalIssued().call(), tuple(a._basket_receipts), dict(a._receipt_book),
                     d.basket.functions.holdings(rid).call() if rid else None)
    cash, principal, supply, _, _, holding = before = books()
    wallet = d.buck.functions.balanceOf(a.address).call()
    dispatch, sends, fault = d.net_market_ops.execute, [], [True]
    index = SURPLUS_FLOW["refuse_argument"]
    def send(owner, target, method, args):
        phase = m.flow["phase"] if m.flow and m.flow["status"] != "complete" else "repay"
        sends.append((phase, method))
        hit = fault[0] and phase in (case.get("lost"), case.get("unknown"), case.get("refused"))
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
    monkeypatch.setattr(d.net_market_ops, "execute", send)
    sent = lambda phase: [method for p, method in sends if p == phase]
    if case.get("view"):
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
    if case.get("preview"):
        simulate, previews = d.net_simulate, []
        def refused(owner, target, method, args, block_number=None):
            previews.append(method)
            raise RuntimeError("execution reverted: CreditShortfallIncreased")
        monkeypatch.setattr(d, "net_simulate", refused)
        quote = a.assess_net_surplus(d, day)
        assert previews == ["redeemShares"] and quote["context"]["basket_exits"] == [
            dict(token_id=rid, unavailable="execution reverted: CreditShortfallIncreased")]
        assert quote["plan"].action == "retire" and quote["plan"].redeem_shares == 0 and quote["plan"].spend_usdc > 0
        assert quote["context"]["reason"] == "purchase-to-retire"
    if case.get("reduction"):
        plan_reduction, plans = d.net_credit_ops.plan_reduction, []
        def illiquid(owner, token_id, requested, **kwargs):
            plans.append(requested)
            return ReductionPlan(token_id, 0, 0, 0, 0, case["reduction"]) if len(plans) == 1 else plan_reduction(
                owner, token_id, requested, **kwargs)
        monkeypatch.setattr(d.net_credit_ops, "plan_reduction", illiquid)

    a.advance_net_surplus(d, day, ctr)
    phase = case.get("lost") or case.get("unknown") or case.get("refused") or case.get("view")
    deposits = lambda: [e for e in m.events if e["status"] == "confirmed" and e.get("operation") == "deposit"]

    if case.get("unknown"):
        # No hash: the outcome cannot be proved either way, so nothing is
        # booked, nothing is sent again and later income also waits.
        assert m.active and m.flow["status"] == "pending" and m.flow["phase"] == phase and m.flow["hash"] is None
        assert a.last_action == "surplus-pending" and m.status()["kind"] == "surplus"
        count = len(sends)
        for later in (day, day + 1, 30):  # the first payday itself; the calendar observes each one
            d.anvil.warp_to(a.calendar_machine.epoch + later * 86400)
            assert a.advance_net_surplus(d, later, ctr) is True
            assert "surplus hash unavailable" in a.last_error and len(sends) == count
        assert m.flow["phase"] == phase and a.income_total == 0 and a._net_repayment_target is None
        if phase == "buy":
            assert books() == before
        else:
            assert books()[1:] == before[1:] and len(deposits()) == (1 if retire else 0)
        return

    if case.get("lost"):
        # The transaction mined. Its saved hash is authenticated and applied
        # exactly once; recovery sends nothing.
        assert m.active and m.flow["status"] == "pending" and m.flow["phase"] == phase and m.flow["hash"]
        assert a.last_action == "surplus-pending" and len(sent(phase)) == 1
        lost_hash, count = m.flow["hash"], len(sends)
        assert books()[1:5] == before[1:5], "nothing is booked from an unauthenticated reply"
        assert a.advance_net_surplus(d, day, ctr) is True
        applied = [e for e in m.events if e["status"] == "confirmed" and e.get("operation") == phase][-1]
        assert applied["transaction_hash"] == lost_hash and len(sent(phase)) == 1
        assert len(sends) == count + (1 if phase == "buy" else 0), "recovery re-sent nothing; a buy then deposits"
    elif case.get("refused") or case.get("view"):
        refusal = [e for e in m.events if e["status"] == "refused"][-1]
        assert a.last_action == "surplus-refused" and not m.active and m.flow["phase"] == phase
        assert bool(refusal["transaction_hash"]) == bool(case.get("refused")), "a view failure sent nothing"
        if case.get("refused"):
            assert d.w3.eth.get_transaction_receipt(refusal["transaction_hash"])["status"] == 0
        assert m.surplus_due == (phase == "deposit") and a._net_repayment_target is None
        if phase == "deposit":
            bought = [e for e in m.events if e["status"] == "confirmed" and e.get("operation") == "buy"]
            assert len(bought) == 1 and not deposits() and not a._basket_receipts
            acquired = int(bought[0]["amounts"]["net_buck_scaled"]) // Q
            assert d.buck.functions.balanceOf(a.address).call() >= wallet + acquired - 1
            assert a.advance_net_surplus(d, day, ctr) is False, "one attempt a day"
        else:
            assert books() == before, "a proved refusal changed nothing"
        # A known refusal never blocks the next day's work or decision.
        day += 1
        _surplus_day(d, a, day)
        assert a.advance_net_surplus(d, day, ctr) is True
        if phase == "deposit":
            assert [e["status"] for e in m.events if e["status"] in ("resumed", "planned")].count("resumed") == 1
            assert len(sent("buy")) == 1, "the purchased cash is deposited; nothing is bought again"

    for _ in range(6):
        if m.active or a._net_repayment_target is not None or a._net_repayment_interrupted:
            a._recover_pending_operations(d, ctr, day)
    assert not m.active and m.flow["status"] == "complete" and m.status()["retirement"] is None, a.last_error
    assert d.buck.functions.totalIssued().call() == supply - (principal if retire else 0)
    assert [e["seq"] for e in m.events] == list(range(1, len(m.events) + 1))
    hashes = [e["transaction_hash"] for e in m.events if e["status"] == "confirmed"]
    assert len(set(hashes)) == len(hashes), "no receipt is applied twice"
    if not retire:
        assert a.last_action == "save" and len(a._basket_receipts) == 1 and len(deposits()) == 1
        assert len(sent("buy")) == (2 if case.get("refused") == "buy" else 1)
        assert len(sent("deposit")) == (2 if case.get("refused") == "deposit" else 1)
        shares, basis = d.basket.functions.holdings(a._basket_receipts[0]).call()
        assert (basis, shares) == a._receipt_book[a._basket_receipts[0]]
        assert basis == a._basket_in == int(deposits()[0]["amounts"]["buck"])
        assert d.usdc.functions.balanceOf(a.address).call() >= a.cash_buffer
        assert d.net_credit_ops.household(a.address, a._token_ids).issued == principal == 0
        return
    reductions = [e for e in m.events if e.get("kind") == "retirement" and e["status"] == "confirmed"]
    assert sum(int(e["amounts"]["principal"]) for e in reductions) == principal == a.unwound
    assert d.net_credit_ops.household(a.address, a._token_ids).issued == 0
    assert d.buck.functions.balanceOf(a.address).call() >= wallet - RAW, "every prior wallet BUCK survives"
    redeemed = [e for e in m.events if e["status"] == "confirmed" and e.get("operation") == "redeem"]
    if case.get("preview"):
        assert not redeemed and not sent("redeem") and d.basket.functions.holdings(rid).call() == holding
        assert d.usdc.functions.balanceOf(a.address).call() < cash, "the retirement was bought, never an unguarded exit"
    else:
        assert len(redeemed) == 1 and len(sent("redeem")) == (2 if case.get("refused") else 1)
        assert d.basket.functions.holdings(rid).call()[0] == holding[0] - int(redeemed[0]["amounts"]["shares_retired"])
        assert d.usdc.functions.balanceOf(a.address).call() == cash
    if case.get("reduction"):
        waits = [e for e in m.events if e.get("kind") == "retirement" and e["status"] == "waiting"]
        assert len(waits) == 1 and case["reduction"] in waits[0]["reason"]
        # The exit's cash was real and stayed owned; the target and its
        # protected reserve waited. Nothing was retired by the refusal.
        assert waits[0]["accounts"]["issued"] == str(principal) and waits[0]["seq"] < reductions[0]["seq"]
        assert waits[0]["retirement"]["target"] == str(principal) and int(waits[0]["retirement"]["reserve"]) > wallet - RAW
    assert not d.chain.mismatches


def _wallet_saver(d, make, index, **settings):
    """A mortgage-free saver with wages, bills and prepared trading; no wallet account yet."""
    a = make(index, CreditSaverAgent, settings=WALLET["lifecycle"]["settings"] | settings)
    for contract, method, args in ((d.usdc, "approve", [d.swap_guard.address, 2**256 - 1]),
                                   (d.buck, "approve", [d.permit2.address, 2**256 - 1]),
                                   (d.permit2, "approve", [d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1])):
        a._proxy_exec(d, contract.address, contract.encode_abi(method, args=args))
    a.advance_net_calendar(d, 0, {})
    return a


def _wallet_day(d, a, day):
    d.anvil.warp_to(a.calendar_machine.epoch + day * 86400)
    d.chain.send(d.kctrl.functions.compute())


def test_household_wallet_keeps_a_buck_working_balance_in_its_own_custody(household_world):
    d, _, make = household_world
    a = _wallet_saver(d, make, 87)
    m, ctr = a.refinance_machine, {}
    main = lambda: (d.usdc.functions.balanceOf(a.address).call(), d.buck.functions.netBalanceScaled(a.address).call())
    wallet = lambda: d.buck.functions.balanceOf(a._wallet.address).call()
    supply = d.buck.functions.totalIssued().call()
    assert a.advance_net_wallet(d, 0, ctr) is False and a._wallet is None, "nothing is tended before a payday"
    assert m.status()["next_wallet_day"] == 30
    spending = a.income_annual * a.p["living_frac"] / RAW

    # Paydays are each household's own day of the month. A neighbour who
    # arrived the same day but is paid on the 7th is paid, and tends its
    # wallet, on day 7; this household waits for its own day.
    early = _wallet_saver(d, make, 89, pay_day=7)
    assert (a.p["pay_day"], early.p["pay_day"]) == (0, 7)
    assert early.refinance_machine.status()["next_wallet_day"] == 7 == early.calendar_machine.next_pay
    _wallet_day(d, a, 7)
    assert early.advance_net_wallet(d, 7, {}) is True and early.last_action == "wallet-buy", early.last_error
    assert early.income_total == early.income_annual // 12 and early.wallet_bought > 0
    assert (early.calendar_machine.next_pay, early.refinance_machine.status()["next_wallet_day"]) == (37, 37)
    assert a.advance_net_wallet(d, 7, ctr) is False and a.income_total == 0 and a._wallet is None
    assert a.calendar_machine.next_pay == 30

    # First payday: wages and bills settle, then dollars above the buffer buy
    # the working balance. It is delivered to the wallet's own account.
    _wallet_day(d, a, 30)
    assert a.advance_net_wallet(d, 30, ctr) is True and a.last_action == "wallet-buy", a.last_error
    assert a.income_total == a.income_annual // 12 and a.living_paid > 0, "the payday came first"
    plan = m.flow["plan"]
    def wanted(day):
        # The assessment's own recorded cost/fair ratio sizes the target.
        context = [e for e in m.events if e.get("kind") == "wallet" and e["status"] == "planned"][-1]["context"]
        return policy.net_wallet_target(int(context["marginal_ratio_wad"]), day, spending, a.p)
    target = wanted(30)
    assert target >= int(policy.money_target(30, spending, 0, a.p) * RAW) > 0
    assert plan.action == "buy" and plan.target == target and plan.held == 0 and plan.cash_buffer == a.cash_buffer
    assert d.reg.functions.isVerified(a._wallet.address).call() and not d.reg.functions.isCarrying(a._wallet.address).call()
    assert a._wallet.address != a.address and plan.wallet == a._wallet.address
    bought = [e for e in m.events if e.get("kind") == "wallet" and e["status"] == "confirmed"]
    assert [e["operation"] for e in bought] == ["buy"] and m.events[-1]["outcome"] == "wallet-buy"
    received = int(bought[0]["amounts"]["net_buck_scaled"]) // Q
    assert plan.minimum_net <= received == wallet() <= target + RAW and a.wallet_bought == received
    assert received * 100 >= target * 99, "the shortfall was bought, less only impact and fees"
    planned = [e for e in m.events if e.get("kind") == "wallet" and e["status"] == "planned"][0]
    cash_before = int(planned["accounts"]["usdc"])
    assert cash_before - main()[0] == int(bought[0]["amounts"]["usdc"]) <= plan.spend_usdc
    assert main()[0] >= a.cash_buffer and main()[1] == int(planned["accounts"]["buck_scaled"]), "the main BUCK is untouched"
    assert bought[0]["accounts"]["custody"]["wallet"]["buck_scaled"] == bought[0]["amounts"]["net_buck_scaled"]
    assert d.buck.functions.totalIssued().call() == supply and d.net_credit_ops.household(a.address, a._token_ids).issued == 0
    assert m.status()["kind"] == "wallet" and m.status()["next_wallet_day"] == 60
    block = d.w3.eth.block_number
    assert a.advance_net_wallet(d, 30, ctr) is False and d.w3.eth.block_number == block, "once a pay period"

    # Second payday: adoption has ramped and the balance has decayed a
    # month. Only that shortfall is bought, into the same account.
    _wallet_day(d, a, 60)
    held = wallet()
    assert held < received, "wallet money carries demurrage like any other"
    address = a._wallet.address
    assert a.advance_net_wallet(d, 60, ctr) is True and a.last_action == "wallet-buy", a.last_error
    plan = m.flow["plan"]
    assert plan.target == wanted(60) and plan.held == held
    assert policy.money_target(60, spending, 0, a.p) > policy.money_target(30, spending, 0, a.p), "adoption ramps"
    assert a._wallet.address == address and 0 < wallet() - held <= plan.target - held + RAW
    assert plan.spend_usdc * 5 < cash_before - int(bought[0]["accounts"]["usdc"]), "a top-up, not the balance again"

    # An excess (here a real transfer in) is sold from the wallet's own
    # custody, one impact-capped bite, for dollars delivered home.
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi(
        "transfer", args=[address, int(WALLET["lifecycle"]["gift_buck"])])))
    _wallet_day(d, a, 90)
    held, sold_before = wallet(), a.wallet_sold
    a.calendar_machine.prepare(d, 90)
    cash, home = main()
    q = observe_net_market(d.chain, d.buck, d.usdc, d.pool_ub, d.fee_ub)
    assert a.advance_net_wallet(d, 90, ctr) is True and a.last_action == "wallet-sell", a.last_error
    plan = m.flow["plan"]
    assert plan.action == "sell" and plan.sell_buck == min(held - plan.target, q.reserve_buck * 5000 // 10**6)
    sold = [e for e in m.events if e.get("kind") == "wallet" and e["status"] == "confirmed" and e["operation"] == "sell"]
    assert len(sold) == 1 and int(sold[0]["amounts"]["buck"]) == plan.sell_buck == a.wallet_sold - sold_before
    assert held - wallet() >= plan.sell_buck and held - wallet() <= plan.sell_buck + RAW
    usdc = int(sold[0]["amounts"]["usdc"])
    assert usdc >= plan.minimum_usdc and int(sold[0]["accounts"]["usdc"]) - int(m.events[-3]["accounts"]["usdc"]) >= 0
    assert int(sold[0]["accounts"]["buck_scaled"]) <= home, "selling the wallet's BUCK never touches the main account's"
    assert int(sold[0]["accounts"]["usdc"]) >= cash + usdc - 1, "the dollars came home (wages and bills aside)"
    assert d.buck.functions.totalIssued().call() == supply
    assert [e["seq"] for e in m.events] == list(range(1, len(m.events) + 1)) and not d.chain.mismatches


@pytest.mark.parametrize("case", WALLET["flow"]["cases"], ids=lambda c: c["name"])
def test_household_wallet_applies_known_outcomes_once_and_stops_on_unknown(household_world, monkeypatch, case):
    d, _, make = household_world
    a = _wallet_saver(d, make, 88)
    m, ctr = a.refinance_machine, {}
    _wallet_day(d, a, 30)
    day = 30
    if case["action"] == "sell":
        # A real working balance first, then a real excess to trim next payday.
        assert a.advance_net_wallet(d, 30, ctr) is True and a.last_action == "wallet-buy", a.last_error
        d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi(
            "transfer", args=[a._wallet.address, int(WALLET["lifecycle"]["gift_buck"])])))
        _wallet_day(d, a, 60)
        day = 60
    a.calendar_machine.prepare(d, day)
    held = lambda: d.buck.functions.balanceOf(a._wallet.address).call() if a._wallet is not None else 0
    books = lambda: (held(), a.wallet_bought, a.wallet_sold, d.buck.functions.totalIssued().call())
    before = books()
    dispatch, sends, fault = d.net_market_ops.execute, [], [True]
    index = WALLET["flow"]["refuse_argument"]
    def send(owner, target, method, args):
        phase = m.flow["phase"] if m.flow and m.flow.get("kind") == "wallet" and m.flow["status"] != "complete" else "other"
        sends.append((phase, method, owner))
        hit = fault[0] and phase in (case.get("lost"), case.get("unknown"), case.get("refused"))
        if hit and case.get("unknown"):
            fault[0] = False
            raise RuntimeError("submission outcome unknown")
        if hit and case.get("refused"):
            fault[0] = False
            args = list(args)
            args[index[method]] = 2**200
        receipt = dispatch(owner, target, method, args)
        if hit and case.get("lost"):
            fault[0] = False
            raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
        return receipt
    monkeypatch.setattr(d.net_market_ops, "execute", send)
    trades = lambda: [x for x in sends if x[0] in ("buy", "sell")]
    assert a.advance_net_wallet(d, day, ctr) is True
    phase = case.get("lost") or case.get("unknown") or case.get("refused")
    assert m.flow["kind"] == "wallet" and m.flow["phase"] == phase

    if case.get("unknown"):
        assert m.active and m.flow["status"] == "pending" and m.flow["hash"] is None and a.last_action == "wallet-pending"
        for later in (day, day + 1):
            d.anvil.warp_to(a.calendar_machine.epoch + later * 86400)
            assert a.advance_net_wallet(d, later, ctr) is True and "wallet hash unavailable" in a.last_error
        assert len(trades()) == 1 and books() == (held(), *before[1:]) and held() <= before[0]
        return
    if case.get("lost"):
        assert m.active and m.flow["status"] == "pending" and m.flow["hash"] and a.last_action == "wallet-pending"
        assert (a.wallet_bought, a.wallet_sold) == before[1:3], "nothing is booked from an unauthenticated reply"
        lost_hash = m.flow["hash"]
        assert a.advance_net_wallet(d, day, ctr) is True
        applied = [e for e in m.events if e.get("kind") == "wallet" and e["status"] == "confirmed"][-1]
        assert applied["transaction_hash"] == lost_hash and len(trades()) == 1
        assert m.flow["status"] == "complete" and a.last_action == "wallet-buy"
        assert a.wallet_bought == held() == int(applied["amounts"]["net_buck_scaled"]) // Q > 0
        return
    refusal = [e for e in m.events if e["status"] == "refused"][-1]
    assert a.last_action == "wallet-refused" and not m.active and not m.surplus_due and not m.settlement_due
    assert d.w3.eth.get_transaction_receipt(refusal["transaction_hash"])["status"] == 0
    assert books() == before and a._wallet is not None, "a proved refusal moved no money; the empty account stays"
    assert a.advance_net_wallet(d, day, ctr) is False and m.status()["next_wallet_day"] == day + 30
    # A known refusal blocks nothing: the next payday tends the balance afresh.
    _wallet_day(d, a, day + 30)
    assert a.advance_net_wallet(d, day + 30, ctr) is True and a.last_action == "wallet-buy", a.last_error
    assert len(trades()) == 2 and a.wallet_bought == held() > 0
    assert a.income_total == 2 * (a.income_annual // 12)


def test_refinance_funding_target_is_saved_capital_not_an_unfunded_loan(household_world):
    d, _, make = household_world
    spec = REFINANCE["programme"]
    a = make(83, CreditHouseholdAgent, settings=spec["settings"] | dict(surplus_policy="basket"))
    d.chain.send(d.usdc.functions.mint(a.address, a.cash_buffer))
    block, supply = d.w3.eth.block_number, d.buck.functions.totalIssued().call()
    result = a.assess_net_refinance(d, 1, **spec["forecasts"])
    hold = result["context"]["funding"]
    assert result["plan"] is None and hold["unfunded"] and not hold["refinance_ready"]
    assert hold["cash_floor"] > a.cash_buffer and hold["basis"] == "worthwhile-programme"
    assert d.w3.eth.block_number == block and d.buck.functions.totalIssued().call() == supply
    d.chain.send(d.usdc.functions.mint(a.address, hold["cash_floor"] - a.cash_buffer + RAW))
    funded = a.assess_net_refinance(d, 1, **spec["forecasts"])
    assert funded["plan"] is not None and funded["context"]["funding"]["refinance_ready"]
    assert not funded["context"]["funding"]["unfunded"]
    assert d.buck.functions.totalIssued().call() == supply and not a.refinance_machine.events


def test_saving_between_refinance_tranches_keeps_the_credit_front_and_frees_other_cash(household_world):
    d, _, make = household_world
    spec = REFINANCE["programme"]
    a = make(85, CreditHouseholdAgent, settings=spec["settings"] | dict(surplus_policy="basket"), member=spec["members"][0])
    for contract, method, args in ((d.usdc, "approve", [d.swap_guard.address, 2**256 - 1]),
                                   (d.buck, "approve", [d.permit2.address, 2**256 - 1]),
                                   (d.permit2, "approve", [d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1])):
        a._proxy_exec(d, contract.address, contract.encode_abi(method, args=args))
    ctr, m = {}, a.refinance_machine
    def at(day):
        d.anvil.warp_to(a.calendar_machine.epoch + day * 86400)
        d.chain.send(d.kctrl.functions.compute())
    a.advance_net_refinancing(d, 0, ctr)
    assert a._net_credit_reserve == 0 and a.refis == 0
    at(1)
    a.advance_net_refinancing(d, 1, ctr, forecasts=spec["forecasts"])
    front = m.flow["plan"].reserve_floor
    assert a.refis == 1 and m.flow["status"] == "complete" and a._net_credit_reserve == front > 0, a.last_error
    assert m.events[-1]["accounts"]["reserve_floor"] == str(front) and m.events[-1]["accounts"]["surplus_protection"] is None
    # Unrelated owned BUCK arrives: cash above the issuance front, and dollars to save.
    d.chain.send(d.simlp.functions.exec(d.buck.address, d.buck.encode_abi("transfer", args=[a.address, 500 * RAW])))
    d.chain.send(d.usdc.functions.mint(a.address, 20_000 * RAW))
    at(2)
    a.calendar_machine.prepare(d, 2)
    saving = a.assess_net_surplus(d, 2, forecasts=spec["forecasts"])
    held = d.buck.functions.balanceOf(a.address).call()
    assert saving["plan"] is not None and saving["plan"].action == "save", saving["context"]
    assert saving["plan"].reserve_floor == held > front + 400 * RAW
    assert saving["plan"].cash_buffer == saving["context"]["credit_funding"]["cash_floor"] >= a.cash_buffer
    cash, mortgage = d.usdc.functions.balanceOf(a.address).call(), a.mortgage
    issued = d.net_credit_ops.household(a.address, a._token_ids).issued
    a.advance_net_surplus(d, 2, ctr, forecasts=spec["forecasts"])
    assert a.last_action == "save" and m.flow["kind"] == "surplus" and m.flow["status"] == "complete", a.last_error
    assert len(a._basket_receipts) == 1 and a.mortgage == mortgage
    assert d.net_credit_ops.household(a.address, a._token_ids).issued == issued, "saving neither issues nor retires principal"
    assert d.usdc.functions.balanceOf(a.address).call() >= saving["plan"].cash_buffer, "the next dollar front survives"
    assert cash - d.usdc.functions.balanceOf(a.address).call() == saving["plan"].spend_usdc
    after = d.buck.functions.balanceOf(a.address).call()
    assert after >= saving["plan"].reserve_floor - 1, "one operation protected every prior wallet BUCK"
    # ...but that protection ended with the operation. The lasting front is
    # unchanged, and the other owned BUCK can still be converted to the bank.
    assert a._net_credit_reserve == front and m.flow["plan"].reserve_floor > front
    confirmed = [e for e in m.events if e.get("kind") == "surplus" and e["status"] == "confirmed"]
    assert [e["operation"] for e in confirmed] == ["buy", "deposit"]
    protection = {"usdc": str(saving["plan"].cash_buffer), "buck": str(held)}
    assert all(e["accounts"]["reserve_floor"] == str(front) and e["accounts"]["surplus_protection"] == protection
               for e in confirmed)
    # With no further credit to draw, only owned cash can go to the bank.
    a.p["utilization"] = spec["insufficient_utilization"]
    block = d.w3.eth.block_number
    quoted = a.assess_net_refinance(d, 2, **spec["forecasts"])
    frozen = a.assess_net_refinance(d, 2, **spec["forecasts"], reserve_floor=m.flow["plan"].reserve_floor)
    assert d.w3.eth.block_number == block and quoted["context"]["protected_buck"] == front
    # The old cursor default froze all but the save's own sizing cushion.
    assert frozen["context"]["protected_buck"] == held and (frozen["plan"] is None or frozen["plan"].export_buck < 10 * RAW)
    plan = quoted["plan"]
    assert plan is not None and plan.candidate is None and plan.reserve_floor == front, quoted["context"]
    assert 400 * RAW < plan.export_buck <= after - front and plan.switch_cost == plan.reserve_buy_net == 0
    a.advance_net_refinancing(d, 2, ctr, forecasts=spec["forecasts"])
    assert a.refis == 2 and m.flow.get("kind") != "surplus" and m.flow["status"] == "complete", a.last_error
    assert a.mortgage < mortgage and a._net_credit_reserve == front
    assert front <= d.buck.functions.balanceOf(a.address).call() <= after - plan.export_buck
    assert d.net_credit_ops.household(a.address, a._token_ids).issued == issued, "a wallet prepayment issues nothing"
    assert len(a._basket_receipts) == 1 and not d.chain.mismatches


def test_assessed_refinance_leaves_owned_cash_for_time_between_issue_and_export(household_world, monkeypatch):
    d, _, make = household_world
    spec = REFINANCE["programme"]
    a = make(82, CreditHouseholdAgent, settings=spec["settings"], member=spec["members"][0])
    for contract, method, args in ((d.usdc, "approve", [d.swap_guard.address, 2**256 - 1]),
                                   (d.buck, "approve", [d.permit2.address, 2**256 - 1]),
                                   (d.permit2, "approve", [d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1])):
        a._proxy_exec(d, contract.address, contract.encode_abi(method, args=args))
    a.advance_net_refinancing(d, 0, {})
    d.anvil.warp_to(a.calendar_machine.epoch + 86400)
    d.chain.send(d.kctrl.functions.compute())
    issue, observations = d.net_credit_ops.issue_candidate, []
    def delayed(*args, **kwargs):
        result = issue(*args, **kwargs)
        before = d.buck.functions.netBalanceScaled(a.address).call()
        d.anvil.warp_to(d.w3.eth.get_block("latest")["timestamp"] + 60)
        after = d.buck.functions.netBalanceScaled(a.address).call()
        observations.append((before, after))
        return result
    with monkeypatch.context() as patch:
        patch.setattr(d.net_credit_ops, "issue_candidate", delayed)
        a.advance_net_refinancing(d, 1, {}, forecasts=spec["forecasts"])
    assert observations and observations[0][1] < observations[0][0]
    plan = a.refinance_machine.flow["plan"]
    held = d.buck.functions.balanceOf(a.address).call()
    assert a.refis == 1, dict(error=a.last_error, held=held, exported=plan.export_buck, reserve=plan.reserve_floor)
    assert held >= plan.reserve_floor
    assert d.net_credit_ops.household(a.address, a._token_ids).issued == plan.candidate.requested_spendable + plan.candidate.premium


def test_automatic_debtor_prioritizes_calendar_and_remainders_before_fresh_credit(household_world, monkeypatch):
    d, scenario, make = household_world
    spec = REFINANCE["programme"]
    a, b = [make(80 + i, CreditHouseholdAgent, settings=spec["settings"], member=member)
            for i, member in enumerate(spec["members"])]
    assert [x.p["phase"] for x in (a, b)] == spec["adoption_phases"]
    for x in (a, b):
        x._proxy_exec(d, d.usdc.address, d.usdc.encode_abi("approve", args=[d.swap_guard.address, 2**256 - 1]))
        x._proxy_exec(d, d.buck.address, d.buck.encode_abi("approve", args=[d.permit2.address, 2**256 - 1]))
        x._proxy_exec(d, d.permit2.address, d.permit2.encode_abi(
            "approve", args=[d.buck.address, d.router.address, 2**160 - 1, 2**48 - 1]))
    d.household_epoch = d.w3.eth.get_block("latest")["timestamp"]
    ctr = {}
    def at(day):
        d.anvil.warp_to(d.household_epoch + day * 86400)
        d.chain.send(d.kctrl.functions.compute())
    def step(x, day, *, explicit=False):
        return x.advance_net_refinancing(d, day, ctr, forecasts=spec["forecasts"] if explicit else None)
    def principal(x):
        return d.net_credit_ops.household(x.address, x._token_ids).issued

    for x in (a, b):
        assert not step(x, 0)
        assert x._started and d.usdc.functions.balanceOf(x.address).call() == int(spec["seed_usdc"])
        assert principal(x) == 0 and x._net_refinance_day is None
    at(1)
    a.calendar_machine.prepare(d, 1)
    before = (d.w3.eth.block_number, a.mortgage, a.hypo_mortgage)
    a.p["utilization"] = spec["insufficient_utilization"]
    assert a.assess_net_refinance(d, 1, **spec["forecasts"])["plan"] is None
    a.p["utilization"] = spec["settings"]["utilization"]
    assessed = a.assess_net_refinance(d, 1, **spec["forecasts"])
    assert before == (d.w3.eth.block_number, a.mortgage, a.hypo_mortgage)
    assert assessed["context"]["reason"] == "worthwhile-staged-refinance"
    assert assessed["context"]["programme"]["tranches"] > 1
    fixed = int(spec["settings"]["switch_k"] * 1000 * RAW)
    assert assessed["plan"].switch_cost == fixed
    allocated = assessed["context"]["programme"]["fixed_cost_allocated"]
    assert 0 < allocated < fixed / RAW
    assert assessed["verdict"].value - fixed / RAW + allocated < 0
    assert step(a, 1, explicit=True)
    assert a.refis == 1 and a.refinance_closed and a.switch_paid == fixed, a.last_error
    assert a._next_decision == 2 and not step(b, 1)
    stamp, issued = d.w3.eth.block_number, principal(a)
    assert not step(a, 1) and d.w3.eth.block_number == stamp and principal(a) == issued

    at(2)
    pay = d.net_settlement_ops.pay_exact
    def lost(*args):
        result = pay(*args)
        raise TransactionReceiptPending(_txhex(result.receipt["transactionHash"]))
    with monkeypatch.context() as patch:
        patch.setattr(d.net_settlement_ops, "pay_exact", lost)
        assert step(a, 2)
    m = a.refinance_machine
    assert m.active and m.flow["phase"] == "mortgage" and m.flow["status"] == "pending"
    issued, paid, bank = principal(a), a.external_paid, d.usdc.functions.balanceOf(a.SINK).call()
    at(3)
    assert step(a, 3)
    assert not m.active and a.refis == 2 and principal(a) == issued
    assert a.external_paid > paid and d.usdc.functions.balanceOf(a.SINK).call() == bank
    assert a.switch_paid == fixed

    at(4)
    start = a.start_net_refinance
    def refuse(world, plan, *args, **kwargs):
        return start(world, replace(plan, minimum_usdc=plan.export_buck * 2), *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(a, "start_net_refinance", refuse)
        assert step(a, 4)
    assert m.settlement_due and m.flow["phase"] == "export" and a.refis == 2
    issued = principal(a)
    at(5)
    with monkeypatch.context() as patch:
        patch.setattr(a, "assess_net_refinance", lambda *a, **k: pytest.fail("unsettled export cannot request new credit"))
        assert step(a, 5)
    assert not m.settlement_due and a.refis == 3 and principal(a) == issued, a.last_error

    at(6)
    issued, paid = principal(a), a.external_paid
    with monkeypatch.context() as patch:
        def unavailable():
            raise RuntimeError("fund mark unavailable")
        patch.setattr(d.net_credit_ops, "quote_global_relief_rate", unavailable)
        assert step(a, 6)
        assert a.last_action == "refinance-unavailable"
        assert not step(a, 6)
    assert principal(a) == issued and a.external_paid == paid
    assert m.events[-1]["status"] == "unavailable" and a._next_decision == 7
    at(7)
    assert step(a, 7) and a.refis == 4, a.last_error

    at(9)
    b.p["utilization"] = spec["insufficient_utilization"]
    assert step(b, 9, explicit=True) and b.last_action == "declined" and not b.joined
    assert b._next_decision == 21 and b.refinance_machine.events[-1]["status"] == "declined"
    at(10)
    assert not step(b, 10) and principal(b) == 0
    at(21)
    b.p["utilization"] = spec["settings"]["utilization"]
    assert step(b, 21, explicit=True) and b.refis == 1 and b._next_decision == 22, b.last_error
    at(22)
    b._divest_day = 22
    owed = principal(b)
    assert not step(b, 22) and principal(b) == owed, "a leaving household's work belongs to its exit dispatcher"
    b._divest_day = None

    at(30)
    issued = principal(a)
    mint = a.calendar_machine._mint
    def lost_income(*args):
        receipt = mint(*args)
        raise TransactionReceiptPending(_txhex(receipt["transactionHash"]))
    with monkeypatch.context() as patch:
        patch.setattr(a.calendar_machine, "_mint", lost_income)
        assert step(a, 30)
    assert a.calendar_machine.active and a.income_total == 0 and principal(a) == issued
    assert step(a, 30)  # recovered wage/bills consume the call before any fresh loan
    assert not a.calendar_machine.active and a.income_total == a.income_annual // 12
    assert principal(a) == issued and a.refis == 4
    at(31)
    with monkeypatch.context() as patch:
        def unknown(*args):
            pay(*args)
            raise TransactionReceiptPending(None)
        patch.setattr(d.net_settlement_ops, "pay_exact", unknown)
        assert step(a, 31)
    assert m.active and m.flow["status"] == "pending" and m.flow["hash"] is None
    issued, wages, bank = principal(a), a.income_total, d.usdc.functions.balanceOf(a.SINK).call()
    at(60)
    assert step(a, 60)
    assert a.income_total == wages and principal(a) == issued and d.usdc.functions.balanceOf(a.SINK).call() == bank
    assert a.calendar_machine.income_due == a.income_annual // 12
    assert a.hypo_paid > 0 and a.hypo_interest > 0
    assert [e["day"] for e in m.events if e["status"] == "planned" and e.get("kind") != "calendar"] == [1, 2, 4, 7, 31]
    assert not a._net_dispatching and not d.chain.mismatches
