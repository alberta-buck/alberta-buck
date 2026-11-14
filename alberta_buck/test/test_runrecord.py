"""Run records and the dashboard's server (alberta_buck.sim.runrecord, alberta_buck.sim.dashboard).

No EVM: snapshot frames are synthetic. test/fixtures/run-record.json pins the frame shape for both
languages; core/js/test/dashboard-series.test.js reads the same records.
"""
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from alberta_buck.sim import dashboard, digest, runrecord

FIXTURE = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/run-record.json").read_text())


def test_frame_record_groups_a_snapshot_frame_by_component():
    tokens = FIXTURE["tokens"]
    for flat, want in zip(FIXTURE["flat"], FIXTURE["frames"]):
        assert runrecord.frame_record(flat, tokens) == want
    f = FIXTURE["frames"][2]
    held = f["supply"]["holders"]
    assert sum(held.values()) == f["supply"]["total"], "everyone else is supply less the Carrying accounts"
    assert held == dict(dollar_pool=14_800_000, token_pools=15_600_000, basket=250_000, others=11_850_000)
    assert f["health"] == dict(errors=2, relief_unavailable=0, faults=1, events=2)
    assert f["jubilee"]["relief_available"] == 1_500_000 and f["control"] == dict(k=.747, bvib=1.02, pid=dict(p=-100, i=-2000, d=3))
    assert f["market"]["tokens"][1] == dict(symbol="CNST", ref_usd=7.8, usdc=7.79, buck=7.81)
    # A world without the canonical reads or households records their absence, never a zero.
    bare = runrecord.frame_record(dict(day=4, supply=10, buckK=7 * 10**17), [])
    assert bare["supply"]["holders"] is None and bare["jubilee"] is None and bare["households"] is None
    assert bare["population"] is None and bare["health"] == dict(errors=None, relief_unavailable=None, faults=0, events=0)
    assert runrecord.frame_record(dict(day=0, supply=2**60), [])["supply"]["total"] == str(2**60), "past 2^53 as a string"


def test_recorder_writes_a_growing_run_that_reads_like_a_finished_one(tmp_path):
    rec = runrecord.RunRecorder(tmp_path, "economy test", kind="test", engine="pyrevm")
    assert rec.id.startswith("economy-test-")
    applied = []
    rec.open(meta=dict(run=dict(scenario="eq", seed=7, days=3), buck_contract=dict(
        address="0xB", roles=dict(buck=dict(address="0xB", code_hash="0x1"), basket=dict(address="0xC")))),
        tokens=FIXTURE["tokens"])
    m = json.loads((rec.dir / "manifest.json").read_text())
    assert m["schema"] == runrecord.SCHEMA and m["status"] == "running" and m["kind"] == "test"
    assert m["contracts"] == dict(buck="0xB", basket="0xC") and m["scenario"]["seed"] == 7 and m["frames"] == 0
    for flat in FIXTURE["flat"]:
        if flat["day"] == 1:
            applied.append(dict(action="price_shock", applied_day=1, ok=True, note="x"))
        rec.frame(flat, applied=applied)
    frames, offset = runrecord.read_lines(rec.dir / "frames.jsonl")
    assert frames == FIXTURE["frames"]
    with open(rec.dir / "frames.jsonl", "a") as f:     # a writer caught mid-line
        f.write('{"day": 3, "con')
    again, offset2 = runrecord.read_lines(rec.dir / "frames.jsonl", offset)
    assert again == [] and offset2 == offset, "a partial last line is left for the next read"
    ivs, _ = runrecord.read_lines(rec.dir / "interventions.jsonl")
    assert ivs == [dict(day=1, source="scenario", action="price_shock", applied_day=1, ok=True, note="x")]
    events, _ = runrecord.read_lines(rec.dir / "events.jsonl")
    assert [(e["day"], e["kind"]) for e in events] == [(0, "surplus"), (1, "surplus"), (2, "act"), (2, "surplus")]
    agents, _ = runrecord.read_lines(rec.dir / "agents.jsonl")
    assert len(agents) == 6 and agents[-1]["error"] == "tx reverted" and "note" not in agents[-1]
    rec.intervene(2, "population", dict(cls="HouseholdInvestorAgent", count=5))
    rec.close("complete", outputs=dict(vector="v.json", pdf=None))
    m = json.loads((rec.dir / "manifest.json").read_text())
    assert m["status"] == "complete" and m["frames"] == 3 and m["outputs"] == dict(vector="v.json") and m["finished"]
    assert runrecord.read_lines(rec.dir / "interventions.jsonl")[0][-1]["source"] == "operator"
    assert [r["id"] for r in runrecord.runs(tmp_path)] == [rec.id]
    with pytest.raises(FileExistsError):
        runrecord.RunRecorder(tmp_path, run_id=rec.id).open()     # a complete record is never replaced
    twin = runrecord.RunRecorder(tmp_path, "economy test")
    twin.id, twin.dir = rec.id, rec.dir                          # same name, same second
    assert twin.open().id == rec.id + "-2"
    failed = runrecord.RunRecorder(tmp_path, run_id="cell").open()
    failed.close("failed", error="boom")
    retry = runrecord.RunRecorder(tmp_path, run_id="cell").open()
    assert retry.dir.name == "cell" and (tmp_path / "cell-earlier-1" / "manifest.json").exists()


