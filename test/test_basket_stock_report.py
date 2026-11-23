"""Return numeraires and baseline matching in the funded basket report."""
import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    'stock_report', Path(__file__).resolve().parents[1] / 'scripts/summarize-basket-stock-grid.py')
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class StockReportTest(unittest.TestCase):
    def test_buck_gain_can_coexist_with_usdc_loss(self):
        arm = {'opening': {'block': 71, 'share_price': '100'}, 'closing': {'share_price': '110'},
               'daily': [{'share': 100}, {'share': 110}]}
        frames = [{'block': 71, 'basket': {'hold_index': 1}, 'market': {'book': {'buck_bid': 1}}},
                  {'block': 99, 'basket': {'hold_index': 1.04}, 'market': {'book': {'buck_bid': .8}}}]
        passive, external = REPORT.treatment_benchmarks(arm, frames, 1)
        self.assertAlmostEqual(passive, .04)
        self.assertAlmostEqual(external, -.12)
        frames[-1]['market']['book'] = None
        passive, external = REPORT.treatment_benchmarks(arm, frames, 1)
        self.assertAlmostEqual(passive, .04)
        self.assertIsNone(external)
        arm['opening']['block'] = 72
        self.assertEqual(REPORT.treatment_benchmarks(arm, frames, 1), (None, None))
