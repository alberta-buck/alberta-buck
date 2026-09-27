"""The reverting twin of a price window (gen_prices.revert_like): the
savings demonstration's "Reverting" toggle (doc/CONVERGENCE.org 7a).  Each
series starts at its history's first price, ends there, moves with its own
realized volatility, and is the same bytes on every run."""
import csv
import math

import pytest

from alberta_buck.sim.gen_prices import revert_like


def _write(path, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "close_usd_micro"])
        for k, v in enumerate(rows):
            w.writerow([k, v])


def _read(path):
    with path.open() as f:
        return [int(r[1]) for r in list(csv.reader(f))[1:]]


def _vol(rows):
    logs = [math.log(b / a) for a, b in zip(rows, rows[1:])]
    m = sum(logs) / len(logs)
    return math.sqrt(sum((x - m) ** 2 for x in logs) / (len(logs) - 1))


def test_a_reverting_twin_keeps_the_start_and_the_volatility_and_drops_the_trend(tmp_path):
    n = 1000
    calm = [round(1_000_000 * 1.0003 ** k * (1 + 0.004 * math.sin(k))) for k in range(n)]
    wild = [round(50_000_000 * 1.002 ** k * (1 + 0.03 * math.sin(k * 1.7))) for k in range(n)]
    _write(tmp_path / "hist-calm-2025-09-01-1000d.csv", calm)
    _write(tmp_path / "hist-wild-2025-09-01-1000d.csv", wild)
    files = ["hist-calm-2025-09-01-1000d.csv", "hist-wild-2025-09-01-1000d.csv"]
    out = revert_like(files, out_dir=tmp_path)
    assert out == ["rev-calm-2025-09-01-1000d.csv", "rev-wild-2025-09-01-1000d.csv"]
    for src, dst in zip(files, out):
        h, r = _read(tmp_path / src), _read(tmp_path / dst)
        assert len(r) == len(h)
        assert r[0] == h[0] and r[-1] == h[0]          # ends where it began: no trend
        assert _vol(r) == pytest.approx(_vol(h), rel=0.35)
    assert _vol(_read(tmp_path / out[1])) > 3 * _vol(_read(tmp_path / out[0]))

    first = [(tmp_path / f).read_bytes() for f in out]
    for f in out:
        (tmp_path / f).unlink()
    assert [(tmp_path / f).read_bytes() for f in revert_like(files, out_dir=tmp_path)] == first


def test_the_scenario_refuses_an_unknown_price_mode():
    from alberta_buck.sim import experiment as expmod
    exp = expmod.load("alberta_buck/sim/experiments/demo-savings.toml",
                      sets=["scenario.prices=sideways"])
    with pytest.raises(ValueError, match="history.*revert"):
        expmod.build(exp)
