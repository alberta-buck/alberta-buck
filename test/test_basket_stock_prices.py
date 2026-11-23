"""Offline regression for the funded stock study's stress price tapes."""

import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace
import unittest


path = Path(__file__).resolve().parents[1] / "scripts/run-basket-stock-grid.py"
spec = importlib.util.spec_from_file_location("basket_stock_grid", path)
stock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stock)
signal = stock.script("run-basket-signal-grid")


class StockPricesTest(unittest.TestCase):
    def setUp(self):
        self.days = 120
        self.scenario = SimpleNamespace(tokens=[("LABR",), ("CNST",), ("PAXG",)],
                                        prices=SimpleNamespace(series=[[1_000_000] * self.days,
                                                                       [2_000_000] * self.days,
                                                                       [3_000_000] * self.days]))

    def prices(self, regime, noise=0., seed=41404):
        return stock.stock_prices(signal, self.scenario, regime, self.days, seed, noise, .03)

    def test_isolated_shocks_match_until_the_temporary_recovery(self):
        temp, perm = self.prices("shock-temporary"), self.prices("shock-permanent")
        self.assertEqual(temp[0], [1_000_000] * 40 + [800_000] * 40 + [1_000_000] * 40)
        self.assertEqual(perm[0], [1_000_000] * 40 + [800_000] * 80)
        self.assertEqual(temp[1:], perm[1:])
        self.assertEqual(temp[1:], self.scenario.prices.series[1:])
        compound = self.prices("shock")
        self.assertEqual(compound[0], perm[0])
        self.assertEqual(compound[1][40], 2_500_000)
        self.assertEqual(compound[1][80], 2_000_000)

    def test_trend_moves_relative_prices_while_common_inflation_does_not(self):
        trend = self.prices("commodity-trend")
        self.assertEqual([row[0] for row in trend], [1_000_000, 2_000_000, 3_000_000])
        labour, gold = trend[0], trend[2]
        for day in range(1, self.days):
            self.assertGreater(gold[day] / labour[day], gold[day - 1] / labour[day - 1])
        expected = 3 * math.exp((.25 - .05) * (self.days - 1) / 365.25)
        self.assertAlmostEqual(gold[-1] / labour[-1], expected, delta=2e-6)
        inflated = self.prices("inflating")
        self.assertAlmostEqual(inflated[2][-1] / inflated[0][-1], 3., delta=2e-6)

    def test_mixed_and_seeded_noise_reuse_the_same_grid(self):
        self.assertEqual(self.prices("mixed", .01),
                         signal.close_prices(self.scenario, "mixed", self.days, 41404, .01))
        history_noise = self.prices("history", .01)[0][-1] / 1_000_000
        for regime in ("commodity-trend", "shock-temporary", "shock-permanent"):
            with self.subTest(regime=regime):
                noisy, base = self.prices(regime, .01), self.prices(regime)
                self.assertEqual(noisy, self.prices(regime, .01))
                self.assertNotEqual(noisy, self.prices(regime, .01, seed=41405))
                self.assertAlmostEqual(noisy[0][-1] / base[0][-1], history_noise, delta=2e-6)


if __name__ == "__main__":
    unittest.main()