def test_an_events_accounts_are_written_only_when_they_change(tmp_path):
    """Recommendation 57: an event repeats its household's account snapshot most of the time; the
    record writes it once and a reader carries it forward."""
    rec = runrecord.RunRecorder(tmp_path, run_id="accounts").open()
    a = dict(buck="10", issued="5")
    b = dict(buck="12", issued="5")
    ev = lambda seq, member, accounts: dict(agent="H#1", member=member, seq=seq, kind="act", accounts=accounts)
    rec.frame(dict(day=0, household_events=[ev(1, "0/debtor/0", a), ev(2, "0/debtor/0", a), ev(1, "0/saver/0", a)]))
    rec.frame(dict(day=1, household_events=[ev(3, "0/debtor/0", a), ev(4, "0/debtor/0", b), ev(2, "0/saver/0", b)]))
    rec.close()
    events, _ = runrecord.read_lines(rec.dir / "events.jsonl")
    assert [e.get("accounts") for e in events] == [a, None, a, None, b, b], "per household, only on change"
    assert all({"agent", "member", "seq", "kind", "day"} <= set(e) for e in events)
    carried, last = [], {}
    for e in events:                                   # what a reader reconstructs
        last[e["member"]] = e.get("accounts", last.get(e["member"]))
        carried.append(last[e["member"]])
    assert carried == [a, a, a, a, b, b]


def test_import_vector_replaces_only_its_own_view(tmp_path):
    vector = tmp_path / "eq-world.json"
    vector.write_text(json.dumps(dict(tokens=FIXTURE["tokens"], frames=FIXTURE["flat"], meta=dict(
        run=dict(completed=False, scenario="rebalancing-eq", seed=3, days=10), experiment=dict(name="economy"),
        interventions_applied=[dict(action="arm_shock", applied_day=2, ok=False, note="FAILED")]))))
    runs = tmp_path / "runs"
    out = runrecord.import_vector(vector, runs)
    m = json.loads((out / "manifest.json").read_text())
    assert out.name == "eq-world" and m["status"] == "incomplete" and m["scenario"]["name"] == "economy"
    assert m["revision"] is None and m["source"] == dict(vector=str(vector)) and m["frames"] == 3
    assert runrecord.read_lines(out / "interventions.jsonl")[0] == [dict(day=2, source="scenario", action="arm_shock",
                                                                         applied_day=2, ok=False, note="FAILED")]
    assert runrecord.import_vector(vector, runs) == out, "re-importing the same vector replaces its view"
    other = tmp_path / "elsewhere" / "eq-world.json"
    other.parent.mkdir()
    other.write_text(vector.read_text())
    with pytest.raises(FileExistsError):
        runrecord.import_vector(other, runs)


def test_dashboard_server_reads_runs_and_refuses_what_it_does_not_serve(tmp_path):
    rec = runrecord.RunRecorder(tmp_path, run_id="live").open(tokens=FIXTURE["tokens"])
    rec.frame(FIXTURE["flat"][0])
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>t</title>")
    server = dashboard.serve(tmp_path, port=0, static=static)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    get = lambda path: json.loads(urllib.request.urlopen(base + path, timeout=10).read())
    try:
        assert [r["id"] for r in get("/api/runs")] == ["live"]
        assert get("/api/runs/live/manifest")["status"] == "running"
        assert [f["day"] for f in get("/api/runs/live/frames")] == [0]
        for flat in FIXTURE["flat"][1:]:
            rec.frame(flat)                              # the run grows; the page asks for what is new
        assert [f["day"] for f in get("/api/runs/live/frames?since=0")] == [1, 2]
        assert get("/api/runs/live/frames?since=2") == [] and get("/api/runs/live/interventions") == []
        latest = {r["member"]: r for r in get("/api/runs/live/agents")}
        assert sorted(latest) == ["0/debtor/0", "0/debtor/1"] and latest["0/debtor/0"]["day"] == 2
        assert [r["day"] for r in get("/api/runs/live/agents?member=0/debtor/0")] == [0, 1, 2]
        assert [(e["day"], e["kind"]) for e in get("/api/runs/live/events?member=0/debtor/0")] == [
            (0, "surplus"), (1, "surplus"), (2, "act"), (2, "surplus")]
        assert get("/api/runs/live/events?member=nobody") == []
        assert b"<title>t</title>" in urllib.request.urlopen(base + "/", timeout=10).read()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/runs/live/events", timeout=10)
        assert e.value.code == 400
        for bad in ("/api/runs/nope/manifest", "/api/runs/..%2F..%2Fetc/manifest", "/api/runs/live/secrets"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad, timeout=10)
            assert e.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(ValueError, match="reserved"):
        dashboard.serve(tmp_path, port=8797)


def test_digest_counts_faults_from_dispatcher_events_when_the_counter_is_absent():
    d = digest.digest(dict(frames=FIXTURE["flat"]))
    assert d["faults"] == 1, "the fixture's one act event; frames before 2026-11-14 lack household_faults"
    frames = [dict(f) for f in FIXTURE["flat"]]
    frames[-1]["household_faults"] = 4
    assert digest.digest(dict(frames=frames))["faults"] == 4
