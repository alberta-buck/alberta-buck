"""Economic specification of net-holder demurrage and linear credit relief."""
import math
import copy
from decimal import Decimal, localcontext

import pytest

from alberta_buck.sim.demurrage_reference import (
    JUBILEE, JUBILEE_INVESTED, DemurrageLedger, InsufficientFunds,
    carrying_gross_for_net_units, main, snapshot,
)


def opened(amount=100.0):
    ledger = DemurrageLedger()
    ledger.issue("mortgage", amount, "holder")
    return ledger


def assert_conservation(ledger):
    assert sum(ledger.accounts.values()) + sum(ledger.unspent.values()) == pytest.approx(
        ledger.issued, abs=1e-9
    )
    assert ledger.gross_demurrage == pytest.approx(
        ledger.from_holders + ledger.recycled_by_jubilee, abs=1e-9
    )
    assert ledger.jubilee_fee_created + ledger.jubilee_fee_inherited - ledger.jubilee_fee_realized == pytest.approx(
        ledger.jubilee_carried_fee, abs=1e-9
    )
    assert ledger.jubilee_nominal == pytest.approx(
        ledger.jubilee + ledger.jubilee_carried_fee, abs=1e-9
    )
    assert ledger.jubilee == pytest.approx(
        ledger.fund_demurrage_received + ledger.fund_withdrawn + ledger.fund_other_in
        - ledger.fund_relief_paid - ledger.fund_invested - ledger.fund_other_out,
        abs=1e-9,
    )


def test_issued_unspent_credit_accrues_demurrage_with_zero_ordinary_balance():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 1_000_000, "owner")
    assert ledger.accounts["owner"] == 0
    assert ledger.unspent["owner"] == 1_000_000
    assert ledger.balance_of("owner") == 1_000_000
    assert ledger.positions["facility"].principal == 1_000_000
    assert ledger.issued == 1_000_000

    ledger.advance(1)
    expected_unspent = 1_000_000 * math.exp(-0.02)
    assert ledger.accounts["owner"] == 0
    assert ledger.unspent["owner"] == pytest.approx(expected_unspent)
    assert ledger.balance_of("owner") == pytest.approx(expected_unspent)
    assert ledger.jubilee == pytest.approx(1_000_000 - expected_unspent)
    assert ledger.positions["facility"].buck_years == pytest.approx(1_000_000)
    assert ledger.quote_retirement("facility", 1_000_000).relief == pytest.approx(20_000)
    assert_conservation(ledger)

    result = ledger.retire_available("facility", 1_000_000, "owner")
    assert result.relief == pytest.approx(1_000_000 - expected_unspent)
    assert result.account_payment == pytest.approx(expected_unspent)
    assert ledger.accounts["owner"] == 0
    assert ledger.unspent["owner"] == pytest.approx(0, abs=1e-9)
    assert ledger.positions["facility"].principal == 0
    assert_conservation(ledger)


def test_spending_unspent_moves_buck_without_reissuing_or_closing_credit():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 100, "owner")
    ledger.spend_unspent("owner", "recipient", 40)
    assert ledger.accounts["owner"] == 0
    assert ledger.unspent["owner"] == 60
    assert ledger.accounts["recipient"] == 40
    assert ledger.issued == 100
    assert ledger.positions["facility"].principal == 100

    ledger.transfer("recipient", "owner", 5)
    assert ledger.accounts["owner"] == 5
    assert ledger.unspent["owner"] == 60
    assert ledger.positions["facility"].principal == 100
    ledger.advance(1)
    assert ledger.quote_retirement("facility", 100).relief == pytest.approx(2)
    assert_conservation(ledger)


def test_erc20_transfer_spends_held_and_unspent_without_reissuing_credit():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 100, "owner")
    ledger.transfer("owner", "recipient", 40)
    ledger.transfer("recipient", "owner", 5)
    assert ledger.accounts["owner"] == 5
    assert ledger.unspent["owner"] == 60
    assert ledger.balance_of("owner") == 65
    ledger.transfer("owner", "recipient", 50)
    assert ledger.accounts["owner"] == 0
    assert ledger.unspent["owner"] == 15
    assert ledger.balance_of("owner") == 15
    assert ledger.positions["facility"].principal == 100
    assert ledger.issued == 100
    assert_conservation(ledger)


def test_separate_facilities_keep_independent_issuance_and_relief_ages():
    ledger = DemurrageLedger()
    ledger.issue_unspent("old", 1_000_000, "owner")
    ledger.advance(1)
    ledger.issue_unspent("new", 500_000, "owner")
    ledger.advance(2)
    assert ledger.quote_retirement("old", 1_000_000).relief == pytest.approx(60_000)
    assert ledger.quote_retirement("new", 500_000).relief == pytest.approx(20_000)
    old_before = (ledger.positions["old"].principal, ledger.positions["old"].buck_years)

    result = ledger.retire_available("new", 250_000, "owner")
    assert result.baseline_relief == pytest.approx(10_000)
    assert result.redeemed_fraction == pytest.approx(10_000 / 80_000)
    assert ledger.positions["new"].principal == pytest.approx(250_000)
    assert ledger.positions["new"].buck_years == pytest.approx(500_000)
    assert (ledger.positions["old"].principal, ledger.positions["old"].buck_years) == old_before
    assert ledger.issued == pytest.approx(1_250_000)
    assert_conservation(ledger)


def test_proportional_payout_preserves_cash_for_equal_facilities():
    ledger = DemurrageLedger()
    ledger.issue_unspent("a", 100, "alice")
    ledger.issue_unspent("b", 100, "bob")
    ledger.advance(10)
    expected_relief = 100 * (1 - math.exp(-0.2))
    reserve_quote = ledger.quote_available_retirement("a", 100)
    proportional_quote = ledger.quote_available_retirement("a", 100, payout_policy="proportional")
    assert reserve_quote.relief == pytest.approx(expected_relief * 2)
    assert proportional_quote.relief == pytest.approx(expected_relief)

    first = ledger.retire_available("a", 100, "alice", payout_policy="proportional")
    assert first.relief == pytest.approx(proportional_quote.relief)
    assert ledger.jubilee == pytest.approx(expected_relief)
    second = ledger.retire_available("b", 100, "bob", payout_policy="proportional")
    assert second.relief == pytest.approx(expected_relief)
    assert ledger.balance_of("alice") == pytest.approx(0, abs=1e-9)
    assert ledger.balance_of("bob") == pytest.approx(0, abs=1e-9)
    assert_conservation(ledger)


