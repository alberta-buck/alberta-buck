"""Compare age-weighted marked relief with the cash and supply constraints."""

import pytest

from alberta_buck.sim.demurrage_reference import DemurrageLedger, JUBILEE


def marked_fund():
    ledger = DemurrageLedger()
    ledger.issue_unspent("old", 99_000_000, "bob")
    # Total capped age will be 500 million BUCK-years: the 99-million
    # facility's age is 499/99 years and the 1-million facility's is one.
    ledger.advance(499 / 99 - 1)
    ledger.issue_unspent("young", 1_000_000, "alice")
    ledger.advance(1)
    assert ledger.aggregate_relief == pytest.approx(10_000_000)
    # Top up from another actual holder so that total marked fund value is
    # exactly 10 million; this transfer does not create BUCK.
    ledger.transfer("bob", JUBILEE, 10_000_000 - ledger.jubilee)
    ledger.deploy(9_000_000)
    return ledger


def test_marked_relief_uses_relative_buck_years_not_supply_share_times_age():
    ledger = marked_fund()
    q = ledger.quote_marked_retirement("young", 1_000_000)
    assert q.baseline_relief == pytest.approx(20_000)
    assert q.aggregate_benchmark == pytest.approx(10_000_000)
    assert q.cash_net == pytest.approx(1_000_000)
    assert q.investment_mark == pytest.approx(9_000_000)
    assert q.benchmark_share == pytest.approx(0.002)
    assert q.effective_annual_rate == pytest.approx(0.02)
    assert q.relief == pytest.approx(20_000)
    assert q.liquid_limit == pytest.approx(100_000)
    assert not q.exceeds_liquid_limit
    naive_double_age = q.fund_mark * (1_000_000 / ledger.issued) / 50
    assert naive_double_age == pytest.approx(2_000)
    assert q.relief == pytest.approx(naive_double_age * 10)


def test_current_mark_yields_205_or_190_percent_target_rates():
    ledger = marked_fund()
    ledger.strategy_return("bob", 250_000 / 0.75)
    gain = ledger.quote_marked_retirement("young", 1_000_000)
    assert gain.fund_mark == pytest.approx(10_250_000)
    assert gain.effective_annual_rate == pytest.approx(0.0205)
    assert gain.relief == pytest.approx(20_500)
    ledger.strategy_loss("bob", 250_000 / 0.75 + 500_000)
    loss = ledger.quote_marked_retirement("young", 1_000_000)
    assert loss.fund_mark == pytest.approx(9_500_000)
    assert loss.effective_annual_rate == pytest.approx(0.019)
    assert loss.relief == pytest.approx(19_000)


def test_quote_does_not_sell_or_silently_clamp_to_the_liquidity_limit():
    ledger = marked_fund()
    ledger.deploy(990_000)
    units = ledger.strategy_units
    q = ledger.quote_marked_retirement("young", 1_000_000)
    assert q.relief == pytest.approx(20_000)
    assert q.cash_net == pytest.approx(10_000)
    assert q.liquid_limit == pytest.approx(1_000)
    assert q.exceeds_liquid_limit
    assert ledger.strategy_units == units
    assert ledger.jubilee == pytest.approx(10_000)
    with pytest.raises(ValueError, match="10%"):
        ledger.retire_marked("young", 1_000_000, "alice")
    assert ledger.strategy_units == units
    assert ledger.positions["young"].principal == 1_000_000


def test_marked_retirement_uses_cash_and_leaves_strategy_shares_untouched():
    ledger = marked_fund()
    quote = ledger.quote_marked_retirement("young", 1_000_000)
    units, mark, cash, supply = ledger.strategy_units, ledger.accounts["Jubilee invested"], ledger.jubilee, ledger.issued
    with pytest.raises(ValueError, match="minimum"):
        ledger.retire_marked("young", 1_000_000, "alice", minimum_relief=quote.relief + 1)
    paid = ledger.retire_marked("young", 1_000_000, "alice", minimum_relief=quote.relief)
    assert paid == quote
    assert ledger.strategy_units == units
    assert ledger.accounts["Jubilee invested"] == mark
    assert ledger.jubilee == pytest.approx(cash - quote.relief)
    assert ledger.issued == pytest.approx(supply - 1_000_000)
    assert sum(ledger.accounts.values()) + sum(ledger.unspent.values()) == pytest.approx(ledger.issued)


def test_marked_renewal_does_not_sell_and_respects_the_fund_cash_limit():
    ledger = marked_fund()
    quote = ledger.quote_marked_retirement("young", 1_000_000)
    units = ledger.strategy_units
    result = ledger.renew_unspent("young", "alice", new_limit=2_000_000,
                                  new_premium_rate=0, additional_issuance=100_000,
                                  minimum_relief=quote.relief)
    assert result.relief == pytest.approx(quote.relief)
    assert result.new_principal == pytest.approx(1_100_000 - quote.relief)
    assert result.redemption_proceeds == 0
    assert ledger.strategy_units == units
    assert sum(ledger.accounts.values()) + sum(ledger.unspent.values()) == pytest.approx(ledger.issued)


def test_marked_renewal_cannot_exceed_liquidity_or_renew_a_matured_facility():
    ledger = marked_fund()
    ledger.deploy(990_000)
    with pytest.raises(ValueError, match="10%"):
        ledger.renew_unspent("young", "alice", new_limit=2_000_000,
                             new_premium_rate=0, payout_policy="marked")
    ledger.advance(49)
    with pytest.raises(ValueError, match="matured facility"):
        ledger.renew_unspent("young", "alice", new_limit=2_000_000,
                             new_premium_rate=0, payout_policy="marked")


def test_no_accrued_claim_means_no_relief_even_with_fund_assets():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 100, "owner")
    ledger.spend_unspent("owner", JUBILEE, 10)
    q = ledger.quote_marked_retirement("facility", 100)
    assert q.aggregate_benchmark == 0
    assert q.fund_mark == 10
    assert q.relief == 0
    assert q.multiplier is None
    assert not q.exceeds_liquid_limit


def test_relief_never_exceeds_retired_principal_even_when_mark_is_high():
    ledger = DemurrageLedger()
    ledger.issue_unspent("old", 1_000_000, "alice")
    ledger.advance(49.99)
    ledger.issue_unspent("young", 99_000_000, "bob")
    ledger.advance(0.01)
    ledger.transfer("bob", JUBILEE, 90_000_000)
    ledger.deploy(ledger.jubilee - 1_000_000)
    q = ledger.quote_marked_retirement("old", 1_000_000)
    assert q.effective_annual_rate > 0.02
    assert q.benchmark_share * q.fund_mark > q.principal
    assert q.relief == q.principal
    assert q.exceeds_liquid_limit
