"""Maturity and deferred-fund obligations in the independent reference model."""

import math

import pytest

from alberta_buck.sim.demurrage_reference import DemurrageLedger, InsufficientFunds, JUBILEE


def assert_supply_conserved(ledger):
    assert sum(ledger.accounts.values()) + sum(ledger.unspent.values()) == pytest.approx(ledger.issued)
    assert ledger.jubilee == pytest.approx(
        ledger.fund_demurrage_received + ledger.fund_withdrawn + ledger.fund_other_in
        - ledger.fund_relief_paid - ledger.fund_invested - ledger.fund_other_out
    )


def test_matured_lien_closes_after_owner_spends_buck_and_jubilee_debt_remains():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 1_000_000, "owner")
    ledger.spend_unspent("owner", "merchant", 1_000_000)
    ledger.advance(50)
    cash = ledger.jubilee
    assert cash == pytest.approx(1_000_000 * (1 - math.exp(-1)))
    assert ledger.balance_of("owner") == 0
    assert ledger.accounts["merchant"] == pytest.approx(1_000_000 * math.exp(-1))

    debt, refund = ledger.close_matured("facility", "owner")
    assert (debt, refund) == (1_000_000, 0)
    assert ledger.balance_of("owner") == 0
    assert "facility" not in ledger.positions
    assert ledger.issued == 1_000_000
    assert ledger.deferred_jubilee_principal == 1_000_000
    assert ledger.aggregate_relief == 1_000_000
    assert ledger.jubilee == cash
    assert_supply_conserved(ledger)

    with pytest.raises(InsufficientFunds, match="10%"):
        ledger.burn_deferred_jubilee(cash * 0.10001)
    ledger.burn_deferred_jubilee(cash * 0.1)
    assert ledger.issued == pytest.approx(1_000_000 - cash * 0.1)
    assert ledger.deferred_jubilee_principal == pytest.approx(ledger.issued)
    assert ledger.jubilee == pytest.approx(cash * 0.9)
    assert_supply_conserved(ledger)
    ledger.advance(1)
    assert ledger.jubilee > cash * 0.9
    assert_supply_conserved(ledger)


def test_maturity_uses_pooled_average_issuance_age():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 100, "owner")
    ledger.advance(49)
    ledger.issue_unspent("facility", 100, "owner")
    ledger.advance(25.49)
    with pytest.raises(ValueError, match="average issuance age"):
        ledger.close_matured("facility", "owner")
    ledger.advance(0.01)
    with pytest.raises(ValueError, match="different owner"):
        ledger.close_matured("facility", "someone else")
    assert ledger.close_matured("facility", "owner")[0] == 200
    assert ledger.balance_of("owner") > 0
    with pytest.raises(ValueError, match="closed facility"):
        ledger.issue_unspent("facility", 1, "owner")
    assert_supply_conserved(ledger)


def test_maturity_refunds_the_insurer_nominal_deposit_with_its_carried_fee():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer", "pool")
    ledger.issue_unspent("facility", 100, "owner", insurer="insurer", premium_deposit=10)
    ledger.spend_unspent("owner", "merchant", 90)
    ledger.advance(50)
    debt, refund = ledger.close_matured("facility", "owner")
    assert debt == 100
    assert refund == pytest.approx(10 * math.exp(-1))
    assert ledger.balance_of("owner") == pytest.approx(refund)
    assert ledger.insurance_nominal_sent["pool"] == pytest.approx(10)
    assert not ledger.pending_insurance_refunds
    assert_supply_conserved(ledger)


def test_illiquid_insurer_does_not_block_release_and_still_owes_original_refund():
    ledger = DemurrageLedger()
    ledger.register_insurer_pool("insurer", "pool")
    ledger.register_insurer_pool("other insurer", "other pool")
    ledger.issue_unspent("facility", 100, "owner", insurer="insurer", premium_deposit=10)
    ledger.spend_unspent("owner", "merchant", 90)
    ledger.transfer("pool", "merchant", 10)
    ledger.advance(50)
    assert ledger.close_matured("facility", "owner") == (100, 0)
    assert ledger.pending_insurance_refunds["facility"] == ("owner", "pool", 10)
    with pytest.raises(InsufficientFunds):
        ledger.claim_matured_insurance_refund("facility", "owner")
    assert "facility" in ledger.pending_insurance_refunds
    ledger.transfer("merchant", "pool", 10)
    assert ledger.claim_matured_insurance_refund("facility", "owner") == pytest.approx(10)
    assert ledger.balance_of("other pool") == 0
    assert not ledger.pending_insurance_refunds
    assert_supply_conserved(ledger)


def test_maturity_does_not_automatically_sell_fund_positions():
    ledger = DemurrageLedger()
    ledger.issue_unspent("facility", 1000, "owner")
    ledger.spend_unspent("owner", "merchant", 1000)
    ledger.advance(1)
    ledger.deploy(10)
    ledger.advance(49)
    units, invested, cash = ledger.strategy_units, ledger.accounts["Jubilee invested"], ledger.jubilee
    ledger.close_matured("facility", "owner")
    assert (ledger.strategy_units, ledger.accounts["Jubilee invested"], ledger.jubilee) == (units, invested, cash)
    ledger.redeem_strategy(0.5)
    assert ledger.jubilee > cash
    assert ledger.balance_of("owner") == 0
    assert_supply_conserved(ledger)
