"""A real V3/Anvil cash-only guard fixture; not a household economy."""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from web3 import Web3

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.credit_households import CreditHouseholdAgent
from alberta_buck.sim.gauge import active_reserves
from alberta_buck.sim.router import full_range_ticks, sqrt_price_x96
from buck_core import (Expect, NetCreditOps, NetMarketOps, ReductionPlan, SettlementOps, TransactionReceiptPending,
                       TransactionReverted, deploy_net_stack, load_artifact)
from buck_core.session import _txhex

RAW = 10**6
ZERO = "0x" + "00" * 20
YEAR = 365 * 24 * 60 * 60 + 6 * 60 * 60
ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("BuckNet", "BuckNetMaturity", "BuckNetTransfers", "BuckNetQuoter", "BuckNetSwapGuard", "JubileeFund",
            "IdentityRegistryHarness", "BuckCredit", "BuckKControllerDirect", "UniswapV3Factory", "UniswapV3Pool",
            "BuckBasketEquity", "MockERC20", "SimLP")


def _exec(chain, actor, contract, method, args):
    encoded = contract.encode_abi(method, args=args)
    return chain.send(actor.functions.exec(contract.address, bytes.fromhex(encoded[2:])))


@pytest.mark.skipif(
    not shutil.which("anvil") or not all((ROOT / "out" / f"{name}.sol" / f"{name}.json").is_file()
                                         for name in REQUIRED),
    reason="requires anvil and built net/simulation artifacts; run make nix-sim-build",
)
def test_python_guard_uses_actual_net_value_from_aged_v3_pool():
    with Anvil(timestamp=1_900_000_000) as anvil:
        chain = Chain(anvil.w3, anvil.w3.eth.accounts[0])
        gov = chain.deployer
        registry = chain.deploy("IdentityRegistryHarness", gov)
        credit = chain.deploy("BuckCredit")
        controller = chain.deploy("BuckKControllerDirect", 100_000_000_000, 643_004, 0,
                                  1800, 0, 950_000_000_000_000_000, 750_000_000_000_000_000, gov)
        stack = deploy_net_stack(chain, registry, credit, controller, gov)
        buck, fund = stack.token, stack.fund
        factory = chain.deploy("UniswapV3Factory")
        basket = chain.deploy("BuckBasketEquity", buck.address, controller.address, factory.address,
                              gov, 3000, 600, 64, 500, 1000)
        usdc = chain.deploy("MockERC20", "USD Coin", "USDC", 6)
        provider, buyer = chain.deploy("SimLP"), chain.deploy("SimLP")
        g, e = (1, 2), ((1, 2), (1, 2))
        for target, carrying in ((fund.address, True), (basket.address, False),
                                 (provider.address, False), (buyer.address, False)):
            chain.send(registry.functions.bindContract(target, g, e, True, carrying))
        chain.send(basket.functions.enableNetAccounting())
        chain.send(fund.functions.configure(buck.address, basket.address))
        _exec(chain, provider, credit, "setCreditIssuer", [gov, True])
        chain.send(credit.functions.createCredit(provider.address, 0, 2_000 * RAW, 2_000 * RAW, 0, 0, 0, 0))
        tid = credit.functions.tokenOfOwnerByIndex(provider.address, 0).call()
        actors = {provider.address.lower(): provider, buyer.address.lower(): buyer}
        credit_ops = NetCreditOps(chain, stack, lambda owner, target, method, args:
                                  _exec(chain, actors[owner.lower()], target, method, args))
        credit_ops.issue(provider.address, tid, 2_000 * RAW, 1_200 * RAW, 0, ZERO)

        chain.send(factory.functions.createPool(buck.address, usdc.address, 500))
        pool_address = factory.functions.getPool(buck.address, usdc.address, 500).call()
        pool = chain.at("UniswapV3Pool", pool_address)
        chain.send(registry.functions.bindContract(pool_address, g, e, True, True))
        chain.send(pool.functions.initialize(sqrt_price_x96(buck.address, RAW, usdc.address, RAW)))
        chain.send(usdc.functions.mint(provider.address, 2_000 * RAW))
        token0, token1 = pool.functions.token0().call(), pool.functions.token1().call()
        lo, hi = full_range_ticks(10)
        chain.send(provider.functions.mint(pool_address, lo, hi, 1_000 * RAW, token0, token1))
        guard = chain.deploy("BuckNetSwapGuard", buck.address, usdc.address, factory.address)
        chain.send(usdc.functions.mint(buyer.address, 500 * RAW))
        _exec(chain, buyer, usdc, "approve", [guard.address, 2**256 - 1])
        _exec(chain, buyer, credit, "setCreditIssuer", [gov, True])
        chain.send(credit.functions.createCredit(buyer.address, 0, 200 * RAW, 200 * RAW, 0, 0, 0, 0))
        buyer_id = credit.functions.tokenOfOwnerByIndex(buyer.address, 0).call()
        candidate = credit_ops.assess_issue_candidate(buyer.address, buyer_id, 200 * RAW, 100 * RAW, 0)
        assert candidate.reasons == ()
        issued = credit_ops.issue_candidate(buyer.address, candidate)
        assert issued.principal == 100 * RAW
        # Exercise the same direct, user-paid Universal Router path as the JS
        # market. The two approvals are explicit and local to this fixture.
        def vendored(name):
            artifact = json.loads((ROOT / "alberta_buck/sim/artifacts" / f"{name}.json").read_text())
            return artifact["abi"], artifact["bytecode"]["object"]
        permit_abi, permit_bytecode = vendored("Permit2")
        permit = chain.deploy("Permit2", abi=permit_abi, bytecode=permit_bytecode)
        weth = chain.deploy("WETH9")
        _, pool_bytecode = load_artifact("UniswapV3Pool")
        pool_hash = Web3.keccak(hexstr=pool_bytecode if pool_bytecode.startswith("0x") else "0x" + pool_bytecode)
        router_abi, router_bytecode = vendored("UniversalRouter")
        params = (permit.address, weth.address, ZERO, factory.address, bytes(32), pool_hash, ZERO, ZERO, ZERO, ZERO)
        router = chain.deploy("UniversalRouter", params, abi=router_abi, bytecode=router_bytecode)
        _exec(chain, buyer, buck, "approve", [permit.address, 2**256 - 1])
        _exec(chain, buyer, permit, "approve", [buck.address, router.address, 2**160 - 1, 2**48 - 1])
        def sale_send(owner, target, method, args):
            data = target.encode_abi(method, args=args)
            return chain.send(actors[owner.lower()].functions.exec(target.address, bytes.fromhex(data[2:])),
                              expect=Expect.EITHER)
        sales = NetMarketOps(chain, buck, guard, sale_send, router=router, settlement=usdc)
        before_buck = buck.functions.balanceOf(buyer.address).call()
        before_usdc = usdc.functions.balanceOf(buyer.address).call()
        with pytest.raises(ValueError, match="bounds"):
            sales.sell_exact_input(buyer.address, pool_address, RAW, 0, buyer.address, 500)
        with pytest.raises(ValueError, match="pool or recipient"):
            sales.sell_exact_input(buyer.address, pool_address, RAW, 1, pool_address, 500)
        def unknown_sale(*_args):
            raise RuntimeError("dispatch outcome unavailable")
        unknown_sales = NetMarketOps(chain, buck, guard, unknown_sale, router=router, settlement=usdc)
        with pytest.raises(TransactionReceiptPending) as unknown:
            unknown_sales.sell_exact_input(buyer.address, pool_address, RAW, 1, buyer.address, 500)
        assert not unknown.value.transaction_hash
        with pytest.raises(TransactionReverted) as refused:
            sales.sell_exact_input(buyer.address, pool_address, RAW, 500 * RAW, buyer.address, 500)
        assert refused.value.transaction_hash
        after_buck = buck.functions.balanceOf(buyer.address).call()
        assert 0 <= before_buck - after_buck <= 10  # elapsed demurrage only
        assert usdc.functions.balanceOf(buyer.address).call() == before_usdc
        with pytest.raises(ValueError, match="canonical"):
            sales.sell_exact_input(buyer.address, provider.address, RAW, 1, buyer.address, 500)
        def lost_sale(owner, target, method, args):
            receipt = sale_send(owner, target, method, args)
            assert receipt["status"] == 1
            raise TransactionReceiptPending(receipt["transactionHash"])
        lost_sales = NetMarketOps(chain, buck, guard, lost_sale, router=router, settlement=usdc)
        with pytest.raises(TransactionReceiptPending) as pending_sale:
            lost_sales.sell_exact_input(buyer.address, pool_address, RAW, 1, buyer.address, 500)
        assert pending_sale.value.transaction_hash
        sale_receipt = anvil.w3.eth.get_transaction_receipt(pending_sale.value.transaction_hash)
        sale = sales.sale_from_receipt(buyer.address, pool_address, buyer.address, RAW, 1, sale_receipt,
                                       expected_hash=pending_sale.value.transaction_hash)
        assert sale.nominal_paid == sale.paid == RAW and sale.paid_scaled == RAW * 10**36 and sale.received > 0
        assert credit.functions.issuanceOf(buyer_id).call()[0] == 100 * RAW
        # Sale proceeds are not a mortgage payment. Only a separate confirmed
        # USDC transfer to the simulated bank reduces the modeled liability.
        bank = anvil.w3.eth.accounts[1]
        external_mortgage = 10 * RAW
        assert external_mortgage == 10 * RAW
        paid = SettlementOps(chain, usdc, sale_send).pay_exact(buyer.address, bank, sale.received)
        external_mortgage -= paid.amount
        assert external_mortgage == 10 * RAW - sale.received
        assert usdc.functions.balanceOf(bank).call() == sale.received
        assert credit.functions.issuanceOf(buyer_id).call()[0] == 100 * RAW
        with pytest.raises(ValueError, match="hash mismatch"):
            sales.sale_from_receipt(buyer.address, pool_address, buyer.address, RAW, 1, sale_receipt,
                                    expected_hash="0x" + "00" * 32)
        with pytest.raises(ValueError, match="does not match bounds"):
            sales.sale_from_receipt(buyer.address, pool_address, buyer.address, RAW - 1, 1, sale_receipt,
                                    expected_hash=_txhex(sale_receipt["transactionHash"]))
        duplicate = dict(sale_receipt)
        duplicate["logs"] = list(sale_receipt["logs"]) * 2
        with pytest.raises(ValueError, match="ambiguous"):
            sales.sale_from_receipt(buyer.address, pool_address, buyer.address, RAW, 1, duplicate,
                                    expected_hash=_txhex(sale_receipt["transactionHash"]))
        # Spend the remaining wallet before the later buyback.
        remaining_wallet = buck.functions.balanceOf(buyer.address).call()
        _exec(chain, buyer, buck, "transfer", [provider.address, max(0, remaining_wallet - 2)])
        market = NetMarketOps(chain, buck, guard, lambda owner, target, method, args:
                              _exec(chain, buyer, target, method, args))
        at = anvil.w3.eth.get_block("latest")["timestamp"]
        anvil.warp_to(at + YEAR)
        assert buck.functions.netBalanceOf(pool_address).call() < buck.functions.balanceOf(pool_address).call()
        wallet_before = buck.functions.balanceOf(buyer.address).call()
        purchase = market.buy_exact_input(buyer.address, pool_address, 100 * RAW, 70 * RAW, buyer.address)
        assert purchase.paid == 100 * RAW
        assert 70 * RAW <= purchase.received < purchase.nominal_received
        assert abs(buck.functions.balanceOf(buyer.address).call() - wallet_before - purchase.received) <= 2
        assert credit.functions.issuanceOf(tid).call()[0] == 1_200 * RAW
        assert credit.functions.issuanceOf(buyer_id).call()[0] == 100 * RAW
        # A market purchase alone does not retire credit. The owner authorizes
        # and settles one specific NFT, reducing issuance only at that point.
        now = anvil.w3.eth.get_block("latest")["timestamp"]
        plan = credit_ops.plan_reduction(buyer.address, buyer_id, 10 * RAW)
        assert plan.reason == "ready" and plan.principal == 10 * RAW
        settled = credit_ops.settle_plan(buyer.address, plan, now + 3600)
        assert settled.principal == 10 * RAW and 0 < settled.relief_scaled < 10 * RAW * 10**36
        assert settled.owner_cost_scaled + settled.relief_scaled == settled.principal * 10**36
        assert credit.functions.issuanceOf(buyer_id).call()[0] == 90 * RAW
        assert credit.functions.issuanceOf(tid).call()[0] == 1_200 * RAW
        before = usdc.functions.balanceOf(buyer.address).call()
        exact = market.buy_exact_output(buyer.address, pool_address, 20 * RAW, 50 * RAW, buyer.address)
        assert exact.received >= 20 * RAW and exact.nominal_received > exact.received
        assert usdc.functions.balanceOf(buyer.address).call() == before - exact.paid

        # Exercise the existing adaptive household's discount path using the
        # real guard and selected-NFT settlement. This isolated action is not
        # a full net household deployment, basket or five-year economy.
        agent = CreditHouseholdAgent(7)
        agent.proxy, agent._token_ids = buyer, [buyer_id]
        agent._basket_receipts, agent._receipt_book, agent._savings_proxy = [], {}, None
        agent.p = {"surplus_policy": "adaptive", "paydown_share": .1, "paydown_double": .005,
                   "renew_in": 0, "term_months": 60}
        agent.max_impact_bp = 9000
        agent.unwound = agent.trade_loss = 0
        agent.last_error = agent.last_action = ""
        agent.arrive_day, agent.exited = 0, False
        agent.mortgage, agent._face0, agent.joined = 100 * RAW, 200 * RAW, True
        agent._basket_usd = lambda _: 1.0
        d = SimpleNamespace(buck=buck, credit=credit, kctrl=controller, usdc=usdc, pool_ub=pool_address, fee_ub=500,
                            chain=chain, w3=chain.w3, buck_accounting="net", net_credit_ops=credit_ops,
                            net_market_ops=market, basket_impl="equity")
        observed = agent._state(d, 0)
        capacity = credit_ops.quote_household_activated_capacity(buyer.address, [buyer_id])
        assert observed["activated_capacity"] == capacity.capacity / RAW > 0
        assert observed["issuance_limit"] == capacity.shared_limit / RAW
        assert observed["unactivated"] == capacity.unactivated_present / RAW
        assert not observed["maturities_pending"] and observed["accounting"] == "net"

        class BacklogView:
            def household(self, owner, ids):
                return SimpleNamespace(issued=credit_ops.household(owner, ids).issued, unactivated=10 * RAW)

            def quote_household_activated_capacity(self, _owner, _ids):
                return SimpleNamespace(capacity=0, shared_limit=10 * RAW, unactivated_present=10 * RAW,
                                       maturities_pending=True)

        d.net_credit_ops = BacklogView()
        blocked_state = agent._state(d, 0)
        assert blocked_state["maturities_pending"] and blocked_state["activated_capacity"] == 0
        assert blocked_state["issuance_limit"] == 0
        assert blocked_state["unactivated"] == 0 and blocked_state["spendable"] == observed["spendable"]
        d.net_credit_ops = credit_ops
        ru, rb = active_reserves(chain, pool_address, usdc, buck)
        state = {"ru": ru / RAW, "rb": rb / RAW, "fee": .0005, "basket_usd": 1.5 * ru / rb}
        remaining = credit.functions.issuanceOf(buyer_id).call()[0]
        cash_before = usdc.functions.balanceOf(buyer.address).call()

        def expect_refused_buy(owner, target, method, args):
            data = target.encode_abi(method, args=args)
            return chain.send(actors[owner.lower()].functions.exec(target.address, bytes.fromhex(data[2:])),
                              expect=Expect.REVERT)

        d.net_market_ops = NetMarketOps(chain, buck, guard, expect_refused_buy)
        expensive = state | {"basket_usd": 1.01 * ru / rb}
        with pytest.raises(TransactionReverted):
            agent._save_or_pay_down(d, expensive, cash_before, {})
        assert not agent._net_trade_uncertain and agent._net_repayment_reserve is None
        assert usdc.functions.balanceOf(buyer.address).call() == cash_before
        assert credit.functions.issuanceOf(buyer_id).call()[0] == remaining
        d.net_market_ops = market
        now = anvil.w3.eth.get_block("latest")["timestamp"]
        credit_ops.authorize_reduction(buyer.address, buyer_id, RAW, 0, now + 3600)
        agent._save_or_pay_down(d, state, cash_before, {})
        assert agent.last_action == "repay-pending"
        agent.act(d, None, 0, 0, {})
        assert agent.last_action == "repay-pending"  # untracked authorization blocks the whole turn
        assert usdc.functions.balanceOf(buyer.address).call() == cash_before
        assert credit.functions.issuanceOf(buyer_id).call()[0] == remaining
        credit_ops.cancel_reduction(buyer.address, buyer_id)

        def fail_reduce(owner, target, method, args):
            if target.address == fund.address:
                data = target.encode_abi(method, args=[args[0], args[1] + 1])
                receipt = chain.send(actors[owner.lower()].functions.exec(target.address, bytes.fromhex(data[2:])),
                                     expect=Expect.REVERT)
                raise TransactionReverted(method, "simulated mined refusal", receipt)
            return _exec(chain, actors[owner.lower()], target, method, args)

        d.net_credit_ops = NetCreditOps(chain, stack, fail_reduce)
        counters = {}
        agent._save_or_pay_down(d, state, cash_before, counters)
        cash_after_buy = usdc.functions.balanceOf(buyer.address).call()
        assert cash_after_buy < cash_before and agent._net_repayment_reserve is not None
        assert agent.last_action == "repay-pending" and agent.unwound == 0
        assert agent._net_pending_reverted and agent._net_pending_hash
        assert credit.functions.issuanceOf(buyer_id).call()[0] == remaining
        assert buck.functions.reductionAuthorization(buyer_id).call()[0] > 0
        agent._save_or_pay_down(d, state, cash_after_buy, counters)
        assert usdc.functions.balanceOf(buyer.address).call() == cash_after_buy
        original = buck.functions.reductionAuthorization(buyer_id).call()
        agent._net_pending_reverted = False
        with pytest.raises(ValueError, match="outcome uncertain"):
            agent.cancel_pending_repayment(d)
        assert agent.reconcile_pending_repayment(d, counters) == 0
        assert agent._net_pending_reverted
        credit_ops.authorize_reduction(buyer.address, buyer_id, original[0], original[1] + 1, original[2])
        with pytest.raises(ValueError, match="changed"):
            agent.cancel_pending_repayment(d)
        credit_ops.authorize_reduction(buyer.address, buyer_id, *original)
        agent.cancel_pending_repayment(d)

        class OneSlice:
            def household(self, *args):
                return credit_ops.household(*args)

            def retire_household(self, owner, ids, requested, deadline, **kw):
                # Exercise real settlement with an independent per-attempt
                # limit, as can happen when the fund is short of cash.
                return credit_ops.retire_household(owner, ids, min(requested, 10 * RAW), deadline, **kw)

        d.net_credit_ops = OneSlice()
        ru, rb = active_reserves(chain, pool_address, usdc, buck)
        state.update(ru=ru / RAW, rb=rb / RAW)
        agent._save_or_pay_down(d, state, cash_after_buy, counters)
        first_retired = remaining - credit.functions.issuanceOf(buyer_id).call()[0]
        assert first_retired == 10 * RAW and agent._net_repayment_reserve is not None
        assert usdc.functions.balanceOf(buyer.address).call() == cash_after_buy
        d.net_credit_ops = credit_ops
        ru, rb = active_reserves(chain, pool_address, usdc, buck)
        state.update(ru=ru / RAW, rb=rb / RAW)
        agent._save_or_pay_down(d, state, cash_after_buy, counters)
        retired = remaining - credit.functions.issuanceOf(buyer_id).call()[0]
        assert retired > first_retired and retired == agent.unwound == counters["bcdRetired"]
        assert usdc.functions.balanceOf(buyer.address).call() == cash_after_buy
        assert credit.functions.issuanceOf(tid).call()[0] == 1_200 * RAW

        # A transport can lose the response after a successful settlement.
        # No authorization remains in that case, but the household must not
        # count or retry the uncertain transaction without reconciling it.
        chain.send(credit.functions.createCredit(buyer.address, 0, 200 * RAW, 200 * RAW, 0, 0, 0, 0))
        second_id = credit.functions.tokenOfOwnerByIndex(buyer.address, 1).call()
        credit_ops.issue(buyer.address, second_id, 200 * RAW, 100 * RAW, 0, ZERO)
        _exec(chain, buyer, buck, "transfer", [provider.address, 100 * RAW])
        receipts = []

        def lose_settlement_receipt(owner, target, method, args):
            receipt = _exec(chain, actors[owner.lower()], target, method, args)
            if target.address == fund.address:
                receipts.append(receipt)
                raise TransactionReceiptPending(receipt["transactionHash"])
            return receipt

        class LostReceipt(NetCreditOps):
            def retire_household(self, owner, ids, requested, deadline, **kw):
                return super().retire_household(owner, ids, min(requested, RAW), deadline, **kw)

        agent2 = CreditHouseholdAgent(8)
        agent2.proxy, agent2._token_ids = buyer, [second_id]
        agent2._basket_receipts, agent2._receipt_book, agent2._savings_proxy = [], {}, None
        agent2.p = agent.p.copy()
        agent2.max_impact_bp = 9000
        agent2.unwound = agent2.trade_loss = 0
        agent2.last_error = agent2.last_action = ""
        agent2.arrive_day, agent2.exited = 0, False
        d.net_credit_ops = LostReceipt(chain, stack, lose_settlement_receipt)
        ru, rb = active_reserves(chain, pool_address, usdc, buck)
        state2 = {"ru": ru / RAW, "rb": rb / RAW, "fee": .0005, "basket_usd": 1.5 * ru / rb}
        agent2._save_or_pay_down(d, state2, cash_after_buy, {})
        cash_after_lost = usdc.functions.balanceOf(buyer.address).call()
        assert receipts and receipts[0]["status"] == 1
        assert credit.functions.issuanceOf(second_id).call()[0] == 99 * RAW
        assert buck.functions.reductionAuthorization(second_id).call()[0] == 0
        assert agent2.unwound == 0 and "outcome uncertain" in agent2.last_error and agent2._net_pending_hash
        agent2._save_or_pay_down(d, state2, cash_after_lost, {})
        assert usdc.functions.balanceOf(buyer.address).call() == cash_after_lost
        with pytest.raises(ValueError, match="reconcile"):
            agent2.cancel_pending_repayment(d)
        with pytest.raises(ValueError, match="hash mismatch"):
            d.net_credit_ops.reduction_from_receipt(buyer.address, second_id, RAW, receipts[0],
                                                    expected_hash="0x" + "11" * 32)
        real_hash = agent2._net_pending_hash
        agent2._net_pending_hash = "0x" + "11" * 32
        agent2.act(d, None, 0, 0, {})
        assert agent2.last_action == "repay-pending" and agent2.unwound == 0
        assert usdc.functions.balanceOf(buyer.address).call() == cash_after_lost
        agent2._net_pending_hash = real_hash
        reconciled = {}
        agent2.act(d, None, 0, 0, reconciled)
        assert agent2.last_action == "repay-reconciled"
        assert agent2.unwound == RAW and reconciled["bcdRetired"] == RAW
        assert reconciled["hhPaidDown"] == RAW and usdc.functions.balanceOf(buyer.address).call() == cash_after_lost
        with pytest.raises(ValueError, match="known transaction hash"):
            agent2.reconcile_pending_repayment(d, {})

        # The same uncertainty exists if the market purchase succeeds but
        # the client loses its response before it can start settlement.
        chain.send(credit.functions.createCredit(buyer.address, 0, 200 * RAW, 200 * RAW, 0, 0, 0, 0))
        third_id = credit.functions.tokenOfOwnerByIndex(buyer.address, 2).call()
        credit_ops.issue(buyer.address, third_id, 200 * RAW, 100 * RAW, 0, ZERO)
        _exec(chain, buyer, buck, "transfer", [provider.address, 100 * RAW])
        buy_receipts = []

        def lose_buy_receipt(owner, target, method, args):
            receipt = _exec(chain, actors[owner.lower()], target, method, args)
            buy_receipts.append(receipt)
            raise TransactionReceiptPending(receipt["transactionHash"])

        agent3 = CreditHouseholdAgent(9)
        agent3.proxy, agent3._token_ids = buyer, [third_id]
        agent3._basket_receipts, agent3._receipt_book, agent3._savings_proxy = [], {}, None
        agent3.p = agent.p.copy()
        agent3.max_impact_bp = 9000
        agent3.unwound = agent3.trade_loss = 0
        agent3.last_error = agent3.last_action = ""
        agent3.arrive_day, agent3.exited = 0, False
        d.net_credit_ops = credit_ops
        d.net_market_ops = NetMarketOps(chain, buck, guard, lose_buy_receipt)
        ru, rb = active_reserves(chain, pool_address, usdc, buck)
        state3 = {"ru": ru / RAW, "rb": rb / RAW, "fee": .0005, "basket_usd": 1.5 * ru / rb}
        cash3 = usdc.functions.balanceOf(buyer.address).call()
        with pytest.raises(TransactionReceiptPending):
            agent3._save_or_pay_down(d, state3, cash3, {})
        cash_after_lost_buy = usdc.functions.balanceOf(buyer.address).call()
        assert buy_receipts and buy_receipts[0]["status"] == 1 and cash_after_lost_buy < cash3
        assert agent3._net_trade_uncertain and agent3._net_pending_buy_hash
        assert credit.functions.issuanceOf(third_id).call()[0] == 100 * RAW
        agent3._save_or_pay_down(d, state3, cash_after_lost_buy, {})
        assert "buyback outcome uncertain" in agent3.last_error
        assert usdc.functions.balanceOf(buyer.address).call() == cash_after_lost_buy
        with pytest.raises(ValueError, match="hash mismatch"):
            d.net_market_ops.purchase_from_receipt(buyer.address, pool_address, buyer.address, buy_receipts[0],
                                                   expected_hash="0x" + "11" * 32)
        malformed = dict(buy_receipts[0]) | {"logs": []}
        bad_evidence = NetMarketOps(chain, buck, guard, lambda *_: malformed)
        with pytest.raises(TransactionReceiptPending) as missing:
            bad_evidence.buy_exact_input(buyer.address, pool_address, RAW, 0, buyer.address)
        assert missing.value.transaction_hash == agent3._net_pending_buy_hash
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "buy-reconciled"
        assert not agent3._net_trade_uncertain and agent3._net_repayment_reserve is not None
        assert usdc.functions.balanceOf(buyer.address).call() == cash_after_lost_buy
        with pytest.raises(ValueError, match="known transaction hash"):
            agent3.reconcile_pending_buy(d)
        d.net_credit_ops = OneSlice()
        ru, rb = active_reserves(chain, pool_address, usdc, buck)
        state3.update(ru=ru / RAW, rb=rb / RAW)
        agent3._save_or_pay_down(d, state3, cash_after_lost_buy, {})
        assert credit.functions.issuanceOf(third_id).call()[0] == 90 * RAW
        assert usdc.functions.balanceOf(buyer.address).call() == cash_after_lost_buy

        # A lost authorization response is a separate phase: the fund was
        # never called, so no principal may be counted as retired.
        auth_receipts = []

        def lose_authorization(owner, target, method, args):
            receipt = _exec(chain, actors[owner.lower()], target, method, args)
            if method == "authorizeReduction" and args[1]:
                auth_receipts.append(receipt)
                raise TransactionReceiptPending(receipt["transactionHash"])
            return receipt

        d.net_credit_ops = NetCreditOps(chain, stack, lose_authorization)
        before_principal = credit.functions.issuanceOf(third_id).call()[0]
        before_unwound = agent3.unwound
        assert agent3._net_repayment_reserve is not None
        assert agent3._retire_acquired(d, RAW, 0) == 0
        assert agent3._net_pending_phase == "authorization" and agent3._net_pending_hash
        assert credit.functions.issuanceOf(third_id).call()[0] == before_principal
        assert agent3.unwound == before_unwound
        auth = buck.functions.reductionAuthorization(third_id).call()
        assert auth[0] == RAW and auth_receipts[0]["status"] == 1
        with pytest.raises(ValueError, match="outcome uncertain"):
            agent3.cancel_pending_repayment(d)
        plan = ReductionPlan(third_id, auth[0], 0, auth[1], 0, "ready")
        with pytest.raises(ValueError, match="hash mismatch"):
            credit_ops.authorization_from_receipt(buyer.address, plan, auth[2], auth_receipts[0], "0x" + "11" * 32)
        credit_ops.authorize_reduction(buyer.address, third_id, auth[0], auth[1] + 1, auth[2])
        with pytest.raises(ValueError, match="changed"):
            agent3.reconcile_pending_repayment(d, {})
        credit_ops.authorize_reduction(buyer.address, third_id, *auth)
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "authorize-reconciled"
        assert agent3._net_authorization_confirmed and agent3.unwound == before_unwound
        cancellation_receipts = []

        def lose_cancellation(owner, target, method, args):
            receipt = _exec(chain, actors[owner.lower()], target, method, args)
            if method == "authorizeReduction" and args[1] == 0:
                cancellation_receipts.append(receipt)
                raise TransactionReceiptPending(receipt["transactionHash"])
            return receipt

        d.net_credit_ops = NetCreditOps(chain, stack, lose_cancellation)
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "cancel-pending" and agent3._net_cancel_hash
        assert buck.functions.reductionAuthorization(third_id).call()[0] == 0
        assert cancellation_receipts[0]["status"] == 1 and agent3.unwound == before_unwound
        with pytest.raises(ValueError, match="cancellation outcome uncertain"):
            agent3.cancel_pending_repayment(d)
        cancellation_hash = agent3._net_cancel_hash
        agent3._net_cancel_hash = "0x" + "11" * 32
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "cancel-pending" and agent3._net_cancel_uncertain
        agent3._net_cancel_hash = cancellation_hash
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "repay-canceled" and not agent3._net_cancel_uncertain
        assert agent3._net_pending_token is None
        assert buck.functions.reductionAuthorization(third_id).call()[0] == 0
        assert credit.functions.issuanceOf(third_id).call()[0] == before_principal

        # A mined cancellation revert leaves the authorization in place. The
        # next turn can retry it, but cannot count a repayment.
        d.net_credit_ops = NetCreditOps(chain, stack, lose_authorization)
        assert agent3._retire_acquired(d, RAW, 0) == 0
        agent3.act(d, None, 0, 0, {})
        assert agent3._net_authorization_confirmed

        def refuse_cancellation(owner, target, method, args):
            if method == "authorizeReduction" and args[1] == 0:
                data = target.encode_abi(method, args=[args[0] + 10**9, 0, 0, 0])
                receipt = chain.send(actors[owner.lower()].functions.exec(target.address, bytes.fromhex(data[2:])),
                                     expect=Expect.REVERT)
                raise TransactionReverted(method, "simulated cancellation refusal", receipt)
            return _exec(chain, actors[owner.lower()], target, method, args)

        d.net_credit_ops = NetCreditOps(chain, stack, refuse_cancellation)
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "repay-pending" and not agent3._net_cancel_uncertain
        assert buck.functions.reductionAuthorization(third_id).call()[0] == RAW

        def lose_reverted_cancellation(owner, target, method, args):
            if method == "authorizeReduction" and args[1] == 0:
                data = target.encode_abi(method, args=[args[0] + 10**9, 0, 0, 0])
                receipt = chain.send(actors[owner.lower()].functions.exec(target.address, bytes.fromhex(data[2:])),
                                     expect=Expect.REVERT)
                raise TransactionReceiptPending(receipt["transactionHash"])
            return _exec(chain, actors[owner.lower()], target, method, args)

        d.net_credit_ops = NetCreditOps(chain, stack, lose_reverted_cancellation)
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "cancel-pending" and agent3._net_cancel_hash
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "cancel-reverted" and not agent3._net_cancel_uncertain
        assert buck.functions.reductionAuthorization(third_id).call()[0] == RAW

        def cancel_without_event(owner, target, method, args):
            receipt = _exec(chain, actors[owner.lower()], target, method, args)
            return dict(receipt) | {"logs": []} if method == "authorizeReduction" and args[1] == 0 else receipt

        d.net_credit_ops = NetCreditOps(chain, stack, cancel_without_event)
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "cancel-pending" and agent3._net_cancel_hash
        assert buck.functions.reductionAuthorization(third_id).call()[0] == 0
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "repay-canceled" and agent3._net_pending_token is None
        assert agent3.unwound == before_unwound

        # A mined authorization revert did not authorize or settle anything;
        # reconcile that receipt before allowing another attempt.
        def refuse_authorization(owner, target, method, args):
            if method == "authorizeReduction" and args[1]:
                data = target.encode_abi(method, args=[args[0], args[1], args[2], 0])
                receipt = chain.send(actors[owner.lower()].functions.exec(target.address, bytes.fromhex(data[2:])),
                                     expect=Expect.REVERT)
                raise TransactionReverted(method, "simulated expired authorization", receipt)
            return _exec(chain, actors[owner.lower()], target, method, args)

        d.net_credit_ops = NetCreditOps(chain, stack, refuse_authorization)
        assert agent3._retire_acquired(d, RAW, 0) == 0
        assert agent3._net_pending_phase == "authorization" and agent3._net_pending_reverted
        with pytest.raises(ValueError, match="outcome uncertain"):
            agent3.cancel_pending_repayment(d)
        agent3.act(d, None, 0, 0, {})
        assert agent3._net_pending_token is None and agent3.last_action == "authorize-reverted"
        assert agent3.unwound == before_unwound and credit.functions.issuanceOf(third_id).call()[0] == before_principal

        def lose_authorization_without_hash(owner, target, method, args):
            receipt = _exec(chain, actors[owner.lower()], target, method, args)
            if method == "authorizeReduction" and args[1]:
                raise RuntimeError("transport lost both receipt and hash")
            return receipt

        d.net_credit_ops = NetCreditOps(chain, stack, lose_authorization_without_hash)
        assert agent3._retire_acquired(d, RAW, 0) == 0
        assert agent3._net_pending_phase == "authorization" and agent3._net_pending_hash is None
        assert agent3._pending_repayment(d)
        agent3.act(d, None, 0, 0, {})
        assert agent3.last_action == "repay-pending" and agent3._net_pending_phase == "authorization"
        with pytest.raises(ValueError, match="known transaction hash"):
            agent3.reconcile_pending_repayment(d, {})
        with pytest.raises(ValueError, match="outcome uncertain"):
            agent3.cancel_pending_repayment(d)
        assert credit.functions.issuanceOf(third_id).call()[0] == before_principal
        credit_ops.cancel_reduction(buyer.address, third_id)  # explicit external recovery in this fixture

        def malformed_authorization_response(owner, target, method, args):
            receipt = _exec(chain, actors[owner.lower()], target, method, args)
            return object() if method == "authorizeReduction" and args[1] else receipt

        malformed_ops = NetCreditOps(chain, stack, malformed_authorization_response)
        malformed_batch = malformed_ops.retire_household(
            buyer.address, [third_id], RAW, anvil.w3.eth.get_block("latest")["timestamp"] + 3600)
        assert malformed_batch.status == "authorization-pending"
        assert malformed_batch.pending_phase == "authorization" and malformed_batch.pending_transaction_hash is None
        assert malformed_batch.retired == 0 and credit.functions.issuanceOf(third_id).call()[0] == before_principal
        credit_ops.cancel_reduction(buyer.address, third_id)

        # No cancellation hash means the household cannot safely tell a
        # pending transaction from a completed cancellation after a restart.
        agent7 = CreditHouseholdAgent(13)
        agent7.proxy, agent7._token_ids, agent7._net_repayment_reserve = buyer, [third_id], 1
        agent7.arrive_day, agent7.exited = 0, False
        agent7.unwound = agent7.trade_loss = 0
        agent7.last_error = agent7.last_action = ""
        d.net_credit_ops = NetCreditOps(chain, stack, lose_authorization)
        assert agent7._retire_acquired(d, RAW, 0) == 0
        agent7.act(d, None, 0, 0, {})
        assert agent7._net_authorization_confirmed
        cancel_attempts = []

        def lose_cancellation_without_hash(owner, target, method, args):
            receipt = _exec(chain, actors[owner.lower()], target, method, args)
            if method == "authorizeReduction" and args[1] == 0:
                cancel_attempts.append(receipt)
                raise RuntimeError("transport lost both cancellation receipt and hash")
            return receipt

        d.net_credit_ops = NetCreditOps(chain, stack, lose_cancellation_without_hash)
        agent7.act(d, None, 0, 0, {})
        assert agent7.last_action == "cancel-pending" and agent7._net_cancel_uncertain and not agent7._net_cancel_hash
        assert buck.functions.reductionAuthorization(third_id).call()[0] == 0
        agent7.act(d, None, 0, 0, {})
        assert agent7.last_action == "cancel-pending" and len(cancel_attempts) == 1
        with pytest.raises(ValueError, match="known transaction hash"):
            agent7.reconcile_pending_cancellation(d)
        with pytest.raises(ValueError, match="cancellation outcome uncertain"):
            agent7.cancel_pending_repayment(d)
        assert credit.functions.issuanceOf(third_id).call()[0] == before_principal

        # If the second authorization's response is lost, the first NFT's
        # successfully settled principal and receipt still belong in the batch.
        batch_ids = []
        for _ in range(2):
            chain.send(credit.functions.createCredit(buyer.address, 0, 2 * RAW, 2 * RAW, 0, 0, 0, 0))
            token_id = credit.functions.tokenOfOwnerByIndex(buyer.address, 3 + len(batch_ids)).call()
            credit_ops.issue(buyer.address, token_id, 2 * RAW, RAW, 0, ZERO)
            batch_ids.append(token_id)

        def lose_second_authorization(owner, target, method, args):
            receipt = _exec(chain, actors[owner.lower()], target, method, args)
            if method == "authorizeReduction" and args[0] == batch_ids[1]:
                raise TransactionReceiptPending(receipt["transactionHash"])
            return receipt

        batch_ops = NetCreditOps(chain, stack, lose_second_authorization)
        captured = []

        class Capture:
            def __init__(self, ops):
                self.ops = ops

            def retire_household(self, *args, **kwargs):
                result = self.ops.retire_household(*args, **kwargs)
                captured.append(result)
                return result

        # An unavailable post-batch balance must not erase the earlier
        # settlement or the pending authorization's exact transaction data.
        bad_balance = SimpleNamespace(buck=object(), w3=d.w3, buck_accounting="net", net_credit_ops=Capture(batch_ops))
        agent4 = CreditHouseholdAgent(10)
        agent4.proxy, agent4._token_ids, agent4._net_repayment_reserve = buyer, batch_ids, 1
        agent4.last_error = ""
        assert agent4._retire_acquired(bad_balance, 2 * RAW, 0) == RAW
        batch = captured.pop()
        assert batch.retired == RAW and len(batch.settlements) == 1
        assert batch.status == "authorization-pending" and batch.pending_phase == "authorization"
        assert batch.blocked_token_id == batch_ids[1] and batch.pending_transaction_hash
        assert agent4._net_pending_hash == batch.pending_transaction_hash and agent4._net_repayment_reserve == 1
        assert credit.functions.issuanceOf(batch_ids[0]).call()[0] == 0
        assert credit.functions.issuanceOf(batch_ids[1]).call()[0] == RAW
        credit_ops.cancel_reduction(buyer.address, batch_ids[1])

        def new_small_facility():
            index = credit.functions.balanceOf(buyer.address).call()
            chain.send(credit.functions.createCredit(buyer.address, 0, 2 * RAW, 2 * RAW, 0, 0, 0, 0))
            token_id = credit.functions.tokenOfOwnerByIndex(buyer.address, index).call()
            credit_ops.issue(buyer.address, token_id, 2 * RAW, RAW, 0, ZERO)
            return token_id

        first, second = new_small_facility(), new_small_facility()

        class PlanUnavailable(NetCreditOps):
            def plan_reduction(self, owner, token_id, requested, **kwargs):
                if token_id == second:
                    raise ConnectionError("simulated planning read failure")
                return super().plan_reduction(owner, token_id, requested, **kwargs)

        bad_balance.net_credit_ops = Capture(PlanUnavailable(chain, stack, credit_ops.execute))
        agent5 = CreditHouseholdAgent(11)
        agent5.proxy, agent5._token_ids, agent5._net_repayment_reserve = buyer, [first, second], 1
        agent5.last_error = ""
        assert agent5._retire_acquired(bad_balance, 2 * RAW, 0) == RAW
        failed_plan = captured.pop()
        assert failed_plan.status == "plan-unavailable" and failed_plan.blocked_token_id == second
        assert failed_plan.retired == RAW and len(failed_plan.settlements) == 1
        assert "planning read failure" in failed_plan.error and "plan-unavailable" in agent5.last_error
        assert agent5._net_repayment_reserve == 1 and agent5._pending_repayment(bad_balance)
        agent5.arrive_day, agent5.exited = 0, False
        agent5.act(bad_balance, None, 0, 0, {})
        assert agent5.last_action == "repay-recheck" and not agent5._net_repayment_interrupted
        assert credit.functions.issuanceOf(first).call()[0] == 0
        assert credit.functions.issuanceOf(second).call()[0] == RAW

        first, second = new_small_facility(), new_small_facility()

        class PreflightUnavailable(NetCreditOps):
            def settle_plan(self, owner, plan, deadline):
                if plan.token_id == second:
                    raise ConnectionError("simulated authorization preflight failure")
                return super().settle_plan(owner, plan, deadline)

        deadline = anvil.w3.eth.get_block("latest")["timestamp"] + 3600
        batch = PreflightUnavailable(chain, stack, credit_ops.execute).retire_household(
            buyer.address, [first, second], 2 * RAW, deadline)
        assert batch.status == "preflight-unavailable" and batch.blocked_token_id == second
        assert batch.retired == RAW and len(batch.settlements) == 1
        assert credit.functions.issuanceOf(first).call()[0] == 0
        assert credit.functions.issuanceOf(second).call()[0] == RAW

        first = new_small_facility()
        bad_balance.net_credit_ops = Capture(credit_ops)
        agent6 = CreditHouseholdAgent(12)
        agent6.proxy, agent6._token_ids, agent6._net_repayment_reserve = buyer, [first], 1
        agent6.last_error = ""
        assert agent6._retire_acquired(bad_balance, 2 * RAW, 0) == RAW
        partial = captured.pop()
        assert partial.status == "partial" and partial.retired == RAW
        assert "balance unavailable" in agent6.last_error and agent6._net_repayment_reserve == 1
        assert agent6._pending_repayment(bad_balance)
        assert credit.functions.issuanceOf(first).call()[0] == 0
        assert not chain.mismatches
