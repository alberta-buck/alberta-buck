"""WP-14 (WAVE3.org decision 17, R16; CARRY-CONVEXITY.org D7 "The lever
count"): the local skews' pure rules and the per-class booking, no chain.

  * skew_bands        the undertakings' two edges under a signed fill
  * skew_thresholds   the facility's x / y under a drawn line
  * skew_ideal        the seeder's range centre under its position
  * position_amounts / range_position   the seeder's range as a position
  * class_books / book_classes          the per-class booking (send on change,
                                        lazy registration, failure recording)

Every kappa-0 case asserts the SAME floats come back (`is`), which is what
makes the banked cells byte-identical (L3).
"""

from types import SimpleNamespace

import pytest

from alberta_buck.sim import shadow_book
from alberta_buck.sim.facility_agent import (
    HOLD, ISSUE, RETIRE, facility_decision, skew_thresholds,
)
from alberta_buck.sim.seeder_agent import (
    Q96, position_amounts, range_position, single_sided_liquidity,
    skew_ideal, sqrt_at_tick,
)
from alberta_buck.sim.undertaking_agents import EPS_MAX, skew_bands

E6 = 10 ** 6
M = 1_000_000 * E6


# -- the undertakings ------------------------------------------------------ #

def test_ut_kappa_zero_returns_the_same_floats():
    eps = 0.005
    eps_w, eps_s, skew, fill = skew_bands(eps, 0.0, 500_000 * E6, 10 * M)
    assert eps_w is eps and eps_s is eps and skew == 0.0
    assert fill == pytest.approx(0.05)
    # an empty book or no cap: no skew whatever kappa is.
    assert skew_bands(eps, 0.1, 0, 10 * M)[:3] == (eps, eps, 0.0)
    assert skew_bands(eps, 0.1, 5 * M, 0)[:3] == (eps, eps, 0.0)


def test_ut_long_absorbed_deepens_weak_side_and_offers_sooner():
    # kappa* = 0.1: one eps (0.005) of skew at a 5% fill.
    eps_w, eps_s, skew, fill = skew_bands(0.005, 0.1, 500_000 * E6, 10 * M)
    assert fill == pytest.approx(0.05)
    assert skew == pytest.approx(0.005)
    assert eps_w == pytest.approx(0.010)          # the ladder bids deeper
    assert eps_s == pytest.approx(0.0)            # offers the inventory at par
    # the at-par unwind threshold 1 + eps_w / 2 is HIGHER: fires sooner.
    assert 1.0 + eps_w / 2.0 > 1.0 + 0.005 / 2.0


def test_ut_long_issued_is_the_mirror():
    eps_w, eps_s, skew, fill = skew_bands(0.005, 0.1, -500_000 * E6, 10 * M)
    assert fill == pytest.approx(-0.05)
    assert skew == pytest.approx(-0.005)
    assert eps_s == pytest.approx(0.010)          # needs a deeper premium
    assert eps_w == pytest.approx(0.0)            # bids at par: retires sooner
    # the retire threshold 1 + skew / 2 is BELOW par: fires sooner.
    assert 1.0 + skew / 2.0 < 1.0


def test_ut_edges_clamped_never_through_par_and_bid_positive():
    eps_w, eps_s, skew, fill = skew_bands(0.03, 2.0, 10 * M, 10 * M)
    assert fill == 1.0 and skew == 2.0
    assert eps_w == EPS_MAX and eps_s == 0.0
    assert 1.0 - eps_w > 0                        # the ladder's first bid
    eps_w, eps_s, _, _ = skew_bands(0.03, 2.0, -10 * M, 10 * M)
    assert eps_w == 0.0 and eps_s == EPS_MAX
    # a book beyond its cap is a full fill, not more.
    assert skew_bands(0.03, 0.1, 30 * M, 10 * M)[3] == 1.0


# -- the facility ----------------------------------------------------------- #

def test_fac_kappa_zero_returns_the_same_floats():
    x, y = 0.03, 0.03
    x_eff, y_eff, skew, fill = skew_thresholds(x, y, 0.0, 1 * M, 2 * M)
    assert x_eff is x and y_eff is y and skew == 0.0
    assert fill == pytest.approx(-0.5)
    assert skew_thresholds(x, y, 0.1, 0, 2 * M)[:3] == (x, y, 0.0)
    assert skew_thresholds(x, y, 0.1, 1 * M, 0)[:3] == (x, y, 0.0)


