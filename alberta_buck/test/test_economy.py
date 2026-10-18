"""The household economy's grid: its cells, their settings, and the acceptance judgement."""
import json

from alberta_buck.sim import economy

CENTRE                          = dict(debtor_rate=6., saver_rate=6., investor_rate=12., adopt_years=1.)


def test_cells_cover_the_grid_and_its_variants_once():
    assert len(economy.cells("centre")) == 2
    grid                        = economy.cells("grid")
    assert len(grid) == len({c.name for c in grid}) == 36
    every                       = economy.cells("all")
    assert len(every) == len({c.name for c in every}) == 42
    cell                        = economy.Cell("lo", "hi", "revert", 45517)
    sets                        = cell.sets(CENTRE)
    assert cell.name == "slo-dhi-revert-45517"
    assert "scenario.prices=revert" in sets and "scenario.seed=45517" in sets
    assert f"{economy.POP}.debtor_rate=2.0" in sets and f"{economy.POP}.adopt_years=0.3333333333333333" in sets
    assert f"{economy.POP}.gain=0" in economy.Cell("mid", "mid", "history", 41404, "open").sets(CENTRE)


def test_adopt_names_only_completed_checkpoints(tmp_path):
    for name, completed in (("done", True), ("running", False)):
        (tmp_path / f"{name}.partial").write_text(json.dumps({"meta": {"run": {"completed": completed}}}))
    (tmp_path / "torn.partial").write_text('{"meta": ')
    assert economy.adopt(tmp_path) == ["done"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["done.json", "running.partial", "torn.partial"]


def test_a_short_economy_runs_to_its_closing_summary():
    from alberta_buck.sim import experiment as expmod, rng
    from alberta_buck.sim.loop import run
    from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
    mode                        = rng.mode()            # the economy selects keyed draws process-wide
    try:
        scenario                = expmod.build(expmod.load(economy.repo_root() / economy.EXPERIMENT))
        scenario.days           = 3
        with PyrevmAnvil(port=0) as evm:
            summary             = run(scenario, evm, out_path=None, verbose=False, basket_impl="equity-machine")
    finally:
        rng.set_mode(mode)
    assert summary is not None


def frame(day, bvib, k, **extra):
    return dict(day=day, basketVal=int(bvib * 1e18), buckK=int(k * 1e18), supply=int(60e12), **extra)


def test_judge_passes_a_steady_world_and_fails_a_railed_or_drifting_one(tmp_path):
    def judged(frames):
        path                    = tmp_path / "v.json"
        path.write_text(json.dumps(dict(frames=frames, meta={"experiment": {"deploy": {"kmin": 0., "kmax": .95}}})))
        return economy.judge(path)

    steady                      = [frame(d, 1 + (.004 if d % 2 else -.004), .75) for d in range(730)]
    assert judged(steady)["passed"]
    railed                      = [frame(d, 1., .95 if d >= 400 else .75) for d in range(730)]
    r                           = judged(railed)
    assert not r["passed"] and r["longest_rail"] == 330
    drifting                    = [frame(d, .97, .8) for d in range(730)]
    assert not judged(drifting)["passed"]
    broken                      = steady[:-1] + [frame(729, 1., .75, octl=[dict(member="0/debtor/0", error="boom")])]
    assert judged(broken)["errors"] == 1 and not judged(broken)["passed"]