def test_proportional_payout_uses_realized_strategy_return_and_matches_preview():
    ledger = DemurrageLedger()
    ledger.issue_unspent("a", 100, "alice")
    ledger.issue_unspent("b", 100, "bob")
    ledger.advance(10)
    ledger.deploy(20)
    ledger.issue_unspent("gain-source", 5, "donor")
    ledger.strategy_return("donor", 5)
    cash_before = ledger.jubilee
    quote = ledger.quote_available_retirement("a", 100, payout_policy="proportional")
    assert quote.redeemed_fraction == pytest.approx(0.5)
    assert quote.redemption_proceeds == pytest.approx(12.5)
    assert quote.relief == pytest.approx(cash_before / 2 + 12.5)
    result = ledger.retire_available("a", 100, "alice", payout_policy="proportional")
    assert result.relief == pytest.approx(quote.relief)
    assert result.rate_multiplier == pytest.approx(result.relief / result.baseline_relief)
    assert ledger.jubilee == pytest.approx(cash_before / 2)
    assert_conservation(ledger)


@pytest.mark.parametrize("portfolio_ratio", [1.025, 0.95])
def test_proportional_payout_realizes_the_funds_actual_premium_or_discount(portfolio_ratio):
    ledger = DemurrageLedger()
    ledger.issue_unspent("a", 100, "alice")
    ledger.issue_unspent("b", 100, "bob")
    ledger.advance(10)
    ledger.issue_unspent("gain-source", 10, "donor")
    target_value = portfolio_ratio * ledger.aggregate_relief
    ledger.transfer("donor", JUBILEE, target_value - ledger.jubilee)
    reserve = 0.1 * ledger.aggregate_relief
    ledger.deploy(ledger.jubilee - reserve)

    quote = ledger.quote_available_retirement("a", 100, payout_policy="proportional")
    assert quote.baseline_relief == pytest.approx(20)
    assert quote.rate_multiplier == pytest.approx(portfolio_ratio)
    assert quote.relief == pytest.approx(20 * portfolio_ratio)
    result = ledger.retire_available("a", 100, "alice", payout_policy="proportional")
    assert result.relief == pytest.approx(quote.relief)
    assert_conservation(ledger)


def test_proportional_payout_cannot_pay_a_zero_age_facility():
    ledger = DemurrageLedger()
    ledger.issue_unspent("old", 100, "alice")
    ledger.advance(1)
    ledger.issue_unspent("new", 100, "bob")
    quote = ledger.quote_available_retirement("new", 100, 0.5, payout_policy="proportional")
    assert quote.baseline_relief == 0
    assert quote.relief == 0
    assert_conservation(ledger)


def test_proportional_payout_handles_zero_sale_proceeds_and_keeps_other_cash():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 100, "owner")
    ledger.advance(10)
    ledger.deploy(10)
    ledger.strategy_loss("owner", 10)
    cash_before = ledger.jubilee
    result = ledger.retire_available("facility", 50, "owner", payout_policy="proportional")
    assert result.redemption_proceeds == 0
    assert result.relief == pytest.approx(cash_before / 2)
    assert ledger.jubilee == pytest.approx(cash_before / 2)
    assert_conservation(ledger)


def test_renewal_can_apply_the_proportional_payout_policy():
    ledger = DemurrageLedger()
    ledger.issue_unspent("a", 100, "alice", credit_limit=100)
    ledger.issue_unspent("b", 100, "bob", credit_limit=100)
    ledger.advance(10)
    expected_relief = 100 * (1 - math.exp(-0.2))
    renewal = ledger.renew_unspent("a", "alice", new_limit=100, new_premium_rate=0,
                                  payout_policy="proportional")
    assert renewal.relief == pytest.approx(expected_relief)
    assert renewal.new_principal == pytest.approx(100 - expected_relief)
    second = ledger.retire_available("b", 100, "bob", payout_policy="proportional")
    assert second.relief == pytest.approx(expected_relief)
    assert_conservation(ledger)


def test_owned_credit_cannot_be_reduced_by_another_account():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 100, "owner")
    with pytest.raises(ValueError, match="different owner"):
        ledger.retire_available("facility", 10, "other")
    assert_conservation(ledger)


def test_premium_deposits_and_releases_use_each_credit_insurer_pool():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer-a", "pool-a")
    ledger.register_insurer_pool("insurer-b", "pool-b")
    ledger.issue_unspent("credit-a", 100, "alice", insurer="insurer-a", premium_deposit=10)
    ledger.issue_unspent("credit-b", 200, "bob", insurer="insurer-b", premium_deposit=20)
    assert ledger.unspent["alice"] == 90
    assert ledger.unspent["bob"] == 180
    assert ledger.accounts["pool-a"] == 10
    assert ledger.accounts["pool-b"] == 20
    assert ledger.positions["credit-a"].insurance_pool == "pool-a"
    assert ledger.positions["credit-b"].insurance_pool == "pool-b"

    result = ledger.retire_available("credit-a", 100, "alice")
    assert result.insurance_pool == "pool-a"
    assert result.insurance_refund == 10
    assert ledger.accounts["pool-a"] == 0
    assert ledger.accounts["pool-b"] == 20
    assert ledger.positions["credit-b"].insurance_principal == 20
    assert ledger.positions["credit-b"].principal == 200
    assert_conservation(ledger)


def test_insurer_pool_cannot_be_replaced_while_its_deposit_is_active():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer", "old-pool")
    ledger.issue_unspent("credit", 100, "owner", insurer="insurer", premium_deposit=10)
    with pytest.raises(ValueError, match="original pool"):
        ledger.register_insurer_pool("insurer", "new-pool")
    assert ledger.insurer_pools["insurer"] == "old-pool"
    assert ledger.positions["credit"].insurance_pool == "old-pool"
    assert_conservation(ledger)


