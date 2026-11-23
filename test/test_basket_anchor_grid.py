"""Offline checks for the common-anchor basket replay inputs."""

from bisect import bisect_right
from datetime import date, timedelta
import importlib.util
from pathlib import Path
import unittest

from alberta_buck.sim.prices import Prices
from alberta_buck.sim.quotes import ingest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("anchor_grid", ROOT / "scripts/run-basket-anchor-grid.py")
GRID = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GRID)


class BasketAnchorGridTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens, cls.tapes, cls.calendar = GRID.build_tapes()

    def test_common_anchors_match_vendored_observations_and_baseline_bridge(self):
        raw = dict(LABR=ingest.load_labour_us(), CNST=ingest.load_construction_us(),
                   FOOD=ingest.load_food_us(), NRGC=ingest.load_energy_us(), PAXG=ingest.load_gold())
        observed = [dict(raw[token]) for token in self.tokens]
        self.assertEqual(self.tokens, ["LABR", "CNST", "FOOD", "NRGC", "PAXG"])
        for anchor in self.calendar["common_anchors"]:
            month = date.fromisoformat(anchor["observation_month"])
            expected = [round(series[month] * 1_000_000) // 100 for series in observed]
            self.assertEqual(expected, anchor["prices"])
            for tape in self.tapes.values():
                self.assertEqual([column[anchor["day"]] for column in tape], expected)
        history = Prices([f"hist-{token.lower()}.csv" for token in self.tokens])
        self.assertEqual(self.tapes["pinned-bridge"], [column[:730] for column in history.series])
        self.assertNotEqual(self.tapes["pinned-bridge"], self.tapes["straight-bridge"])
        self.assertNotEqual(self.tapes["straight-bridge"], self.tapes["carry-forward"])

    def test_five_year_tapes_preserve_prefix_and_check_coverage(self):
        tokens, tapes, calendar = GRID.build_tapes(1825)
        self.assertEqual(tokens, self.tokens)
        self.assertEqual((calendar["first_date"], calendar["last_date"]), ("2020-11-01", "2025-10-30"))
        self.assertEqual(calendar["availability_lag_calendar_days"], 61)
        self.assertGreater(calendar["first_anchor_beyond_horizon"]["day"], 1824)
        for name, tape in tapes.items():
            self.assertTrue(all(len(column) == 1825 for column in tape))
            self.assertEqual([column[:730] for column in tape], self.tapes[name])
            for anchor in calendar["common_anchors"]:
                self.assertEqual([column[anchor["day"]] for column in tape], anchor["prices"])
        with self.assertRaises(ValueError):
            GRID.build_tapes(0)
        with self.assertRaises(ValueError):
            GRID.build_tapes(36500)

    def test_carry_forward_does_not_use_an_anchor_before_availability(self):
        anchors = self.calendar["common_anchors"]
        first = date.fromisoformat(self.calendar["first_date"])
        self.assertEqual((first.isoformat(), self.calendar["last_date"]), ("2020-11-01", "2022-10-31"))
        self.assertEqual(len(anchors), 24)
        self.assertEqual(self.calendar["last_common_anchor_day"], 699)
        upcoming = self.calendar["first_anchor_beyond_horizon"]
        self.assertEqual(upcoming["day"], 730)
        self.assertEqual(upcoming["assumed_available"], "2022-11-01")
        dates = [date.fromisoformat(anchor["assumed_available"]) for anchor in anchors]
        for anchor, available in zip(anchors, dates, strict=True):
            self.assertEqual(available - date.fromisoformat(anchor["observation_month"]), timedelta(days=61))
        self.assertTrue(all(len(column) == 730 for tape in self.tapes.values() for column in tape))
        for offset, row in enumerate(zip(*self.tapes["carry-forward"], strict=True)):
            latest = bisect_right(dates, first + timedelta(days=offset)) - 1
            self.assertEqual(list(row), anchors[latest]["prices"])


if __name__ == "__main__":
    unittest.main()
