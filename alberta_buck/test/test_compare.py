"""Comparisons (alberta_buck.sim.compare): the spec, its runs and their settings, and the figures the
report and the dashboard's Compare tab read. No EVM: the null comparison (make nix-venv-compare-null)
checks on a real world that two runs with the same settings are the same, frame for frame."""
import json

import pytest

from alberta_buck.sim import compare
from alberta_buck.sim.experiment import load


SPEC = """
question = "what does it change?"
experiment = "economy"
days = 30
[set]
"scenario.seed" = 7
[[axis]]
name = "leak"
note = "the integrator's leak"
[axis.values]
year = {}
none = { "deploy.leak_days" = 0.0 }
[[axis]]
name = "prices"
[axis.values]
history = { "scenario.prices" = "history" }
revert = { "scenario.prices" = "revert", "deploy.k0" = 0.5 }
"""


@pytest.fixture
def spec(tmp_path):
    path = tmp_path / "trial.toml"
    path.write_text(SPEC)
    return compare.load_spec(path)


def test_every_combination_is_one_run_named_by_its_values(spec):
    runs = spec.runs()
    assert [r.id for r in runs] == ["trial.leak-year.prices-history", "trial.leak-year.prices-revert",
                                    "trial.leak-none.prices-history", "trial.leak-none.prices-revert"]
    assert spec.reference("leak") == "year" and spec.reference("prices") == "history"
    assert [r.id for r in spec.runs({"leak": "none"})] == ["trial.leak-none.prices-history", "trial.leak-none.prices-revert"]


def test_a_runs_settings_are_the_shared_then_its_own_and_the_experiment_reads_them(spec):
    run = spec.runs()[3]
    assert run.sets() == ["scenario.seed=7", "deploy.leak_days=0.0", "scenario.prices='revert'", "deploy.k0=0.5"]
    exp = load(spec.experiment, sets=run.sets())
    assert exp.scenario["seed"] == 7 and exp.scenario["prices"] == "revert"
    assert exp.deploy["leak_days"] == 0.0 and exp.deploy["k0"] == 0.5


def test_labels_name_runs_so_they_are_checked(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(SPEC.replace("year = {}", '"one year" = {}'))
    with pytest.raises(ValueError, match="one year"):
        compare.load_spec(bad)
    bad.write_text('experiment = "economy"\n')
    with pytest.raises(ValueError, match="at least one"):
        compare.load_spec(bad)
    bad.write_text(SPEC.replace('experiment = "economy"', 'experiment = "nothing-here"'))
    with pytest.raises(ValueError, match="no experiment"):
        compare.load_spec(bad)


def test_the_index_pairs_runs_that_differ_on_one_axis_with_their_reference(spec):
    body = compare.index(spec, 30)
    assert [a["reference"] for a in body["axes"]] == ["year", "history"]
    found = {(axis, r["id"], ref["id"]) for axis, r, ref in compare.pairs(body)}
    assert found == {
        ("leak", "trial.leak-none.prices-history", "trial.leak-year.prices-history"),
        ("leak", "trial.leak-none.prices-revert", "trial.leak-year.prices-revert"),
        ("prices", "trial.leak-year.prices-revert", "trial.leak-year.prices-history"),
        ("prices", "trial.leak-none.prices-revert", "trial.leak-none.prices-history"),
    }


def frame(day, k=0.75, bvib=1.0, total=24_000_000_000_000, errors=0, extra=None):
    return {"day": day, "control": {"k": k, "bvib": bvib}, "market": {"buck_usd": 1.0},
            "supply": {"total": total, "basket_lien": 6_000_000_000_000, "household_principal": 0},
            "population": {"roles": {"investor": {"arrived": 30 + day, "active": 30 + day, "exited": 0}}},
            "health": {"errors": errors, "faults": 0}, "basket": extra or {}}


def test_divergence_finds_the_first_day_a_pair_differs():
    a = [frame(d) for d in range(5)]
    assert compare.divergence(a, [frame(d) for d in range(5)]) == {"watched": None, "any": None, "days": 5}
    b = [frame(d, extra={"x": 1} if d >= 1 else None, k=0.74 if d >= 3 else 0.75) for d in range(5)]
    assert compare.divergence(a, b) == {"watched": 3, "any": 1, "days": 4}
    assert compare.divergence(a, b[:2])["days"] == 2, "only the days both recorded"


def test_headline_figures_and_their_differences():
    a = [frame(d, k=0.75 - d / 100, bvib=1 + d / 100, errors=1) for d in range(4)]
    for day, f in enumerate(a):
        f["basket"].update(share_price=1.02 + day * .01, hold_index=1.03 - day * .01)
    h = compare.headline(a)
    assert h["share_return"] == pytest.approx(1.05 / 1.02 - 1)
    assert h["hold_return"] == pytest.approx(1 / 1.03 - 1)
    assert compare.headline([frame(0)])["hold_return"] is None
    assert h["days"] == 4 and h["k_end"] == pytest.approx(0.72) and h["k_min"] == pytest.approx(0.72)
    assert h["bvib_gap_mean"] == pytest.approx(0.015) and h["bvib_above"] == pytest.approx(0.75)
    assert h["supply_end"] == 24_000_000 and h["arrived"] == {"investor": 33} and h["refused_days"] == 4
    d = compare.delta(h, compare.headline([frame(d) for d in range(4)]))
    assert d["k_end"] == pytest.approx(-0.03) and d["arrived"] == {"investor": 0} and d["refused_days"] == 4
    assert compare.headline([]) == {"days": 0}


def test_report_reads_the_records_and_writes_the_summary_into_the_index(spec, tmp_path, capsys):
    runs = tmp_path / "runs"
    compare.write_index(runs, compare.index(spec, 30))
    for i, r in enumerate(spec.runs()):
        d = runs / r.id
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"status": "complete"}))
        (d / "frames.jsonl").write_text("".join(json.dumps(frame(day, k=0.75 - i * day / 1000)) + "\n" for day in range(3)))

    class Args:
        pass
    args = Args()
    args.spec, args.runs = str(spec.path), str(runs)
    assert compare.report(args) == 0
    body = json.loads(compare.index_path(runs, "trial").read_text())
    pair = next(p for p in body["summary"]["pairs"] if p["run"] == "trial.leak-none.prices-history")
    assert pair["against"] == "trial.leak-year.prices-history" and pair["held"] == {"prices": "history"}
    assert pair["diverges"]["watched"] == 1 and pair["delta"]["k_end"] == pytest.approx(-0.004)
    assert [c["name"] for c in compare.comparisons(runs)] == ["trial"]
    assert "diverges" in capsys.readouterr().out
