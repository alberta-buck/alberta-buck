"""The household economy's grid: its cells, their settings, and the acceptance judgement."""
import json
import pytest

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


def test_adopt_names_only_completed_checkpoints(tmp_path, monkeypatch):
    rendered                    = []
    monkeypatch.setattr(economy, "ensure_pdf", lambda p: rendered.append(p.stem))
    for name, completed in (("done", True), ("running", False)):
        (tmp_path / f"{name}.partial").write_text(json.dumps({"meta": {"run": {"completed": completed}}}))
    (tmp_path / "torn.partial").write_text('{"meta": ')
    assert economy.adopt(tmp_path) == ["done"]
    assert rendered == ["done"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["done.json", "running.partial", "torn.partial"]


def test_a_short_economy_runs_to_its_closing_summary(monkeypatch):
    from alberta_buck.sim import experiment as expmod, rng
    from alberta_buck.sim.loop import run
    from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
    from alberta_buck.sim.credit_households import CreditHouseholdAgent
    frames                      = []
    original                    = CreditHouseholdAgent.act
    def transient_error(self, d, scenario, day, tick, ctr):
        original(self, d, scenario, day, tick, ctr)
        self.last_error         = "transient" if day == 1 else ""
    monkeypatch.setattr(CreditHouseholdAgent, "act", transient_error)
    mode                        = rng.mode()            # the economy selects keyed draws process-wide
    try:
        scenario                = expmod.build(expmod.load(economy.repo_root() / economy.EXPERIMENT))
        scenario.days           = 3
        with PyrevmAnvil(port=0) as evm:
            summary             = run(scenario, evm, out_path=None, verbose=False, basket_impl="equity-machine",
                                      on_frame=frames.append)
    finally:
        rng.set_mode(mode)
    assert summary is not None
    assert all(abs(actual - declared) < 200 for actual, declared in zip(frames[0]["eq_w"], frames[0]["eq_decl"])), \
        "the economy opens near its declared basket allocation"
    assert any(a.get("error") == "transient" for a in frames[1]["octl"]), "errors between chain marks survive"
    assert all(not a.get("error") for a in frames[2]["octl"])


def frame(day, bvib, k, **extra):
    return dict(day=day, basketVal=int(bvib * 1e18), buckK=int(k * 1e18), supply=int(60e12), **extra)


def test_judge_passes_a_steady_world_and_fails_a_railed_or_drifting_one(tmp_path):
    def judged(frames):
        path                    = tmp_path / "v.json"
        path.write_text(json.dumps(dict(frames=frames, meta={"run": {"completed": True},
                                    "experiment": {"deploy": {"kmin": 0., "kmax": .95}}})))
        return economy.judge(path)

    steady                      = [frame(d, 1 + (.004 if d % 2 else -.004), .75) for d in range(1827)]
    assert judged(steady)["passed"]
    railed                      = [frame(d, 1., .95 if d >= 400 else .75) for d in range(1827)]
    r                           = judged(railed)
    assert not r["passed"] and r["longest_rail"] == 1427
    drifting                    = [frame(d, .97, .8) for d in range(1827)]
    assert not judged(drifting)["passed"]
    # An error followed by clean days still fails; looking only at the final frame hid it.
    broken                      = (steady[:400] + [frame(400, 1., .75, octl=[dict(member="0/debtor/0", error="boom")])]
                                   + steady[401:])
    assert judged(broken)["errors"] == 1 and not judged(broken)["passed"]
    assert not judged(steady[:30])["passed"] and not judged(steady[:30])["full_horizon"]
    assert not judged(steady[:400] + steady[401:])["passed"], "missing daily data is not five-year acceptance"
    stalled                     = steady[:]
    stalled[431]                = frame(431, 1., .75, octl=[dict(member="seller", exit_started=400, exited=False)])
    assert judged(stalled)["exit_overdue"] == ["seller"] and not judged(stalled)["passed"]
    # Clearing the error, or falsely claiming to have exited, cannot turn stranded credit green.
    stranded                    = steady[:-1] + [frame(1826, 1., .75, octl=[dict(member="seller", role="debtor",
                                    exit_started=400, exited=True, released=True, limit=100e6)])]
    assert judged(stranded)["exit_invalid"] == ["seller"] and not judged(stranded)["passed"]
    recent                      = steady[:-1] + [frame(1826, 1., .75, octl=[dict(member="seller", role="debtor",
                                    exit_started=1820, exited=False)])]
    assert judged(recent)["passed"], "an exit still within its settlement window is not overdue"


def test_policy_comparisons_and_seed_selection_are_explicit():
    policies                    = economy.cells("policies")
    assert len(policies) == len({c.name for c in policies}) == 16
    assert all(c.supply == c.demand == "mid" for c in policies)
    assert set(economy.cells("centre")) <= set(policies), "comparisons reuse the adaptive baseline cells"
    assert len(economy.selected("policies", 45517)) == 8
    assert {c.seed for c in economy.selected("centre", 45517)} == {45517}
    with pytest.raises(ValueError, match="no policies cells"):
        economy.selected("policies", 999)
    with pytest.raises(ValueError, match="campaign"):
        economy.campaign_path(economy.repo_root(), "../another")


def test_pdf_is_repaired_without_rerunning_a_simulation(tmp_path, monkeypatch):
    from hashlib import sha256
    from alberta_buck.sim import report
    vector                      = tmp_path / "run.json"
    vector.write_text('{"a": 1}')
    pdf                         = vector.with_suffix(".pdf")
    pdf.write_bytes(b"old PDF")
    calls                       = []
    def render(path):
        calls.append(path)
        pdf.write_bytes(sha256(path.read_bytes()).hexdigest().encode())
    monkeypatch.setattr(report, "render", render)
    economy.ensure_pdf(vector)
    economy.ensure_pdf(vector)
    assert calls == [vector]


def test_failed_process_is_not_adopted_as_success(tmp_path):
    runs                        = tmp_path / "runs"
    runs.mkdir()
    (runs / "failed.partial").write_text(json.dumps({"meta": {"run": {"completed": True}}}))
    (tmp_path / "status.json").write_text(json.dumps({"failed": {"returncode": 1}}))
    assert economy.adopt(runs) == []
    (tmp_path / "status.json").write_text(json.dumps({"failed": {"status": "running"}}))
    assert economy.adopt(runs) == [], "a completed vector can precede the final checks and process exit"


def test_stress_cells_resolve_to_the_common_economy_and_explicit_interventions():
    from alberta_buck.sim import experiment as expmod
    stress                      = economy.cells("stress")
    assert len(stress) == len({c.name for c in stress}) == 32
    for cell in stress:
        exp                     = expmod.load(economy.repo_root() / economy.EXPERIMENT, cell.sets(CENTRE))
        assert len(exp.interventions) == 1 and exp.interventions[0]["day"] == 730
        assert exp.scenario["agents"]["HouseholdPopulationAgent"] == 1
        assert exp.scenario["agents"]["FatCreditBorrowerAgent"] == 0
        assert exp.scenario["agents"]["ShockAgent"] == int(exp.interventions[0]["action"] == "arm_shock")


def test_nonzero_worker_exit_fails_even_with_a_completed_vector(tmp_path, monkeypatch):
    from types import SimpleNamespace
    source                      = tmp_path / economy.EXPERIMENT
    source.parent.mkdir(parents=True)
    source.write_text('[agents.HouseholdPopulationAgent]\ndebtor_rate=6\nsaver_rate=6\n')
    monkeypatch.setattr(economy, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(economy, "freeze", lambda root, base: root)
    monkeypatch.setattr(economy, "environment", lambda root: {})
    def process(command, **kwargs):
        if "--out" in command:
            path                = economy.Path(command[command.index("--out") + 1])
            path.write_text(json.dumps({"meta": {"run": {"completed": True}}}))
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(economy.subprocess, "run", process)
    args                        = SimpleNamespace(campaign="failed", cells="centre", seed=None, days=30, jobs=1)
    assert economy.run(args) == 1
    base                        = economy.campaign_path(tmp_path, "failed")
    assert not list((base / "runs").glob("*.json"))
    assert all(s["status"] == "failed" for s in json.loads((base / "status.json").read_text()).values())
    args.days                   = 1827
    with pytest.raises(ValueError, match="horizon changed"):
        economy.run(args)
