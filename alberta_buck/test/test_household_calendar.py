"""Independent dated-lender vectors, shared with the JS household."""
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from alberta_buck.sim.household_calendar import MortgageBook

VECTORS = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/household-calendar.json").read_text())


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_dated_mortgage_matches_independent_fraction_oracle(case):
    book = MortgageBook(int(case["principal"]), int(case["apr_ppb"]), case["start_day"])
    for step in case["steps"]:
        if "advance" in step:
            book.advance(step["advance"])
        if "pay" in step:
            p = step["pay"]
            assert book.record_payment(p["key"], p["day"], int(p["amount"])) == step.get("changed", True)
        before = book.state
        state = book.at(step["at"]) if "at" in step else book.state
        assert book.state == before
        expected = {k: v if k == "day" else int(v) for k, v in step["expected"].items()}
        assert asdict(state) == expected
        assert state.balance == book.principal + state.interest - state.paid


def test_recovery_uses_payment_day_not_discovery_day():
    timely, late, shadow = [MortgageBook(400_000_000_000, 60_000_000) for _ in range(3)]
    timely.advance(10)
    timely.record_payment("bank", 10, 100_000_000_000)
    for b in (timely, late, shadow):
        b.advance(90)
    overstated = late.state.interest
    late.record_payment("bank", 10, 100_000_000_000)
    assert late.state == timely.state
    assert late.state.interest < overstated == shadow.state.interest
    assert shadow.state.paid == 0 and shadow.state.balance > late.state.balance
    # Observation cadence cannot change the bank's interest convention.
    stepped = MortgageBook(400_000_000_000, 60_000_000)
    for day in range(1, 91):
        stepped.advance(day)
    assert stepped.state == shadow.state


def test_conflicting_or_excess_payments_do_not_rewrite_history():
    book = MortgageBook(100, 0)
    book.advance(10)
    assert book.record_payment("later", 10, 90)
    before = book.state, book.payments
    for key, day, amount, error in (("later", 10, 89, "allocation changed"),
                                     ("early", 0, 20, "effective debt"),
                                     ("future", 11, 1, "invalid"),
                                     ("zero", 10, 0, "invalid")):
        with pytest.raises(ValueError, match=error):
            book.record_payment(key, day, amount)
        assert (book.state, book.payments) == before
    assert not book.record_payment("later", 10, 90)
    detached = book.payments
    detached["later"] = (0, 1)
    assert book.payments == before[1]
    with pytest.raises(AttributeError):
        book.state = None


def test_invalid_domains_and_overflow_preserve_the_clock():
    for args in ((True, 0), (-1, 0), (2**256, 0), (1, -1), (1, 0, .5)):
        with pytest.raises(ValueError):
            MortgageBook(*args)
    book = MortgageBook(2**256 - 1, 1)
    before = book.state
    with pytest.raises(ValueError, match="overflows"):
        book.advance(1)
    assert book.state == before
    for day in (-1, .5, True, 2**53):
        with pytest.raises(ValueError):
            book.advance(day)
    book = MortgageBook(100, 0, 5)
    book.advance(10)
    with pytest.raises(ValueError, match="backwards"):
        book.advance(9)
    with pytest.raises(ValueError, match="unavailable"):
        book.at(4)