def test_distinct_insurers_cannot_share_pool_or_another_insurers_strategy():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer-a", "pool-a")
    with pytest.raises(ValueError, match="own pool"):
        ledger.register_insurer_pool("insurer-b", "pool-a")
    with pytest.raises(ValueError, match="investment cannot"):
        ledger.register_insurer_pool("insurer-b", ledger.insurance_strategies["pool-a"])
    assert "insurer-b" not in ledger.insurer_pools
    assert_conservation(ledger)


def test_reference_does_not_silently_issue_to_a_carrying_pool():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer", "pool")
    with pytest.raises(ValueError, match="non-Carrying facility owners"):
        ledger.issue_unspent("facility", 100, "pool")
    ledger.issue_unspent("facility", 100, "owner")
    with pytest.raises(ValueError, match="before funding it or issuing credit"):
        ledger.register_insurer_pool("insurer-2", "owner")
    assert ledger.balance_of("owner") == 100
    assert_conservation(ledger)


def test_insolvent_insurer_pool_does_not_use_another_insurers_funds():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer-a", "pool-a")
    ledger.register_insurer_pool("insurer-b", "pool-b")
    ledger.issue_unspent("credit-a", 100, "alice", insurer="insurer-a", premium_deposit=10)
    ledger.issue_unspent("credit-b", 200, "bob", insurer="insurer-b", premium_deposit=20)
    ledger.transfer("pool-a", "outsider", 10)
    before = (ledger.accounts.copy(), ledger.unspent.copy(), ledger.strategy_units)
    with pytest.raises(InsufficientFunds, match="insurance pool pool-a"):
        ledger.retire_available("credit-a", 100, "alice")
    assert (ledger.accounts, ledger.unspent, ledger.strategy_units) == before
    assert ledger.positions["credit-a"].principal == 100
    assert ledger.accounts["pool-b"] == 20
    assert_conservation(ledger)


def test_insurance_pool_returns_nominal_amount_with_average_carried_demurrage():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer", "pool")
    ledger.issue_unspent("credit", 100, "owner", insurer="insurer", premium_deposit=10)
    ledger.advance(1)
    pool_net = ledger.accounts["pool"]
    pool_fee = ledger.insurance_carried_fee["pool"]
    quote = ledger.quote_available_retirement("credit", 50)
    assert quote.insurance_principal_released == 5
    assert quote.insurance_nominal_required == 5
    assert quote.insurance_refund == pytest.approx(pool_net / 2)
    sent_before = ledger.insurance_nominal_sent["pool"]
    result = ledger.retire_available("credit", 50, "owner")
    assert ledger.insurance_nominal_sent["pool"] - sent_before == pytest.approx(
        result.insurance_nominal_required
    )
    assert ledger.accounts["pool"] == pytest.approx(pool_net / 2)
    assert ledger.insurance_carried_fee["pool"] == pytest.approx(pool_fee / 2)
    assert ledger.positions["credit"].insurance_principal == 5
    assert_conservation(ledger)


def test_insurer_redeems_own_investment_before_nominal_refund():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer-a", "pool-a")
    ledger.register_insurer_pool("insurer-b", "pool-b")
    ledger.issue_unspent("credit-a", 100, "alice", insurer="insurer-a", premium_deposit=10)
    ledger.issue_unspent("credit-b", 200, "bob", insurer="insurer-b", premium_deposit=20)
    ledger.invest_insurance("pool-a", 7)
    ledger.advance(5)
    ledger.issue_unspent("new", 20, "donor")
    ledger.spend_unspent("donor", "pool-a", 5)
    expected_net = 5 + 5 * math.exp(-0.02 * 5)
    pool_b_before = (ledger.accounts["pool-b"], ledger.insurance_carried_fee["pool-b"])
    sent_before = ledger.insurance_nominal_sent["pool-a"]

    quote = ledger.quote_available_retirement("credit-a", 100)
    assert quote.insurance_nominal_required == 10
    assert quote.insurance_refund == pytest.approx(expected_net)
    result = ledger.retire_available("credit-a", 100, "alice")
    assert result.insurance_refund == pytest.approx(expected_net)
    assert ledger.insurance_nominal_sent["pool-a"] - sent_before == pytest.approx(10)
    assert ledger.balance_of("pool-a") == pytest.approx(0, abs=1e-9)
    assert ledger.balance_of(ledger.insurance_strategies["pool-a"]) == pytest.approx(5)
    assert (ledger.accounts["pool-b"], ledger.insurance_carried_fee["pool-b"]) == pool_b_before
    assert_conservation(ledger)


def test_carrying_transfers_preserve_fee_history_between_fund_and_insurer():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer", "pool")
    ledger.issue_unspent("credit", 100, "owner", insurer="insurer", premium_deposit=10)
    ledger.advance(1)
    pool_net = ledger.accounts["pool"]
    pool_fee = ledger.insurance_carried_fee["pool"]
    fund_fee = ledger.jubilee_carried_fee
    ledger.transfer("pool", JUBILEE, 5)
    assert ledger.jubilee_fee_inherited == pytest.approx(pool_fee / 2)
    assert ledger.jubilee_carried_fee == pytest.approx(fund_fee + pool_fee / 2)
    assert ledger.accounts["pool"] == pytest.approx(pool_net / 2)
    assert_conservation(ledger)

    inherited_before = ledger.insurance_carried_fee["pool"]
    fund_nominal = ledger.jubilee_nominal
    fund_carry = ledger.jubilee_carried_fee
    ledger.transfer(JUBILEE, "pool", fund_nominal / 2)
    assert ledger.insurance_carried_fee["pool"] == pytest.approx(inherited_before + fund_carry / 2)
    assert_conservation(ledger)


def test_idle_facility_can_close_using_nominal_insurance_refund_and_net_relief():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer", "pool")
    ledger.issue_unspent("facility", 100, "owner", insurer="insurer", premium_deposit=10)
    ledger.advance(50)
    result = ledger.retire_available("facility", 100, "owner")
    assert result.insurance_principal_released == 10
    assert result.insurance_nominal_required == 10
    assert result.insurance_refund == pytest.approx(10 * math.exp(-1))
    assert result.relief == pytest.approx(100 * (1 - math.exp(-1)))
    assert ledger.insurance_nominal_sent["pool"] == pytest.approx(10)
    assert ledger.positions["facility"].principal == 0
    assert_conservation(ledger)


