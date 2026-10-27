"""Economic specification of net-holder demurrage and linear credit relief."""
import math
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
    assert sum(ledger.accounts.values()) == pytest.approx(ledger.issued, abs=1e-9)
    assert ledger.gross_demurrage == pytest.approx(
        ledger.from_holders + ledger.recycled_by_jubilee, abs=1e-9
    )
    assert ledger.jubilee_fee_created - ledger.jubilee_fee_realized == pytest.approx(
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


def test_actual_redemption_result_overrides_presale_quote(monkeypatch):
    ledger = opened()
    ledger.advance(10)
    ledger.deploy(ledger.jubilee)
    preview = ledger.quote_available_retirement("mortgage", 50, 0.5)
    assert preview.redemption_proceeds > 0
    redeem = ledger.redeem_strategy

    def loses_value_before_sale(fraction):
        ledger.strategy_loss("holder", ledger.accounts[JUBILEE_INVESTED])
        return redeem(fraction)

    monkeypatch.setattr(ledger, "redeem_strategy", loses_value_before_sale)
    result = ledger.retire_available("mortgage", 50, "holder", 0.5)
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
    one.transfer(JUBILEE, "recipient", 10)
    two.transfer(JUBILEE, "recipient", 5)
    two.transfer(JUBILEE, "recipient", 5)
    assert two.accounts["recipient"] == pytest.approx(10)
    assert one.accounts["recipient"] == pytest.approx(10)
    assert two.jubilee == pytest.approx(one.jubilee)
    assert two.jubilee_carried_fee == pytest.approx(one.jubilee_carried_fee)
    assert two.jubilee_nominal_sent == pytest.approx(one.jubilee_nominal_sent)
    assert_conservation(one)
    assert_conservation(two)


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
