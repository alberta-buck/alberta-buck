"""Completed vectors produce PDFs; checkpoints and failed rendering cannot masquerade as finished reports."""

import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from alberta_buck.sim import report
from alberta_buck.sim.snapshot import Snapshotter, household_summary
from alberta_buck.sim.plot_rebalancing import household_position, machine_weights


def test_report_uses_machine_targets_and_open_positions_not_legacy_counters():
    f                           = {"eq_w": [2500, 7500], "eq_lean": [3000, 7000],
                                   "poolWeights": [[.1, .9], [.9, .1]],
                                   "octl": [dict(member="a", active=True, basket=100, basket_mark=120),
                                            dict(member="b", active=True, basket=50, basket_mark=45)]}
    assert machine_weights(f, 2) == [(.25, .3), (.75, .7)]
    assert household_position(f) == (150, 165, pytest.approx(.1))
    f["octl"][1]["basket_mark"] = None
    assert household_position(f) == (150, None, None)
    f["basket_positions"] = [{"basis": 50, "mark": 75}]
    assert household_position(f) == (150, None, None)  # Do not mask an unavailable household mark.
    f["octl"] = []
    assert household_position(f) == (50, 75, .5)
    f["basket_positions"][0]["mark"] = None
    assert household_position(f) == (50, None, None)
    gift = dict(member="gift", role="investor", active=True, basket=0, basket_at_risk=0,
                basket_mark=7, basket_receipts=1, mark_block=12, aux_cash=19)
    assert household_position({"octl": [gift]}) == (0, 7, None)
    summary = household_summary([gift])
    assert summary["basket_mtm"] == 7 and summary["basket_roi"] is None
    assert summary["aux_cash"] == 19 and summary["mark_basis"] == "receipts"


@pytest.fixture
def snapshot():
    # Use the real saved schema and writer, with just two observations to keep rendering fast.
    source                      = Path(__file__).resolve().parents[2] / "test/vectors/rebalancing-sim.json"
    data                        = json.loads(source.read_text())
    snap                        = Snapshotter.__new__(Snapshotter)
    snap.tokens                 = data["tokens"]
    snap.d                      = SimpleNamespace(dec=data["decimals"])
    snap.s                      = SimpleNamespace(days=1827)
    snap.meta                   = {"run": {"completed": False, "days": 1827}}
    snap.frames                 = [data["frames"][0], data["frames"][-1]]
    for day, frame in zip((0, 1826), snap.frames):
        frame["day"]            = day
        frame["octl"]           = [{"idx": 0, "theta": 1., "pattern": "salary", "nw": 0, "hypo": 0,
                                    "mortgage": 0, "drawn": 1000000, "limit": 2000000, "deploys": 1,
                                    "active": True}]
    return snap


def test_five_year_completion_writes_a_real_multipage_pdf(snapshot, tmp_path):
    snapshot.meta["buck_contract"] = {"artifact": "Buck", "accounting": "legacy-signed", "address": "0x123"}
    path, pdf                   = report.finish(snapshot, tmp_path / "five.years.partial")
    assert json.loads(path.read_text())["meta"]["run"]["completed"] is True
    assert pdf == tmp_path / "five.years.pdf"
    raw                         = pdf.read_bytes()
    assert raw.startswith(b"%PDF-") and raw.rstrip().endswith(b"%%EOF")
    assert len(re.findall(rb"/Type /Page\b", raw)) == 4
    assert b"Vector SHA256:" in raw
    assert b"BUCK accounting: legacy-signed" in raw
    assert report.pdf_path(path.with_suffix(".json")) == pdf


def test_accounting_label_does_not_guess_missing_or_partial_metadata():
    assert report.buck_accounting({}) == "unrecorded"
    assert report.buck_accounting({"meta": {"buck_contract": None}}) == "unrecorded"
    assert report.buck_accounting({"meta": {"buck_contract": {"artifact": "Buck"}}}) == "unrecorded"
    data                        = {"meta": {"buck_contract": {"artifact": "BuckNet", "accounting": "net-facility"}}}
    assert report.buck_accounting(data) == "net-facility (BuckNet)"


def test_automatic_paths_are_unique_for_long_runs_only(monkeypatch, tmp_path):
    monkeypatch.setattr(report, "DEFAULT_OUTPUT_DIR", tmp_path)
    first                       = report.output_path(None, report.FIVE_YEARS)
    second                      = report.output_path(None, report.FIVE_YEARS)
    assert first.parent == second.parent == tmp_path and first != second
    assert first.suffix == second.suffix == ".json"
    assert report.output_path(None, report.FIVE_YEARS - 1) is None
    assert report.output_path("chosen.json", report.FIVE_YEARS) == "chosen.json"


