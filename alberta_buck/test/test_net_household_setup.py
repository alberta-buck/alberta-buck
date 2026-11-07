"""Existing household setup, credit/portfolio books and the remaining decision gate."""
from dataclasses import replace
from fractions import Fraction
import random
from types import SimpleNamespace

import pytest
from web3.logs import DISCARD
from buck_core import TransactionReceiptPending, TransactionReverted

from alberta_buck.sim import experiment as expmod, identity as idmod
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.credit_households import CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent
from alberta_buck.sim.deploy import deploy
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.scenario import ROUTING

RAW = 10**6
Q = 10**36
YEAR = 365 * 86400 + 6 * 3600


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
        def make(i, cls):
            agent = cls(i)
            agent.settings = dict(home_k=1000, premium_bp=100)
            agent.setup(d, scenario, rng)
            return agent
        yield d, scenario, make


def test_primary_household_setup_and_per_facility_credit_marks(household_world):
    d, scenario, make = household_world
    chain, evm = d.chain, d.anvil
    agents = []
    for i, cls in enumerate((CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent)):
        a = make(i, cls)
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
    sale = d.net_market_ops.sell_exact_input(owner, d.pool_ub, 100 * RAW, 90 * RAW, owner, d.fee_ub)
    assert sale.nominal_paid == 100 * RAW
    assert d.usdc.functions.balanceOf(owner).call() - cash == sale.received >= 90 * RAW
    assert held - d.buck.functions.balanceOf(owner).call() == sale.paid
    assert d.net_credit_ops.household(owner, a._token_ids).issued == issued.principal
    assert d.buck.functions.totalIssued().call() == supply
    assert (a.mortgage, a.external_paid) == (initial_mortgage, initial_paid), "export is not bank payment"

    receiver = a._new_proxy(d)
    purchase = d.net_market_ops.buy_exact_input(owner, d.pool_ub, 10 * RAW, 9 * RAW, receiver.address)
    assert purchase.nominal_received > purchase.received >= 9 * RAW
    assert d.buck.functions.netBalanceScaled(receiver.address).call() == purchase.net_scaled
    assert d.buck.functions.balanceOf(d.router.address).call() == 0
    assert d.buck.functions.balanceOf(d.swap_guard.address).call() == 0
    assert d.usdc.functions.balanceOf(d.swap_guard.address).call() == 0
    assert not d.reg.functions.isVerified(d.permit2.address).call()
    assert not d.reg.functions.isVerified(d.swap_guard.address).call()

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