def test_fac_drawn_line_needs_deeper_premium_and_retires_sooner():
    x_eff, y_eff, skew, fill = skew_thresholds(0.03, 0.03, 0.1, 1 * M, 2 * M)
    assert fill == pytest.approx(-0.5) and skew == pytest.approx(-0.05)
    assert x_eff == pytest.approx(0.08)           # x + kappa |f|
    assert y_eff == pytest.approx(0.0)            # y - kappa |f|, floored
    # through the rule: at a 4% premium a fresh line issues, a half-drawn
    # skewed one holds; at a 1% discount the skewed line retires, an
    # unskewed one holds.
    assert facility_decision(0.96, 0.03, 0.03, 0, 4 * M, 0.5, False)[0] == ISSUE
    assert facility_decision(0.96, x_eff, y_eff, 1 * M, 4 * M, 0.5, False)[0] == HOLD
    assert facility_decision(1.01, 0.03, 0.03, 1 * M, 4 * M, 0.5, True)[0] == HOLD
    assert facility_decision(1.01, x_eff, y_eff, 1 * M, 4 * M, 0.5, True)[0] == RETIRE


def test_fac_fill_clamped_at_the_cap():
    x_eff, y_eff, skew, fill = skew_thresholds(0.03, 0.03, 0.02, 3 * M, 2 * M)
    assert fill == -1.0 and skew == pytest.approx(-0.02)
    assert x_eff == pytest.approx(0.05) and y_eff == pytest.approx(0.01)


# -- the seeder ------------------------------------------------------------- #

def test_sd_kappa_zero_returns_the_ideal_itself():
    ideal = 1_050_000
    out, skew, fill = skew_ideal(ideal, 0.0, -2 * M, 10 * M)
    assert out is ideal and skew == 0.0 and fill == pytest.approx(-0.2)
    assert skew_ideal(ideal, 0.1, 0, 10 * M)[0] is ideal
    assert skew_ideal(ideal, 0.1, 5 * M, 0)[0] is ideal


def test_sd_sold_buck_lowers_the_centre_bought_raises_it():
    ideal = 1_050_000
    out, skew, fill = skew_ideal(ideal, 0.1, -2 * M, 10 * M)
    assert fill == pytest.approx(-0.2) and skew == pytest.approx(-0.02)
    assert out == round(ideal * 0.98)
    out, skew, _ = skew_ideal(ideal, 0.1, 5 * M, 10 * M)
    assert skew == pytest.approx(0.05) and out == round(ideal * 1.05)


def test_position_amounts_match_single_sided_funding():
    # A range entirely above the price holds token0 only, and exactly the
    # amount single_sided_liquidity was inverted from (up to the shave and
    # the integer round-down).
    lo, hi = 60, 600
    amount = 5 * M
    L = single_sided_liquidity(lo, hi, amount, above=True)
    a0, a1 = position_amounts(L, sqrt_at_tick(0), lo, hi)
    assert a1 == 0
    assert 0.99 * amount <= a0 <= amount
    # entirely below: token1 only.
    Lb = single_sided_liquidity(-600, -60, amount, above=False)
    b0, b1 = position_amounts(Lb, sqrt_at_tick(0), -600, -60)
    assert b0 == 0
    assert 0.99 * amount <= b1 <= amount
    # inside: both, and the token0 side shrinks as the price rises through
    # the range (the range converts).
    mid0, mid1 = position_amounts(L, sqrt_at_tick(300), lo, hi)
    assert 0 < mid0 < a0 and mid1 > 0
    top0, top1 = position_amounts(L, sqrt_at_tick(hi), lo, hi)
    assert top0 == 0 and top1 > mid1
    assert position_amounts(0, Q96, lo, hi) == (0, 0)
    assert position_amounts(L, Q96, hi, lo) == (0, 0)


def test_range_position_signs():
    # buck-funded: what left the range was SOLD (issued, negative).
    assert range_position("buck", 5 * M, 5 * M) == 0
    assert range_position("buck", 5 * M, 3 * M) == -2 * M
    assert range_position("buck", 5 * M, 6 * M) == 0          # never positive
    # usdc-funded: the BUCK now in the range was BOUGHT (absorbed, positive).
    assert range_position("usdc", 5 * M, 0) == 0
    assert range_position("usdc", 5 * M, 2 * M) == 2 * M
    assert range_position("", 5 * M, 2 * M) == 0


# -- the per-class booking -------------------------------------------------- #

def test_class_books_signs_and_caps():
    ctr = {"ut_issued_open": 2 * M, "ut_absorbed_open": 3 * M, "ut_cap": 15 * M,
           "fac_drawn": 1 * M, "fac_cap": 4 * M, "sd_q": -5 * E6, "sd_cap": 10 * M}
    b = shadow_book.class_books(ctr)
    assert b["uts"] == (-2 * M, 15 * M)
    assert b["utw"] == (3 * M, 15 * M)
    assert b["fac"] == (-1 * M, 4 * M)
    assert b["sd"] == (-5 * E6, 10 * M)
    assert shadow_book.class_books({}) == {c: (0, 0) for c in shadow_book.CLASSES}
    # the lumped sum of WP-13 is untouched.
    assert shadow_book.net_inventory(ctr) == 3 * M - 2 * M - 1 * M


