"""Economic specification of net-holder demurrage and linear credit relief."""
import math
from decimal import Decimal, localcontext

import pytest

from alberta_buck.sim.demurrage_reference import (
    JUBILEE, JUBILEE_INVESTED, DemurrageLedger, InsufficientFunds, main, snapshot,
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
    assert ledger.gross_demurrage == pytest.approx(100)
    assert ledger.recycled_by_jubilee == pytest.approx(36.7879441171)
    assert quote.relief == pytest.approx(100)
    assert quote.account_payment == 0
    assert quote.fund_shortfall == pytest.approx(36.7879441171)
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
    quote = ledger.retire("mortgage", 100, "holder")
    assert quote.relief == pytest.approx(100)
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
    ledger.retire("credit", 100, "borrower")
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
    assert many.quote_retirement("mortgage", 100).relief == pytest.approx(100, abs=1e-10)
    assert_conservation(many)


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
    assert "retirement shortfall: 0.000000" in output
    assert main(["--years", "50", "--deploy-fund", "--strategy-return", "10"]) == 0
    output = capsys.readouterr().out
    assert "Strategy return: 10.000000" in output
    assert "retirement shortfall: 26.787944" in output