def test_renewal_crystallizes_facility_relief_and_adds_capacity_atomically():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 100, "owner", credit_limit=100, premium_rate=0.02)
    ledger.spend_unspent("owner", "recipient", 80)
    ledger.advance(10)
    unspent = ledger.unspent["owner"]
    cash = ledger.jubilee
    result = ledger.renew_unspent("facility", "owner", new_limit=150,
                                  new_premium_rate=0.01, additional_issuance=50)
    assert result.baseline_relief == pytest.approx(20)
    assert result.relief == pytest.approx(cash)
    assert result.new_principal == pytest.approx(100 - cash + 50)
    assert ledger.positions["facility"].principal == pytest.approx(result.new_principal)
    assert ledger.positions["facility"].credit_limit == 150
    assert ledger.positions["facility"].premium_rate == 0.01
    assert ledger.positions["facility"].buck_years == 0
    assert ledger.unspent["owner"] == pytest.approx(unspent + 50)
    assert ledger.accounts["owner"] == pytest.approx(0)
    assert_conservation(ledger)
    ledger.advance(1)
    assert ledger.quote_retirement("facility", result.new_principal).relief == pytest.approx(
        result.new_principal * 0.02
    )
    assert_conservation(ledger)


def test_renewal_lower_premium_refunds_original_insurer_pool():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer", "pool")
    ledger.issue_unspent("facility", 100, "owner", credit_limit=100,
                         premium_rate=0.02, insurer="insurer", premium_deposit=10)
    ledger.advance(1)
    old_pool_net = ledger.accounts["pool"]
    before = ledger.insurance_nominal_sent["pool"]
    result = ledger.renew_unspent("facility", "owner", new_limit=120,
                                  new_premium_rate=0.01, new_insurance_principal=5)
    assert result.insurance_principal_released == 10
    assert result.insurance_refunded == pytest.approx(old_pool_net)
    assert result.insurance_funded == 5
    assert result.old_insurance_pool == result.new_insurance_pool == "pool"
    assert ledger.insurance_nominal_sent["pool"] - before == 10
    assert ledger.accounts["pool"] == pytest.approx(5)
    assert ledger.positions["facility"].insurance_principal == 5
    assert ledger.positions["facility"].premium_rate == 0.01
    assert_conservation(ledger)


def test_renewal_moves_premium_between_insurers_without_replacing_nft():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("a", "pool-a")
    ledger.register_insurer_pool("b", "pool-b")
    ledger.issue_unspent("facility", 100, "owner", credit_limit=100,
                         premium_rate=0.02, insurer="a", premium_deposit=10)
    result = ledger.renew_unspent("facility", "owner", new_limit=100,
                                  new_premium_rate=0.01, new_insurer="b",
                                  new_insurance_principal=8)
    assert result.position == "facility"
    assert result.insurance_refunded == 10
    assert result.insurance_funded == 8
    assert ledger.accounts["pool-a"] == 0
    assert ledger.accounts["pool-b"] == 8
    assert ledger.accounts["owner"] == 2
    assert ledger.unspent["owner"] == 90
    assert ledger.positions["facility"].insurance_pool == "pool-b"
    assert ledger.positions["facility"].principal == 100
    assert_conservation(ledger)


def test_failed_renewal_rolls_back_jubilee_sale_and_all_new_terms():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 100, "owner", credit_limit=100, premium_rate=0.02)
    ledger.advance(10)
    ledger.deploy(ledger.jubilee)
    before = copy.deepcopy(ledger.__dict__)
    with pytest.raises(ValueError, match="worsen the issued-credit shortfall"):
        ledger.renew_unspent("facility", "owner", new_limit=110,
                             new_premium_rate=0.01, additional_issuance=50)
    assert ledger.__dict__ == before
    assert_conservation(ledger)


def test_k_cut_does_not_call_credit_back_and_renewal_cannot_worsen_shortfall():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 100, "owner", credit_limit=100)
    ledger.reappraise_limit("facility", 80)
    assert ledger.positions["facility"].principal == 100
    assert ledger.unspent["owner"] == 100
    result = ledger.renew_unspent("facility", "owner", new_limit=90, new_premium_rate=0)
    assert result.new_principal == 100
    assert ledger.positions["facility"].credit_limit == 90
    before = copy.deepcopy(ledger.__dict__)
    with pytest.raises(ValueError, match="worsen the issued-credit shortfall"):
        ledger.renew_unspent("facility", "owner", new_limit=80,
                             new_premium_rate=0, additional_issuance=1)
    assert ledger.__dict__ == before
    assert_conservation(ledger)


def test_insurer_switch_refund_failure_rolls_back_without_cross_pool_use():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("a", "pool-a")
    ledger.register_insurer_pool("b", "pool-b")
    ledger.issue_unspent("facility", 100, "owner", credit_limit=100,
                         insurer="a", premium_deposit=10)
    ledger.transfer("pool-a", "outsider", 10)
    before = copy.deepcopy(ledger.__dict__)
    with pytest.raises(InsufficientFunds, match="pool-a"):
        ledger.renew_unspent("facility", "owner", new_limit=100,
                             new_premium_rate=0.01, new_insurer="b",
                             new_insurance_principal=8)
    assert ledger.__dict__ == before
    assert ledger.accounts["pool-b"] == 0
    assert_conservation(ledger)


@pytest.mark.parametrize("years", [1, 25, 49, 50, 51, 100])
def test_idle_wallet_fund_and_linear_relief(years):
    ledger = opened()
    ledger.advance(years)
    holder = 100 * math.exp(-0.02 * years)
    fund = 100 - holder
    gross = 100 * 0.02 * years
    assert ledger.accounts["holder"] == pytest.approx(holder)
    assert ledger.jubilee == pytest.approx(fund)
    assert ledger.gross_demurrage == pytest.approx(gross)
    assert ledger.from_holders == pytest.approx(fund)
    assert ledger.recycled_by_jubilee == pytest.approx(gross - fund)
    assert ledger.quote_retirement("mortgage", 100).relief == pytest.approx(
        min(100, gross)
    )
    assert_conservation(ledger)


