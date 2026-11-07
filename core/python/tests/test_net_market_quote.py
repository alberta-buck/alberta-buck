"""Cross-language full-width/rounding vectors for household market estimates."""
import json
from pathlib import Path
from dataclasses import replace

import pytest

from buck_core import NetMarketQuote

CASES = json.loads((Path(__file__).resolve().parents[3] / "test/fixtures/net-market-quote.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_net_market_estimates_preserve_carried_fees_and_full_width(case):
    q = NetMarketQuote(**{key: int(value) for key, value in case["quote"].items()})
    assert q.sell_expected(int(case["amount"])) == int(case["sale"])
    assert q.buy_expected(int(case["amount"])) == int(case["purchase"])
    assert q.buy_cost(int(case["target"])) == (int(case["cost"]) if case["cost"] is not None else None)
    assert q.buy_cost(0) == q.buy_expected(0) == q.sell_expected(0) == 0


def test_invalid_quotes_and_amounts_do_not_become_zero_money():
    q = NetMarketQuote(**{key: int(value) for key, value in CASES[0]["quote"].items()})
    for fields in ({"net_scaled": q.gross_scaled + 1}, {"gross_scaled": 0}, {"fee_ppm": 10**6},
                   {"reserve_buck": 0}, {"reserve_usdc": 2**256}):
        with pytest.raises(ValueError, match="quote"):
            replace(q, **fields)
    for amount in (-1, 2**256, True, 1.5):
        for method in (q.buy_cost, q.buy_expected, q.sell_expected):
            with pytest.raises(ValueError, match="amount"):
                method(amount)
