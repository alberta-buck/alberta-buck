"""A household must not guess missing terms for an unfinished buyback."""
from types import SimpleNamespace

import pytest

from alberta_buck.sim.credit_households import CreditHouseholdAgent
from buck_core import TransactionReceiptPending, TransactionReverted


def test_unknown_buyback_target_stops_the_household_before_a_new_action():
    agent = CreditHouseholdAgent(0)
    agent.arrive_day, agent.exited = 0, False
    agent._token_ids = []
    agent._net_repayment_reserve = 1
    agent._net_repayment_target = None
    counters = {}
    agent.act(SimpleNamespace(buck_accounting="net"), None, 0, 0, counters)
    assert agent.last_action == "repay-pending"
    assert "target or reserve unavailable" in agent.last_error
    assert agent._net_repayment_reserve == 1
    with pytest.raises(ValueError, match="target unavailable"):
        agent._retire_acquired(SimpleNamespace(buck_accounting="net"), 25, 0)

    agent._net_repayment_reserve, agent._net_repayment_target = None, 25
    agent.act(SimpleNamespace(buck_accounting="net"), None, 0, 0, counters)
    assert agent.last_action == "repay-pending"
    assert agent._net_repayment_target == 25
    with pytest.raises(ValueError, match="reserve unavailable"):
        agent._retire_acquired(SimpleNamespace(buck_accounting="net"), 25, 0)


def test_daily_turn_finishes_only_the_saved_buyback_target():
    agent = CreditHouseholdAgent(1)
    agent.arrive_day, agent.exited, agent._token_ids = 0, False, []
    agent.proxy = SimpleNamespace(address="payer")
    agent.p = {"surplus_policy": "adaptive"}
    agent.unwound = 0
    agent._net_repayment_reserve, agent._net_repayment_target = 1, 25
    requested = []

    class Credit:
        def household(self, _owner, _ids):
            return SimpleNamespace(issued=25)

        def retire_household(self, _owner, _ids, target, _deadline, **_kwargs):
            requested.append(target)
            return SimpleNamespace(retired=min(10, target), status="partial")

    class Balance:
        def balanceOf(self, _owner):
            return SimpleNamespace(call=lambda: 100)

    world = SimpleNamespace(buck_accounting="net", net_credit_ops=Credit(),
                            buck=SimpleNamespace(functions=Balance()),
                            w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda _: {"timestamp": 0})))
    counters = {}
    for _ in range(3):
        agent.act(world, None, 0, 0, counters)
    assert requested == [25, 15, 5]
    assert agent.unwound == counters["bcdRetired"] == counters["hhPaidDown"] == 25
    assert agent._net_repayment_reserve is agent._net_repayment_target is None

    # Another authorized action can clear the facilities while this plan is
    # waiting. The purchased BUCKs are then available; no repayment is sent.
    agent._net_repayment_reserve, agent._net_repayment_target = 1, 25
    world.net_credit_ops.household = lambda *_: SimpleNamespace(issued=0)
    agent.act(world, None, 0, 0, counters)
    assert requested == [25, 15, 5]
    assert agent._net_repayment_reserve is agent._net_repayment_target is None


def test_revert_without_hash_keeps_buyback_terms_and_stops_retry():
    agent = CreditHouseholdAgent(2)
    agent.proxy = SimpleNamespace(address="payer")
    agent._net_repayment_reserve = agent._net_repayment_target = None

    class Balance:
        def balanceOf(self, _owner):
            return SimpleNamespace(call=lambda: 10)

    class Market:
        def buy_exact_input(self, *_args):
            raise TransactionReverted("buy", "missing hash", {"status": 0})

    world = SimpleNamespace(buck_accounting="net", chain=SimpleNamespace(balance_of=lambda *_: 100),
                            buck=SimpleNamespace(functions=Balance()), usdc=object(),
                            pool_ub="pool", net_market_ops=Market())
    with pytest.raises(TransactionReceiptPending) as pending:
        agent._buy_cash(world, 50, 1, for_repayment=True, repayment_target=25)
    assert not pending.value.transaction_hash
    assert agent._net_trade_uncertain and agent._net_repayment_reserve == 11
    assert agent._net_repayment_target == 25

    for result in (None, SimpleNamespace(paid=51, received=25), SimpleNamespace(paid=50, received=0)):
        agent._net_repayment_reserve = agent._net_repayment_target = None
        world.net_market_ops = SimpleNamespace(buy_exact_input=lambda *_, result=result: result)
        with pytest.raises(TransactionReceiptPending) as malformed:
            agent._buy_cash(world, 50, 1, for_repayment=True, repayment_target=25)
        assert not malformed.value.transaction_hash
        assert agent._net_trade_uncertain and agent._net_repayment_target == 25
