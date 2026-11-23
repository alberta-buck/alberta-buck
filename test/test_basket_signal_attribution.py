"""Offline regression for decision-to-fill attribution."""

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/attribute-basket-signals.py"
SPEC = importlib.util.spec_from_file_location("attribute_basket_signals", SCRIPT)
ATTRIBUTION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ATTRIBUTION)


def decision(token, ask, rate, plan, no_pair, book=0):
    return dict(tx="receipt", token=token, tick=0, index=token, gap=-0.05,
                eligible=ask > 0, leash=False, pairAmount=ask, pairRate=rate, bookAmount=book,
                plannedBuy=plan, planWithoutPairBuy=no_pair, plannedSell=0, planWithoutPairSell=0)


def fill(token, amount, *, tx="receipt"):
    return dict(tx=tx, token=token, tick=0, index=10 + token, side="buy", amount=amount)


class BasketSignalAttributionTest(unittest.TestCase):
    def test_keeps_current_banked_indirect_and_book_only_orders_distinct(self):
        f = {"decisions": [
            decision(0, 20, 1, 20, 10),
            decision(1, 10, 0, 10, 0),
            decision(2, 0, 0, 3, 0),
            decision(3, 0, 0, 8, 8, book=8),
            decision(4, 7, 1, 10, 10, book=10),
            decision(5, 5, 1, 0, 0),
            decision(6, 0, 0, 1, 2),
        ], "trades": [fill(0, 9), fill(1, 10), fill(2, 3), fill(3, 8), fill(4, 10), fill(6, 1)]}
        counts = ATTRIBUTION.classify([f])
        for key, expected in {
            "eligible": 4, "asks": 4, "banked_asks": 1,
            "current_plans": 1, "banked_plans": 1, "indirect_plans": 1, "changed": 4, "decreased": 1,
            "current_fills": 1, "banked_fills": 1, "indirect_fills": 1, "above_control": 2,
            "covered_by_book": 1, "downstream": 1,
        }.items():
            self.assertEqual(counts[key], expected, key)

    def test_rejects_unmatched_and_duplicate_execution(self):
        planned = decision(0, 20, 1, 20, 10)
        with self.assertRaisesRegex(ValueError, "same-transaction"):
            ATTRIBUTION.classify([{"decisions": [planned], "trades": [fill(0, 20, tx="other")]}])
        with self.assertRaisesRegex(ValueError, "duplicate execution"):
            ATTRIBUTION.classify([{"decisions": [planned], "trades": [fill(0, 20), fill(0, 20)]}])


if __name__ == "__main__":
    unittest.main()
