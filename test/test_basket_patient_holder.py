"""Offline regression for partial and full redemptions in a patient-holder replay."""

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("patient_holder", ROOT / "scripts/run-basket-patient-holder.py")
PATIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATIENT)


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


if __name__ == "__main__":
    unittest.main()