class _Fn:
    def __init__(self, log, name, args):
        self.log, self.name, self.args = log, name, args


class _Stab:
    def __init__(self, log, cls):
        self.address = "0x" + cls.ljust(40, "0")
        self.functions = SimpleNamespace(
            setBookAndCap=lambda q, c: _Fn(log, f"{cls}.setBookAndCap", (q, c)))


class _Obs:
    def __init__(self, log):
        self.functions = SimpleNamespace(
            setShadowOffset=lambda q: _Fn(log, "setShadowOffset", (q,)),
            addStabilizer=lambda a, lam: _Fn(log, "addStabilizer", (a, lam)),
            setStabilizerWeight=lambda a, w: _Fn(log, "setStabilizerWeight", (a, w)))


def _deployment(log, perclass=True, registered=("uts", "utw")):
    def send(fn, sender=None):
        log.append((fn.name, fn.args, sender))
    d = SimpleNamespace(chain=SimpleNamespace(send=send), gov="0xG0V",
                        observer=_Obs(log))
    if perclass:
        d.sim_stabs = {c: _Stab(log, c) for c in shadow_book.CLASSES}
        d.sim_stab_reg = set(registered)
        d.sim_stab_gains = {"uts": (10 ** 18, 10 ** 18), "utw": (10 ** 18, 10 ** 18),
                            "fac": (10 ** 18, 5 * 10 ** 17), "sd": (0, 25 * 10 ** 16)}
    return d


def test_book_dispatches_per_class_and_sends_only_on_change():
    log = []
    d = _deployment(log)
    ctr = {}
    shadow_book.book(d, ctr)                       # everything 0: nothing sent
    assert log == [] and ctr.get("sh_class_txs", 0) == 0
    ctr["ut_cap"] = 15 * M                         # the cap struck: both sides
    shadow_book.book(d, ctr)
    assert [(n, a) for n, a, _ in log] == [("uts.setBookAndCap", (0, 15 * M)),
                                           ("utw.setBookAndCap", (0, 15 * M))]
    assert ctr["sh_class_txs"] == 2 and all(s == "0xG0V" for _, _, s in log)
    shadow_book.book(d, ctr)                       # unchanged: no tx
    assert len(log) == 2
    ctr["ut_issued_open"] = 2 * M                  # the strong side issues
    shadow_book.book(d, ctr)
    assert log[-1][:2] == ("uts.setBookAndCap", (-2 * M, 15 * M))
    assert ctr["sh_class_txs"] == 3
    # the lumped offset is never touched on the per-class path.
    assert not any(n == "setShadowOffset" for n, _, _ in log)
    assert "sh_offset" not in ctr


def test_book_registers_a_late_class_on_its_first_cap():
    log = []
    d = _deployment(log, registered=("uts", "utw"))
    ctr = {"fac_drawn": 1 * M}                     # drawn, but no cap yet
    shadow_book.book(d, ctr)
    assert [n for n, _, _ in log] == ["fac.setBookAndCap"]
    assert "fac" not in d.sim_stab_reg              # cap 0: not registered
    ctr["fac_cap"] = 4 * M
    shadow_book.book(d, ctr)
    names = [n for n, _, _ in log]
    assert names[-3:] == ["fac.setBookAndCap", "addStabilizer", "setStabilizerWeight"]
    assert log[-2][1] == (d.sim_stabs["fac"].address, 10 ** 18)
    assert log[-1][1] == (d.sim_stabs["fac"].address, 5 * 10 ** 17)
    assert "fac" in d.sim_stab_reg


def test_book_records_failure_and_keeps_last_books():
    log = []
    d = _deployment(log)
    ctr = {"ut_cap": 15 * M}
    shadow_book.book(d, ctr)
    assert ctr["shb_uts"] == (0, 15 * M)

    def boom(fn, sender=None):
        raise RuntimeError("Revert")
    d.chain.send = boom
    ctr["ut_absorbed_open"] = 1 * M
    shadow_book.book(d, ctr)
    assert ctr["shb_utw"] == (0, 15 * M) and "Revert" in ctr["sh_class_err"]
    assert ctr["sh_class_txs"] == 2


def test_book_lumped_path_without_the_contracts():
    log = []
    d = _deployment(log, perclass=False)
    ctr = {"ut_absorbed_open": 4 * M, "ut_cap": 15 * M}
    shadow_book.book(d, ctr)
    assert [(n, a) for n, a, _ in log] == [("setShadowOffset", (4 * M,))]
    assert ctr["sh_offset"] == 4 * M and "sh_class_txs" not in ctr
