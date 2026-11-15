"""The three-phase price path (scenario.prices = "three-phase"): the control lab's cycle, ported to the
economy's price files (gen_prices.gen_three_phase, three_phase_like)."""
import csv
import math

import pytest

from alberta_buck.sim import gen_prices
from alberta_buck.sim.experiment import build, load


def test_the_series_swing_out_of_phase_about_their_first_prices():
    n, days = 3, 61
    series = [gen_prices.gen_three_phase(100.0, k, n, days) for k in range(n)]
    for k, s in enumerate(series):
        assert s[0] == pytest.approx(100 * (1 + .12 * math.sin(2 * math.pi * k / n)))
        assert max(s) == pytest.approx(112, abs=.1) and min(s) == pytest.approx(88, abs=.1), "sampled daily"
        assert s[30] == pytest.approx(s[0]) and s[60] == pytest.approx(s[0]), "a 30-day period"
    sums = [sum(s[d] for s in series) for d in range(days)]
    assert max(sums) - min(sums) < 1e-9, "evenly out of phase, the three sum to a constant"


def test_a_walk_scales_the_same_steps():
    flat = gen_prices.gen_three_phase(100.0, 1, 5, 200, walk=0.0, seed=7)
    half = gen_prices.gen_three_phase(100.0, 1, 5, 200, walk=0.5, seed=7)
    full = gen_prices.gen_three_phase(100.0, 1, 5, 200, walk=1.0, seed=7)
    logs = [(math.log(h / f), math.log(g / f)) for f, h, g in zip(flat, half, full)]
    assert all(b == pytest.approx(2 * a) for a, b in logs), "the walk's steps are the seed's, scaled"
    assert any(abs(b) > .05 for _, b in logs)
    assert gen_prices.gen_three_phase(100.0, 1, 5, 200, walk=1.0, seed=8) != full, "another seed, other steps"


def test_an_experiment_reads_three_phase_prices(tmp_path, monkeypatch):
    exp = load("alberta_buck/sim/experiments/economy.toml",
               sets=["scenario.prices='three-phase'", "scenario.cycle_amplitude=0.2", "scenario.days=40"])
    hist = build(load("alberta_buck/sim/experiments/economy.toml", sets=["scenario.days=40"]))
    sc = build(exp)
    assert all(f.startswith("tp-") for f in sc.csv_files) and len(sc.csv_files) == len(hist.csv_files)
    for path, first in zip(sc.csv_files, hist.csv_files):
        rows = list(csv.reader((gen_prices.OUT_DIR / path).open()))[1:]
        base = float(list(csv.reader((gen_prices.OUT_DIR / first).open()))[1][1])
        swing = [int(r[1]) / base for r in rows[:31]]
        assert max(swing) == pytest.approx(1.2, abs=.01) and min(swing) == pytest.approx(.8, abs=.01)
    with pytest.raises(ValueError, match="three-phase"):
        build(load("alberta_buck/sim/experiments/economy.toml", sets=["scenario.prices='sideways'"]))