def test_concurrent_completed_worlds_render_separate_valid_pdfs(snapshot, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    snapshot.meta["run"]["completed"] = True
    paths                       = []
    for artifact in ("Buck", "BuckWithBasketHooks"):
        snapshot.meta["buck_contract"] = {"artifact": artifact, "accounting": "legacy-signed"}
        paths.append(snapshot.write(tmp_path / f"{artifact}.json"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        records                 = list(pool.map(report.render, paths))
    for artifact, result in zip(("Buck", "BuckWithBasketHooks"), records):
        assert result["buck_accounting"] == f"legacy-signed ({artifact})"
        raw                     = Path(result["pdf"]).read_bytes()
        assert raw.startswith(b"%PDF-") and raw.rstrip().endswith(b"%%EOF")
        assert artifact.encode() in raw


def test_interactive_completion_preserves_json_and_retries_a_failed_pdf(snapshot, tmp_path, monkeypatch):
    import random
    from alberta_buck.sim.driver import SimDriver

    snapshot.meta["run"]["mode"] = "interactive"
    snapshot.s.days             = report.FIVE_YEARS
    snapshot.capture            = lambda *args: None
    driver                      = SimDriver.__new__(SimDriver)
    driver.snap                 = snapshot
    driver.scenario             = SimpleNamespace(days=report.FIVE_YEARS, ticks_per_day=1)
    driver.day, driver.tick     = report.FIVE_YEARS - 1, 0
    driver.done                 = False
    driver.ts, driver.tick_secs = 0, 1
    driver.timeline_epoch = driver.ts + driver.tick_secs
    driver.anvil                = SimpleNamespace(warp_to=lambda ts: None)
    chain                       = SimpleNamespace(clear_balance_cache=lambda: None, send=lambda operation: None)
    kctrl                       = SimpleNamespace(functions=SimpleNamespace(compute=lambda: None))
    driver.d                    = SimpleNamespace(chain=chain, kctrl=kctrl)
    driver._begin_day           = lambda: None
    driver._whale_tick          = -1
    driver.whales, driver.arbs, driver.agents = [], [], []
    driver._prng                = random.Random(1)
    driver.ctr                  = {}
    driver.init_val = driver.reb_init = driver.dm_init = 0
    driver.proxy                = SimpleNamespace(bump=lambda: None)
    driver.report_path          = tmp_path / "interactive.json"
    driver.report_json = driver.report_pdf = driver.report_error = None
    render                      = report.render
    def broken(path):
        raise RuntimeError("test renderer failure")
    monkeypatch.setattr(report, "render", broken)
    assert driver.step_tick() is False and driver.done
    assert "test renderer failure" in driver.report_error
    assert json.loads(driver.report_path.read_text())["meta"]["run"]["completed"] is True
    monkeypatch.setattr(report, "render", render)
    assert driver.write() == driver.report_path
    assert driver.report_error is None and driver.report_pdf.exists()


def test_checkpoint_is_not_a_completed_report(snapshot, tmp_path):
    path                        = snapshot.write(tmp_path / "checkpoint.json")
    with pytest.raises(ValueError, match="no completed run"):
        report.render(path)
    assert not report.pdf_path(path).exists()


def test_adaptive_households_get_the_policy_page(snapshot, tmp_path):
    for frame in snapshot.frames:
        frame["octl"][0]["surplus_policy"] = "adaptive"
    _, pdf                      = report.finish(snapshot, tmp_path / "households.json")
    assert len(re.findall(rb"/Type /Page\b", pdf.read_bytes())) == 5


def test_old_savings_vector_needs_its_full_configured_horizon(snapshot, tmp_path):
    snapshot.meta               = {"experiment": {"scenario": {"years": 5., "day_step": 1}}}
    snapshot.frames[-1]["day"]   = 1825
    path                        = snapshot.write(tmp_path / "old.json")
    with pytest.raises(ValueError, match="no completed run"):
        report.render(path)
    snapshot.frames[-1]["day"]   = 1826
    snapshot.write(path)
    assert report.render(path)["pages"] == 4


def test_failed_pdf_preserves_completed_json_and_previous_report(snapshot, tmp_path, monkeypatch):
    from alberta_buck.sim import plot_equilibrium

    def broken(*args, **kwargs):
        raise RuntimeError("renderer failed")

    monkeypatch.setattr(plot_equilibrium, "render", broken)
    path                        = tmp_path / "saved.json"
    pdf                         = report.pdf_path(path)
    pdf.write_bytes(b"previous report")
    with pytest.raises(RuntimeError, match="without rerunning the simulation"):
        report.finish(snapshot, path)
    assert json.loads(path.read_text())["meta"]["run"]["completed"] is True
    assert pdf.read_bytes() == b"previous report"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["saved.json", "saved.pdf"]


def test_short_and_unsaved_runs_do_not_render(snapshot, tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("short or unsaved run tried to render")

    monkeypatch.setattr(report, "render", unexpected)
    assert report.finish(snapshot, None) == (None, None)
    snapshot.s.days             = 1824
    path, pdf                   = report.finish(snapshot, tmp_path / "short.json")
    assert path.exists() and pdf is None


def test_index_links_stay_relative_and_escape_filenames(tmp_path):
    data                        = tmp_path / 'a & b.json'
    record                      = {"vector": str(data), "pdf": str(report.pdf_path(data)), "pages": 3,
                                   "last_day": 1826, "bv_tail": 1.01, "k": .75, "fat_limit": 1234,
                                   "refinanced": 2, "cohort": 3}
    target                      = tmp_path / "index.html"
    report.write_index([record], target)
    text                        = target.read_text()
    assert 'href="a%20%26%20b.pdf"' in text
    assert "a &amp; b" in text
    assert "1.0100" in text and "2/3" in text
    assert "unrecorded" in text
    assert "interactive diagnostic" in text and "do not perform that teardown" in text
    record["buck_accounting"]    = "legacy-signed (Buck<fixture>)"
    report.write_index([record], target)
    assert "legacy-signed (Buck&lt;fixture&gt;)" in target.read_text()


def test_digest_decomposes_supply_and_summarises_households_from_recorded_frames(capsys):
    from alberta_buck.sim import digest
    member = lambda role, day, **kw: dict(member=f"0/{role}/0", role=role, drawn=0, basket=0, held_buck=0, nw=0, hypo=0,
                                          action="save", error="", relief_unavailable=None, exited=False) | kw
    frames = [dict(day=d, buckK=int((.75 - .01 * d) * 1e18), basketVal=int((1 + .01 * (d % 2)) * 1e18),
                   buck_usd=1_000_000, supply=(100 + 3 * d) * 10**6, eq_lien=(50 + 2 * d) * 10**6,
                   households=dict(drawn=d * 10**6),
                   octl=[member("debtor", d, drawn=d * 10**6, nw=-5 * 10**6, hypo=-4 * 10**6,
                                error="tx reverted: deposit" if d == 3 else ""),
                         member("investor", d, basket=7 * 10**6, held_buck=2 * 10**6, action="buy",
                                relief_unavailable="MarkPriceDeviation" if d == 1 else None)])
              for d in range(7)]
    frames[-1]["household_faults"] = 1
    carrying = lambda pool, tokens, basket: dict(carrying=dict(
        buck_usdc=dict(nominal_scaled=str(pool * 10**42)), basket=dict(nominal_scaled=str(basket * 10**42)),
        buck_tokens=[dict(nominal_scaled=str(t * 10**42)) for t in tokens]))
    frames[0]["net"], frames[-1]["net"] = carrying(40, [20, 10], 0), carrying(39, [25, 14], 3)
    d = digest.digest(dict(frames=frames))
    assert d["holders"]["first"] == dict(dollar_pool=40., token_pools=30., basket=0., others=30.)
    assert d["holders"]["last"] == dict(dollar_pool=39., token_pools=39., basket=3., others=37.)
    assert digest.holders(frames[1]) is None and d["unbacked"] == 118.
    frames[-1]["net"].update(active_principal=110 * 10**6, deferred_principal=8 * 10**6)
    assert digest.digest(dict(frames=frames))["unbacked"] == 0. and digest.digest(dict(frames=frames[:2]))["unbacked"] is None
    assert d["days"] == 7 and d["households"] == 2 and d["observations"] == 14
    assert d["k"]["first"] == .75 and abs(d["k"]["last"] - .69) < 1e-12
    assert d["supply"] == dict(change=18.0, basket_lien=12.0, household_principal=6.0, other=0.0)
    assert abs(d["bvib"]["above_one"] - 3 / 7) < 1e-12 and d["bvib"]["high"] == 1.01
    assert d["roles"]["debtor"]["vs_counterfactual"] == [-1] and d["roles"]["investor"]["held_buck"] == 2
    assert d["errors"] == 1 and d["relief_unavailable"] == 1 and d["faults"] == 1
    assert [p["day"] for p in d["path"]] == [0, 1, 2, 3, 4, 5, 6]
    with pytest.raises(ValueError, match="no frames"):
        digest.digest(dict(frames=[]))
    assert digest.main([]) == 2
    text = digest.render("v", d)
    assert "supply +18 BUCK = basket lien +12 + household principal +6 + other +0" in text
    assert "held: BUCK/USDC pool 40 -> 39; TOKEN/BUCK pools 30 -> 39; basket 0 -> 3; everyone else 30 -> 37" in text