def test_fifty_year_relief_is_not_all_cash_in_fund():
    ledger = opened()
    ledger.advance(50)
    quote = ledger.quote_retirement("mortgage", 100)
    assert ledger.accounts["holder"] == pytest.approx(36.7879441171)
    assert ledger.jubilee == pytest.approx(63.2120558829)
    assert ledger.jubilee_nominal == pytest.approx(100)
    assert ledger.jubilee_carried_fee == pytest.approx(36.7879441171)
    assert ledger.gross_demurrage == pytest.approx(100)
    assert ledger.recycled_by_jubilee == pytest.approx(36.7879441171)
    assert quote.relief == pytest.approx(100)
    assert quote.account_payment == 0
    assert quote.fund_shortfall == pytest.approx(36.7879441171)
    assert quote.fund_nominal_required is None
    before = snapshot(ledger, "mortgage", "holder")
    with pytest.raises(InsufficientFunds, match="Jubilee lacks"):
        ledger.retire("mortgage", 100, "holder")
    assert snapshot(ledger, "mortgage", "holder") == before


def test_external_acquisition_funds_retirement_without_creating_buck():
    ledger = opened()
    ledger.advance(50)
    # A purchase or fund return must transfer BUCK from an actual counterparty.
    # The non-BUCK consideration is outside this closed BUCK model.
    ledger.transfer("holder", JUBILEE, ledger.accounts["holder"])
    assert ledger.jubilee == pytest.approx(100)
    before_fee = ledger.jubilee_carried_fee
    before_nominal = ledger.jubilee_nominal
    quote = ledger.retire("mortgage", 100, "holder")
    assert quote.relief == pytest.approx(100)
    assert quote.fund_nominal_required == pytest.approx(before_nominal)
    assert quote.fund_carried_fee == pytest.approx(before_fee)
    assert ledger.jubilee_nominal_sent == pytest.approx(before_nominal)
    assert ledger.jubilee_carried_fee == 0
    assert ledger.issued == pytest.approx(0)
    assert_conservation(ledger)


def test_all_issued_buck_in_jubilee_only_pays_itself():
    ledger = DemurrageLedger()
    ledger.issue("credit", 100, JUBILEE)
    ledger.advance(50)
    assert ledger.jubilee == pytest.approx(100)
    assert ledger.from_holders == 0
    assert ledger.gross_demurrage == pytest.approx(100)
    assert ledger.recycled_by_jubilee == pytest.approx(100)
    assert ledger.quote_retirement("credit", 100).fund_shortfall == 0
    assert ledger.jubilee_nominal == pytest.approx(200)
    quote = ledger.retire("credit", 100, "borrower")
    assert quote.fund_nominal_required == pytest.approx(200)
    assert quote.fund_carried_fee == pytest.approx(100)
    assert ledger.jubilee_nominal == 0
    assert_conservation(ledger)


def test_fund_strategy_realizes_sourced_return_and_liquidates_for_relief():
    ledger = opened()
    ledger.advance(10)
    ledger.deploy(ledger.jubilee)
    invested = ledger.accounts[JUBILEE_INVESTED]
    assert ledger.jubilee == 0
    ledger.strategy_return("holder", 2)
    assert ledger.accounts[JUBILEE_INVESTED] == pytest.approx(invested + 2)
    assert_conservation(ledger)

    quote = ledger.retire("mortgage", 50, "holder")
    assert quote.relief == pytest.approx(10)
    assert quote.fund_cash == 0
    assert quote.fund_invested == pytest.approx(invested + 2)
    assert ledger.jubilee == pytest.approx(0)
    assert ledger.accounts[JUBILEE_INVESTED] == pytest.approx(invested - 8)
    assert_conservation(ledger)


def test_cash_counters_and_average_basis_realize_gains_only_on_withdrawal():
    ledger = opened()
    ledger.advance(10)
    ledger.deploy(10)
    ledger.strategy_return("holder", 2)
    assert ledger.fund_invested == 10
    assert ledger.fund_withdrawn == 0
    assert ledger.strategy_open_basis == 10
    assert ledger.strategy_realized_gain == 0
    units, proceeds = ledger.redeem_strategy(0.5)
    assert units == 5
    assert proceeds == 6
    assert ledger.fund_withdrawn == 6
    assert ledger.strategy_open_basis == 5
    assert ledger.strategy_realized_gain == 1
    assert ledger.strategy_realized_loss == 0
    assert_conservation(ledger)


def test_repeated_deployments_share_one_pooled_position_and_average_basis():
    ledger = opened()
    ledger.advance(10)
    ledger.deploy(10)
    ledger.strategy_return("holder", 2)
    ledger.deploy(6)  # 6 BUCK buys 5 units at the new 1.2 BUCK mark.
    assert ledger.strategy_units == pytest.approx(15)
    assert ledger.strategy_open_basis == pytest.approx(16)
    assert ledger.accounts[JUBILEE_INVESTED] == pytest.approx(18)

    units, proceeds = ledger.redeem_strategy(1 / 3)
    assert units == pytest.approx(5)
    assert proceeds == pytest.approx(6)
    assert ledger.strategy_units == pytest.approx(10)
    assert ledger.strategy_open_basis == pytest.approx(32 / 3)
    assert ledger.strategy_realized_gain == pytest.approx(2 / 3)
    assert_conservation(ledger)


def test_partial_relief_is_delivered_at_full_value_after_carried_fee():
    ledger = opened()
    ledger.advance(25)
    assert ledger.jubilee_nominal == pytest.approx(50)
    net_before = ledger.jubilee
    fee_before = ledger.jubilee_carried_fee
    holder_before = ledger.accounts["holder"]
    quote = ledger.quote_retirement("mortgage", 50)
    assert quote.relief == pytest.approx(25)
    assert quote.fund_nominal_required == pytest.approx(25 * 50 / net_before)
    assert quote.fund_carried_fee == pytest.approx(quote.fund_nominal_required - 25)

    paid = ledger.retire("mortgage", 50, "holder")
    assert paid == quote
    assert ledger.jubilee_nominal_sent == pytest.approx(quote.fund_nominal_required)
    assert ledger.jubilee_carried_fee == pytest.approx(fee_before - quote.fund_carried_fee)
    assert ledger.jubilee == pytest.approx(net_before - 25)
    assert ledger.accounts["holder"] == pytest.approx(holder_before - 25)
    assert ledger.positions["mortgage"].principal == pytest.approx(50)
    assert_conservation(ledger)


