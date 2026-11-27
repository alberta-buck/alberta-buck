"""Offline regression for partial and full redemptions in a patient-holder replay."""

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("patient_holder", ROOT / "scripts/run-basket-patient-holder.py")
PATIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATIENT)
STOCK_SPEC = importlib.util.spec_from_file_location("basket_stock", ROOT / "scripts/run-basket-stock-grid.py")
STOCK = importlib.util.module_from_spec(STOCK_SPEC)
STOCK_SPEC.loader.exec_module(STOCK)


class PatientHolderTest(unittest.TestCase):
    def test_opening_mark_keeps_separate_upper_bound_after_redemption_or_topup(self):
        opening = {"members": {name: {"positions": {str(i): {"shares": "10"}}}
                               for i, name in enumerate(("partial", "closed", "topup"))}}
        closing = {"total_shares": "100", "equity_raw": 250,
                   "members": {
                       "partial": {"exited": False, "positions": {"0": {"shares": "4"}}},
                       "closed": {"exited": True, "positions": {}},
                       "topup": {"exited": False, "positions": {"2": {"shares": "14"}}},
                   }}
        result = PATIENT.terminal_sleeve(opening, closing)
        self.assertEqual([row["counterfactual_hold_mark_raw"] for row in result.values()], [25, 25, 25])
        self.assertEqual([row["remaining_opening_shares_upper"] for row in result.values()], ["4", "0", "10"])
        self.assertEqual([row["remaining_opening_mark_upper_raw"] for row in result.values()], [10, 0, 25])
        self.assertTrue(result["closed"]["exited"])

    def test_missed_buy_retains_its_gate_when_a_later_response_fills(self):
        responses = [
            {"day": 0, "tick": tick, "tx": str(tick), "response_log": 7, "decisions": [{
                "token": 0, "detail": {"gap": -gap_bp * 10**14}, "stage": stage,
                "fills": fills, "peers": [{"gate": 2}],
            }]}
            for tick, gap_bp, stage, fills in (
                (1, 130, "urgency_or_band", []),
                (2, 150, "no_directional_rate", [{"side": "sell", "swap": {"log": 4}}]),
                (3, 120, "changed_plan_with_swap", [{"side": "buy", "swap": {"log": 5}}]),
            )
        ]
        [row] = STOCK.ranked_opportunities(responses, ["CNST", "FOOD"], [(100, 100), (110, 102)], 100, 1)
        self.assertEqual((row["stage"], row["gap_bp"], row["tx"]), ("no_directional_rate", -150, "2"))
        self.assertEqual(row["peer_gates"], {"live_spread": 1})
        self.assertTrue(row["same_day_directional_swap"])
        self.assertGreater(row["forward_relative_log_pct"], 0)


if __name__ == "__main__":
    unittest.main()
