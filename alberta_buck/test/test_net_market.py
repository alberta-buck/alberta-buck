"""A real V3/Anvil cash-only guard fixture; not a household economy."""
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from alberta_buck.sim.anvil import Anvil
from alberta_buck.sim.chain import Chain
from alberta_buck.sim.credit_households import CreditHouseholdAgent
from alberta_buck.sim.gauge import active_reserves
from alberta_buck.sim.router import full_range_ticks, sqrt_price_x96
from buck_core import Expect, NetCreditOps, NetMarketOps, TransactionReceiptPending, TransactionReverted, deploy_net_stack

RAW = 10**6
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
        credit_ops.issue(provider.address, tid, 2_000 * RAW, 1_200 * RAW)

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
        credit_ops.issue(buyer.address, buyer_id, 200 * RAW, 100 * RAW)
        # Simulate the borrower having spent the issuance before buying it back.
        _exec(chain, buyer, buck, "transfer", [provider.address, 100 * RAW])
        market = NetMarketOps(chain, buck, guard, lambda owner, target, method, args:
                              _exec(chain, buyer, target, method, args))
        at = anvil.w3.eth.get_block("latest")["timestamp"]
        anvil.warp_to(at + YEAR)
        assert buck.functions.netBalanceOf(pool_address).call() < buck.functions.balanceOf(pool_address).call()
        purchase = market.buy_exact_input(buyer.address, pool_address, 100 * RAW, 70 * RAW, buyer.address)
        assert purchase.paid == 100 * RAW
        assert 70 * RAW <= purchase.received < purchase.nominal_received
        assert buck.functions.balanceOf(buyer.address).call() == purchase.received
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
        agent.p = {"surplus_policy": "adaptive", "paydown_share": .1, "paydown_double": .005}
        agent.max_impact_bp = 9000
        agent.unwound = agent.trade_loss = 0
        agent.last_error = agent.last_action = ""
        d = SimpleNamespace(buck=buck, credit=credit, usdc=usdc, pool_ub=pool_address, fee_ub=500,
                            chain=chain, w3=chain.w3, buck_accounting="net", net_credit_ops=credit_ops,
                            net_market_ops=market, basket_impl="equity")
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
        credit_ops.issue(buyer.address, second_id, 200 * RAW, 100 * RAW)
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
        reconciled = {}
        assert agent2.reconcile_pending_repayment(d, reconciled) == RAW
        assert agent2.unwound == RAW and reconciled["bcdRetired"] == RAW
        with pytest.raises(ValueError, match="known transaction hash"):
            agent2.reconcile_pending_repayment(d, {})

        # The same uncertainty exists if the market purchase succeeds but
        # the client loses its response before it can start settlement.
        chain.send(credit.functions.createCredit(buyer.address, 0, 200 * RAW, 200 * RAW, 0, 0, 0, 0))
        third_id = credit.functions.tokenOfOwnerByIndex(buyer.address, 2).call()
        credit_ops.issue(buyer.address, third_id, 200 * RAW, 100 * RAW)
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
        assert agent3.reconcile_pending_buy(d) > 0
        assert not agent3._net_trade_uncertain and agent3._net_repayment_reserve is not None
        with pytest.raises(ValueError, match="known transaction hash"):
            agent3.reconcile_pending_buy(d)
        d.net_credit_ops = OneSlice()
        ru, rb = active_reserves(chain, pool_address, usdc, buck)
        state3.update(ru=ru / RAW, rb=rb / RAW)
        agent3._save_or_pay_down(d, state3, cash_after_lost_buy, {})
        assert credit.functions.issuanceOf(third_id).call()[0] == 90 * RAW
        assert usdc.functions.balanceOf(buyer.address).call() == cash_after_lost_buy
        assert not chain.mismatches