def test_incoming_strategy_buck_dilutes_old_fund_fee_before_relief():
    ledger = opened()
    ledger.advance(10)
    ledger.deploy(ledger.jubilee)
    ledger.advance(1)
    quote = ledger.quote_retirement("mortgage", 50)
    assert quote.relief > ledger.jubilee
    assert quote.fund_shortfall == 0
    cash_at_payment = quote.relief
    assert quote.fund_nominal_required == pytest.approx(
        cash_at_payment + ledger.jubilee_carried_fee
    )
    nominal_before = ledger.jubilee_nominal_sent
    ledger.retire("mortgage", 50, "holder")
    assert ledger.jubilee_nominal_sent - nominal_before == pytest.approx(
        quote.fund_nominal_required
    )
    assert_conservation(ledger)


def test_available_relief_redeems_before_computing_payout():
    ledger = opened()
    ledger.advance(10)
    ledger.deploy(ledger.jubilee)
    ledger.advance(1)
    units_before = ledger.strategy_units
    invested_before = ledger.accounts[JUBILEE_INVESTED]
    cash_before = ledger.jubilee
    nominal_before = ledger.jubilee_nominal_sent
    quote = ledger.quote_available_retirement("mortgage", 50, 0.5)
    assert quote.redemption_proceeds == pytest.approx(invested_before * 0.5)
    assert quote.fund_net_after_redemption == pytest.approx(
        cash_before + quote.redemption_proceeds
    )
    assert quote.relief <= quote.fund_net_after_redemption
    assert ledger.strategy_units == units_before, "preview does not redeem"

    paid = ledger.retire_available("mortgage", 50, "holder", 0.5)
    assert paid == quote
    assert ledger.strategy_units == pytest.approx(units_before / 2)
    assert ledger.jubilee_nominal_sent - nominal_before == pytest.approx(
        quote.fund_nominal_required
    )
    assert quote.fund_nominal_required - quote.fund_carried_fee == pytest.approx(
        quote.relief
    )
    assert quote.account_payment + quote.relief == pytest.approx(50)
    assert_conservation(ledger)


def test_redemption_bases_differ_when_credit_ages_differ():
    ledger = opened()
    ledger.advance(40)
    ledger.deploy(ledger.jubilee)
    ledger.issue("new-credit", 99_900, "holder")
    by_issuance = ledger.redemption_fraction("mortgage", 100, "issuance")
    by_relief = ledger.redemption_fraction("mortgage", 100, "relief")
    assert by_issuance == pytest.approx(0.001)
    assert by_relief == pytest.approx(1)
    small = ledger.quote_available_retirement("mortgage", 100, by_issuance)
    large = ledger.quote_available_retirement("mortgage", 100, by_relief)
    assert ledger.quote_available_retirement("mortgage", 100) == large
    assert small.relief == pytest.approx(small.redemption_proceeds)
    assert large.relief == pytest.approx(large.redemption_proceeds)
    assert large.relief > 100 * small.relief
    assert_conservation(ledger)


@pytest.mark.parametrize("multiplier", [1.025, 0.95])
def test_post_redemption_cash_controls_the_effective_relief_rate(multiplier):
    ledger = opened()
    ledger.advance(25)
    # A 10% reserve on the 50 BUCK benchmark, observed after a 1% sale.
    fraction = 0.01
    target_cash = 0.1 * ledger.aggregate_relief * multiplier
    pre_cash = (target_cash - fraction * ledger.jubilee) / (1 - fraction)
    ledger.deploy(ledger.jubilee - pre_cash)
    result = ledger.retire_available("mortgage", 10, "holder", fraction)
    assert result.fund_net_after_redemption == pytest.approx(target_cash)
    assert result.rate_multiplier == pytest.approx(multiplier)
    assert result.baseline_relief == pytest.approx(5)
    assert result.relief == pytest.approx(5 * multiplier)
    assert result.account_payment == pytest.approx(10 - 5 * multiplier)
    assert_conservation(ledger)


def test_zero_value_redemption_computes_zero_relief_without_fund_failure():
    ledger = opened()
    ledger.advance(10)
    ledger.deploy(ledger.jubilee)
    units_before = ledger.strategy_units
    ledger.strategy_loss("holder", ledger.accounts[JUBILEE_INVESTED])
    quote = ledger.retire_available("mortgage", 50, "holder", 0.25)
    assert quote.redeemed_units == pytest.approx(units_before * 0.25)
    assert quote.redemption_proceeds == 0
    assert quote.fund_net_after_redemption == 0
    assert quote.relief == 0
    assert quote.account_payment == 50
    assert ledger.strategy_units == pytest.approx(units_before * 0.75)
    assert ledger.strategy_realized_loss == pytest.approx(units_before * 0.25)
    assert ledger.fund_withdrawn == 0
    assert ledger.positions["mortgage"].principal == 50
    assert_conservation(ledger)


@pytest.mark.parametrize("payout_policy", ["reserve", "proportional"])
def test_actual_redemption_result_overrides_presale_quote(monkeypatch, payout_policy):
    ledger = opened()
    ledger.advance(10)
    ledger.deploy(ledger.jubilee)
    preview = ledger.quote_available_retirement("mortgage", 50, 0.5, payout_policy=payout_policy)
    assert preview.redemption_proceeds > 0
    redeem = ledger.redeem_strategy

    def loses_value_before_sale(fraction):
        ledger.strategy_loss("holder", ledger.accounts[JUBILEE_INVESTED])
        return redeem(fraction)

    monkeypatch.setattr(ledger, "redeem_strategy", loses_value_before_sale)
    result = ledger.retire_available("mortgage", 50, "holder", 0.5, payout_policy=payout_policy)
    assert result.redemption_proceeds == 0
    assert result.relief == 0
    assert result.account_payment == 50
    assert_conservation(ledger)


def test_zero_redemption_can_still_pay_from_existing_cash():
    ledger = opened()
    ledger.advance(10)
    ledger.deploy(10)
    ledger.strategy_loss("holder", ledger.accounts[JUBILEE_INVESTED])
    cash = ledger.jubilee
    quote = ledger.retire_available("mortgage", 50, "holder", 0.25)
    assert quote.redemption_proceeds == 0
    assert quote.relief == pytest.approx(cash)
    assert quote.account_payment == pytest.approx(50 - cash)
    assert ledger.jubilee == pytest.approx(0)
    assert_conservation(ledger)


