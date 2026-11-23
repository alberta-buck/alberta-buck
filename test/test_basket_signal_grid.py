"""Offline checks for basket signal replay, attribution, and matched returns."""

import importlib.util
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GRID = load_script("run-basket-signal-grid")
RETURNS = load_script("summarize-basket-signal-grid")
TURNS = load_script("measure-basket-turns")


def frame(day, prices, *orders):
    """Orders are (transaction, TOKEN index, side, pair increment, fill)."""
    result = dict(day=day, prices=prices, decisions=[], trades=[])
    for tx, token, side, increment, amount in orders:
        cap = {"planned" + side.capitalize(): 10 + increment,
               "planWithoutPair" + side.capitalize(): 10}
        position = dict(tx=tx, token=token, tick=0)
        result["decisions"].append(dict(position, index=0, gap=0.03, **cap))
        result["trades"].append(dict(position, index=1, side=side, amount=amount))
    return result


class BasketSignalGridTest(unittest.TestCase):
    def test_reverting_tape_keeps_observed_innovations(self):
        observed = [1000, 1200, 1200, 900]
        reverting = GRID.reverting_history(observed, len(observed))
        self.assertEqual(reverting[0], observed[0])
        self.assertLess(reverting[2], reverting[1])
        for day in range(1, len(observed)):
            before, after = (math.log(price / observed[0]) for price in reverting[day - 1:day + 1])
            self.assertAlmostEqual(after - math.exp(-1 / 30) * before,
                                   math.log(observed[day] / observed[day - 1]))

    def test_every_regime_gets_the_same_seeded_noise(self):
        scenario = SimpleNamespace(tokens=[("LABR",), ("CNST",)], prices=SimpleNamespace(
            series=[[1_000_000, 1_020_000, 990_000, 1_050_000], [2_000_000] * 4]))
        history = GRID.close_prices(scenario, "history", 4, 41404, 0)
        noisy_history = GRID.close_prices(scenario, "history", 4, 41404, .01)
        for regime in GRID.REGIMES:
            with self.subTest(regime=regime):
                clean = GRID.close_prices(scenario, regime, 4, 41404, 0)
                noisy = GRID.close_prices(scenario, regime, 4, 41404, .01)
                self.assertEqual(noisy, GRID.close_prices(scenario, regime, 4, 41404, .01))
                self.assertNotEqual(noisy, GRID.close_prices(scenario, regime, 4, 41405, .01))
                for plain, overlay, baseline, baseline_overlay in zip(clean, noisy, history, noisy_history):
                    self.assertEqual(overlay[0], plain[0])
                    for a, b, c, d in zip(plain, overlay, baseline, baseline_overlay):
                        self.assertAlmostEqual(b / a, d / c, delta=2e-6)

    def test_grid_rejects_partial_capture_and_missing_recording(self):
        args = SimpleNamespace(experiment=None, days=2)
        tokens = ["LABR", "CNST"]
        prices = [[100, 200], [110, 200]]
        cell = dict(settings=[], expected_prices=prices)
        config = GRID.experiment.load().resolved()
        config["scenario"]["days"] = args.days
        frames = [dict(frame(day, price), pairs=[dict(ready=3)]) for day, price in enumerate(prices)]
        frames[0]["decisions"] = [dict(tx="tx", tick=0, index=i, token=i) for i in range(2)]
        capture = dict(tokens=tokens, config=config, frames=frames)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            path.write_text(json.dumps(capture))
            self.assertTrue(GRID.valid_capture(path, args, cell, tokens))
            path.write_text(json.dumps(dict(capture, frames=frames[:1])))
            self.assertFalse(GRID.valid_capture(path, args, cell, tokens))
            frames[0]["decisions"].clear()
            path.write_text(json.dumps(capture))
            self.assertFalse(GRID.valid_capture(path, args, cell, tokens))

    def test_roi_pairs_both_controls_and_averages_repeated_tapes_first(self):
        key = ("both", "standard", "fast")
        groups = {}
        for seed, tape, candidate in [(1, "A", .03), (2, "A", .05), (3, "B", .12),
                                      (4, "B", .90), (5, "B", .90)]:
            common = dict(regime="history", noise_sigma=0., seed=seed, price_digest=tape)
            groups[str(seed)] = {
                key: (dict(common, arm="candidate"), candidate),
                "production": (dict(common, arm="production"), .01),
                "maintenance": (dict(common, arm="maintenance"), .0),
            }
        del groups["4"]["maintenance"]
        groups["5"]["production"][0]["price_digest"] = "different"
        report = RETURNS.render(Path("fixture"), {"days": 36}, groups, [])
        self.assertIn("| 3/5 | 2 | +8.000% | +1.000% | +0.000% | +7.000 | +8.000 |", report)
        self.assertIn("`4` · `both/standard/fast` · maintenance", report)
        self.assertIn("`5` · `both/standard/fast` · different control inputs", report)

    def test_counts_only_executed_increases_once_per_token_side_and_day(self):
        day = frame(0, [100, 100], ("a", 0, "buy", 3, 12), ("b", 0, "buy", 2, 11),
                    ("c", 0, "sell", 1, 9), ("d", 1, "buy", 0, 10), ("e", 1, "sell", 2, 0))
        next_day = frame(1, [110, 100], ("f", 0, "buy", 2, 10))
        self.assertEqual(TURNS.increased_orders([day, next_day]),
                         [(0, 0, "buy"), (0, 0, "sell"), (1, 0, "buy")])
        day["decisions"][0]["index"] = 2
        with self.assertRaisesRegex(ValueError, "lacks a preceding decision"):
            TURNS.increased_orders([day, next_day])

    def test_forward_returns_censor_missing_closes_and_preserve_side(self):
        frames = [frame(0, [100, 100], ("a", 0, "buy", 2, 10)),
                  frame(1, [120, 100], ("b", 0, "sell", 2, 10)),
                  frame(2, [100, 100], ("c", 0, "buy", 2, 10))]
        capture = dict(tokens=["LABR", "CNST"], frames=frames)
        log_change = round(math.log(1.2) * 10_000, 3)
        self.assertEqual(list(TURNS.timing(capture, [1, 2])), [
            ["buy", 1, 2, 1, 1, 0, log_change],
            ["buy", 2, 2, 1, 0, 1, 0.0],
            ["sell", 1, 1, 1, 1, 0, log_change],
            ["sell", 2, 1, 0, 0, 0, ""],
        ])
        with self.assertRaisesRegex(ValueError, "incomplete daily TOKEN prices"):
            list(TURNS.timing(dict(capture, frames=[frames[0], frames[2]]), [1]))


if __name__ == "__main__":
    unittest.main()