def test_debtor_insufficiency_leaves_presale_and_credit_unchanged():
    ledger = opened()
    ledger.advance(10)
    ledger.deploy(ledger.jubilee)
    ledger.strategy_loss("holder", ledger.accounts[JUBILEE_INVESTED])
    before = (snapshot(ledger, "mortgage", "holder"), ledger.strategy_units)
    with pytest.raises(InsufficientFunds, match="borrower lacks"):
        ledger.retire_available("mortgage", 50, "borrower", 0.25)
    assert (snapshot(ledger, "mortgage", "holder"), ledger.strategy_units) == before
    assert_conservation(ledger)


def test_fund_deployment_and_self_demurrage_do_not_create_return():
    ledger = opened()
    ledger.advance(25)
    ledger.deploy(ledger.jubilee)
    with pytest.raises(ValueError, match="external source"):
        ledger.strategy_return(JUBILEE_INVESTED, 1)
    ledger.advance(25)
    assert ledger.jubilee_assets == pytest.approx(100 * (1 - math.exp(-1)))
    assert ledger.recycled_by_jubilee == pytest.approx(100 * math.exp(-1))
    assert ledger.quote_retirement("mortgage", 100).fund_shortfall == pytest.approx(
        100 * math.exp(-1)
    )
    assert_conservation(ledger)


def test_demurrage_on_deployed_buck_reaches_fund_cash():
    ledger = DemurrageLedger()
    ledger.issue("credit", 100, JUBILEE)
    ledger.deploy(80)
    ledger.advance(10)
    investment_fee = 80 * (1 - math.exp(-0.2))
    assert ledger.strategy_demurrage_received == pytest.approx(investment_fee)
    assert ledger.accounts[JUBILEE_INVESTED] == pytest.approx(80 - investment_fee)
    assert ledger.jubilee == pytest.approx(20 + investment_fee)
    assert ledger.fund_demurrage_received == pytest.approx(investment_fee)
    assert ledger.jubilee_assets == pytest.approx(100)
    assert ledger.strategy_demurrage_received + ledger.jubilee_carried_fee == pytest.approx(20)
    assert_conservation(ledger)


def test_global_fund_checkpoint_includes_buck_deployed_outside_fund_cash():
    ledger = opened()
    ledger.transfer("holder", JUBILEE, 5)
    ledger.deploy(4)
    start_net = ledger.jubilee
    start_nominal = ledger.jubilee_nominal
    issued = ledger.issued
    years = 20
    ledger.advance(years)
    expected_net = start_net + (issued - start_net) * (1 - math.exp(-0.02 * years))
    assert ledger.jubilee == pytest.approx(expected_net)
    assert ledger.jubilee_nominal == pytest.approx(start_nominal + issued * 0.02 * years)
    assert_conservation(ledger)


def test_individual_quotes_do_not_guarantee_the_fund_can_pay_everyone():
    ledger = opened()
    ledger.issue("other-credit", 100, "other")
    ledger.advance(50)
    # Each claim fits individually; their sum does not fit together.
    assert ledger.quote_retirement("mortgage", 100).fund_shortfall == 0
    assert ledger.quote_retirement("other-credit", 100).fund_shortfall == 0
    assert ledger.aggregate_relief == pytest.approx(200)
    assert ledger.aggregate_relief - ledger.jubilee_assets == pytest.approx(
        200 * math.exp(-1)
    )
    ledger.retire("mortgage", 100, "holder")
    assert ledger.quote_retirement("other-credit", 100).fund_shortfall == pytest.approx(
        200 * math.exp(-1)
    )
    assert_conservation(ledger)


def test_partial_retirement_reduces_issuance_and_its_age_proportionally():
    ledger = opened()
    ledger.advance(10)
    quote = ledger.retire("mortgage", 50, "holder")
    assert quote.relief == pytest.approx(10)
    assert quote.account_payment == pytest.approx(40)
    assert ledger.positions["mortgage"].principal == pytest.approx(50)
    assert ledger.positions["mortgage"].buck_years == pytest.approx(500)
    assert_conservation(ledger)
    before = ledger.gross_demurrage
    ledger.advance(10)
    assert ledger.gross_demurrage - before == pytest.approx(10)
    assert ledger.quote_retirement("mortgage", 50).relief == pytest.approx(20)
    assert_conservation(ledger)


def test_changing_issuance_preserves_each_positions_buck_years():
    ledger = opened()
    ledger.advance(10)
    ledger.issue("mortgage", 100, "other")
    ledger.issue("new-credit", 50, "other")
    ledger.advance(10)
    assert ledger.positions["mortgage"].buck_years == pytest.approx(3000)
    assert ledger.positions["new-credit"].buck_years == pytest.approx(500)
    assert ledger.quote_retirement("mortgage", 100).relief == pytest.approx(30)
    assert ledger.quote_retirement("new-credit", 50).relief == pytest.approx(10)
    assert ledger.gross_demurrage == pytest.approx(70)
    assert_conservation(ledger)


def test_small_net_holding_charges_only_its_remaining_balance():
    ledger = opened()
    ledger.advance(25)
    ledger.transfer("holder", "other", ledger.accounts["holder"] - 0.5)
    ledger.advance(1)
    assert ledger.accounts["holder"] == pytest.approx(0.5 * math.exp(-0.02))
    assert 0.5 - ledger.accounts["holder"] == pytest.approx(0.0099006633466)
    assert_conservation(ledger)


def test_fresh_payment_into_a_small_net_balance_is_not_eaten_by_old_fees():
    ledger = opened()
    ledger.advance(25)
    ledger.transfer("holder", "other", ledger.accounts["holder"] - 0.5)
    ledger.advance(1)
    prior = ledger.accounts["holder"]
    ledger.transfer("other", "holder", 10)
    assert ledger.accounts["holder"] == pytest.approx(prior + 10)
    ledger.advance(1)
    assert ledger.accounts["holder"] == pytest.approx((prior + 10) * math.exp(-0.02))
    assert_conservation(ledger)


def test_touch_frequency_does_not_change_economics():
    once = opened()
    many = opened()
    once.advance(50)
    for _ in range(600):
        many.advance(1 / 12)
    assert many.accounts["holder"] == pytest.approx(once.accounts["holder"], abs=1e-10)
    assert many.jubilee == pytest.approx(once.jubilee, abs=1e-10)
    assert many.gross_demurrage == pytest.approx(once.gross_demurrage, abs=1e-10)
    assert many.jubilee_carried_fee == pytest.approx(once.jubilee_carried_fee, abs=1e-10)
    assert many.jubilee_nominal == pytest.approx(once.jubilee_nominal, abs=1e-10)
    assert many.quote_retirement("mortgage", 100).relief == pytest.approx(100, abs=1e-10)
    assert_conservation(many)


def test_splitting_a_carrying_payment_preserves_net_value_and_fee():
    one = opened()
    two = opened()
    one.advance(25)
    two.advance(25)
    one.transfer_full_value(JUBILEE, "recipient", 10)
    two.transfer_full_value(JUBILEE, "recipient", 5)
    two.transfer_full_value(JUBILEE, "recipient", 5)
    assert two.accounts["recipient"] == pytest.approx(10)
    assert one.accounts["recipient"] == pytest.approx(10)
    assert two.jubilee == pytest.approx(one.jubilee)
    assert two.jubilee_carried_fee == pytest.approx(one.jubilee_carried_fee)
    assert two.jubilee_nominal_sent == pytest.approx(one.jubilee_nominal_sent)
    assert_conservation(one)
    assert_conservation(two)


def test_carrying_erc20_transfer_is_nominal_not_a_net_promise():
    ledger = DemurrageLedger()
    ledger.issue("credit", 100, JUBILEE)
    ledger.advance(50)
    assert ledger.balance_of(JUBILEE) == pytest.approx(200)
    ledger.transfer(JUBILEE, "recipient", 100)
    assert ledger.accounts["recipient"] == pytest.approx(50)
    assert ledger.balance_of(JUBILEE) == pytest.approx(100)
    assert ledger.jubilee == pytest.approx(50)
    assert_conservation(ledger)


def test_integer_carrying_gross_is_exact_and_minimal():
    # Independently enumerate every possible transfer for small balances.
    for gross in range(1, 36):
        for fee in range(gross + 1):
            available = gross - fee
            assert carrying_gross_for_net_units(0, gross, fee) == 0
            for net in range(1, available + 1):
                sent = carrying_gross_for_net_units(net, gross, fee)
                delivered = sent - fee * sent // gross
                previous = sent - 1 - fee * (sent - 1) // gross
                assert delivered == net
                assert previous < net
                assert sent <= gross
            with pytest.raises(InsufficientFunds):
                carrying_gross_for_net_units(available + 1, gross, fee)


def test_integer_fee_rounding_can_reduce_nominal_payment_by_one_unit():
    gross = 100_000_000
    fee = 20_000_000
    desired_net = 40_000_000
    sent = carrying_gross_for_net_units(desired_net, gross, fee)
    assert sent == 49_999_999
    assert sent - fee * sent // gross == desired_net


@pytest.mark.parametrize("args", [(1, 0, 0), (1, 10, 10)])
def test_integer_carrying_cannot_pay_from_exhausted_balance(args):
    with pytest.raises(InsufficientFunds):
        carrying_gross_for_net_units(*args)


@pytest.mark.parametrize("years", [0.000001, 1, 49, 50, 100, 1000])
def test_decay_against_high_precision_decimal(years):
    ledger = opened()
    ledger.advance(years)
    with localcontext() as ctx:
        ctx.prec = 60
        expected = Decimal(100) * (-Decimal("0.02") * Decimal(str(years))).exp()
    assert ledger.accounts["holder"] == pytest.approx(float(expected), rel=1e-13)
    assert_conservation(ledger)


def test_early_retirement_does_not_invent_account_or_fund_money():
    ledger = opened()
    ledger.advance(10)
    ledger.transfer("holder", "elsewhere", ledger.accounts["holder"])
    before = snapshot(ledger, "mortgage", "holder")
    with pytest.raises(InsufficientFunds, match="holder lacks"):
        ledger.retire("mortgage", 50, "holder")
    assert snapshot(ledger, "mortgage", "holder") == before
    assert_conservation(ledger)


def test_invalid_inputs_and_own_transfer():
    ledger = opened()
    with pytest.raises(ValueError):
        ledger.advance(float("nan"))
    with pytest.raises(ValueError):
        ledger.issue("mortgage", -1, "holder")
    with pytest.raises(ValueError):
        ledger.quote_retirement("mortgage", 101)
    with pytest.raises(InsufficientFunds):
        ledger.transfer("holder", JUBILEE, 101)
    ledger.transfer("holder", "holder", 10)
    assert ledger.accounts["holder"] == 100
    assert_conservation(ledger)


def test_cli_reports_underfunded_and_acquired_cases(capsys):
    assert main(["--years", "50"]) == 0
    output = capsys.readouterr().out
    assert "63.212056" in output and "36.787944" in output
    assert "fund-to-itself" in output
    assert main(["--years", "50", "--acquire-all"]) == 0
    output = capsys.readouterr().out
    assert "aggregate baseline shortfall: 0.000000" in output
    assert main(["--years", "50", "--deploy-fund", "--strategy-return", "10"]) == 0
    output = capsys.readouterr().out
    assert "Strategy return: 10.000000" in output
    assert "aggregate baseline shortfall: 26.787944" in output
    assert main(["--years", "50", "--deploy-fund", "--wipe-strategy",
                 "--retire", "50", "--redeem-fraction", "0.25"]) == 0
    output = capsys.readouterr().out
    assert "Sold 25.00% of strategy units for 0.000000 BUCK" in output
    assert "relief 0.000000, debtor payment 50.000000" in output
    assert main(["--years", "50", "--deploy-fund", "--wipe-strategy",
                 "--retire", "50"]) == 0
    output = capsys.readouterr().out
    assert "Sold 50.00% of strategy units for 0.000000 BUCK" in output
    assert main(["--years", "50", "--deploy-fund", "--wipe-strategy",
                 "--retire", "100", "--redeem-fraction", "1"]) == 0
    output = capsys.readouterr().out
    assert "relief 0.000000, debtor payment 100.000000" in output
