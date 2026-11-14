"""Live sessions through the dashboard's API (alberta_buck.sim.dashboard, server.Session): a real
economy world on pyrevm, started paused, steered by its operator, exported and replayed.

Operator actions that change the world are interventions applied through the experiment engine
(experiment.Interventions.apply_now) and recorded with source "operator"; the exported experiment
schedules them on the same days, so a replay applies the same actions.
"""
import json
import threading
import time
import tomllib
import urllib.error
import urllib.request

import pytest

from alberta_buck.sim import dashboard, runrecord
from alberta_buck.sim import experiment as expmod
from alberta_buck.sim.credit_households import LIVE_SETTINGS, apply_live_settings


def test_live_settings_change_all_or_nothing():
    from types import SimpleNamespace
    a = SimpleNamespace(p=dict(utilization=.7, theta=1., surplus_policy="adaptive"), theta=1., surplus_policy="adaptive")
    note = apply_live_settings(a, dict(utilization=.9, theta=2.5, surplus_policy="retire"))
    assert a.p["utilization"] == .9 and a.theta == 2.5 and a.surplus_policy == "retire" and "theta 2.5" in note
    for bad in (dict(utilization=1.5), dict(home_k=900), dict(surplus_policy="spend"), dict(utilization=.5, theta=-1)):
        with pytest.raises(ValueError):
            apply_live_settings(a, bad)
    assert a.p["utilization"] == .9, "a refused change leaves every setting as it was"
    assert set(LIVE_SETTINGS) >= {"utilization", "bvib_tolerance", "decision_days", "retire_disc"}


def test_experiment_toml_round_trips():
    exp = expmod.load(dashboard.EXPERIMENTS / "economy.toml", sets=["scenario.prices=revert", "scenario.days=4"])
    cfg = json.loads(json.dumps(exp.cfg, default=str))
    cfg["interventions"] = [dict(day=2, action="add_household", role="saver", source="operator",
                                 settings=dict(utilization=.8, home_k=[300., 400.],
                                               bvib_tolerance=dict(mean=.01, sd=.005, lo=0., hi=.02)))]
    back = tomllib.loads(expmod.to_toml(cfg))
    strip = lambda o: ({k: strip(v) for k, v in o.items() if v is not None} if isinstance(o, dict)
                       else [strip(x) for x in o] if isinstance(o, list) else o)
    assert back == strip(cfg)


def _wait(fn, timeout=240, every=.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(every)
    raise TimeoutError("condition not met")


def test_a_live_session_is_steered_recorded_and_exported(tmp_path):
    server = dashboard.serve(tmp_path, port=0, static=tmp_path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body=None, *, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, headers={
            "Content-Type": "application/json", **(headers or {})} if data is not None else {})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw.decode()

    try:
        assert "economy" in call("/api/experiments")
        for body, headers in (({"experiment": "economy"}, {"Content-Type": "text/plain"}),
                              ({"experiment": "economy"}, {"Origin": "http://evil.example"}),
                              ({"experiment": "nope"}, None)):
            with pytest.raises(urllib.error.HTTPError) as e:
                call("/api/sessions", body, headers=headers)
            assert e.value.code in (400, 403)
        started = call("/api/sessions", {"experiment": "economy", "days": 4, "prices": "revert", "paused": True})
        sid, run = started["sid"], started["run"]
        status = _wait(lambda: (s := call(f"/api/sessions/{sid}"))["paused"] and s["day"] == 0 and s)
        assert status["run"] == run and not status["done"] and status["error"] is None
        manifest = call(f"/api/runs/{run}/manifest")
        assert manifest["kind"] == "session" and manifest["capabilities"]["control"] == f"/api/sessions/{sid}"

        members = call(f"/api/sessions/{sid}/members")
        assert members and {"member", "role", "settings"} <= set(members[0])
        target = next(m["member"] for m in members if m["role"] == "debtor")
        done = status["ops"]                    # the opening pause
        for op in ({"op": "intervene", "intervention": {"action": "add_household", "role": "saver",
                                                         "settings": {"home_k": 400.0, "utilization": 0.8}}},
                   {"op": "intervene", "intervention": {"action": "household_settings", "member": target,
                                                         "settings": {"utilization": 0.7, "surplus_policy": "retire"}}},
                   {"op": "shock", "side": "sell", "usd": 1e6, "days": 2}):
            assert call(f"/api/sessions/{sid}/control", op)["ok"]
            done += 1
            _wait(lambda: call(f"/api/sessions/{sid}")["ops"] >= done)        # applied while paused
        last = call(f"/api/sessions/{sid}")["last"]
        assert last["op"] == "shock" and last["ok"] is False and "ShockAgent" in last["note"], \
            "economy.toml casts no ShockAgent: the refusal is reported, not raised"
        members = {m["member"]: m for m in call(f"/api/sessions/{sid}/members")}
        assert "0/saver/op0" in members and members[target]["settings"]["surplus_policy"] == "retire"

        assert call(f"/api/sessions/{sid}/control", {"op": "step", "days": 2})["ok"]
        _wait(lambda: (s := call(f"/api/sessions/{sid}"))["paused"] and s["day"] >= 2)
        frames = call(f"/api/runs/{run}/frames")
        assert [f["day"] for f in frames] == [0, 1] and frames[-1]["population"]["roles"]["saver"]["arrived"] == 7

        exported = call(f"/api/sessions/{sid}/experiment")
        cfg = tomllib.loads(exported)
        ops = [iv for iv in cfg["interventions"] if iv.get("source") == "operator"]
        assert [iv["action"] for iv in ops] == ["add_household", "household_settings"], "failed actions are not exported"
        assert all(iv["day"] == 0 for iv in ops) and cfg["scenario"]["prices"] == "revert"
        (tmp_path / "replay.toml").write_text(exported)
        assert expmod.load(tmp_path / "replay.toml").interventions[-1]["member"] == target

        # A reset rebuilds the world from day 0 under a new record; the old one says so.
        assert call(f"/api/sessions/{sid}/control", {"op": "reset"})["ok"]
        again = _wait(lambda: (x := call(f"/api/sessions/{sid}"))["run"] == f"live-{sid}-r1" and x)
        assert call(f"/api/runs/{run}/manifest")["status"] == "reset" and again["day"] == 0
        assert call(f"/api/sessions/{sid}/control", {"op": "stop"})["ok"]
        _wait(lambda: call(f"/api/sessions/{sid}")["done"])
        m = _wait(lambda: (x := call(f"/api/runs/live-{sid}-r1/manifest"))["status"] != "running" and x)
        assert m["status"] == "stopped"
        ivs, _ = runrecord.read_lines(tmp_path / run / "interventions.jsonl")
        assert [(r["action"], r["source"], r.get("ok")) for r in ivs if r.get("replay", True)] == [
            ("add_household", "operator", True), ("household_settings", "operator", True), ("arm_shock", "operator", False)]
        assert [r["action"] for r in ivs if r.get("replay") is False] == ["pause", "step", "reset"]
        with pytest.raises(urllib.error.HTTPError) as e:
            call(f"/api/sessions/{sid}/control", {"op": "resume"})
        assert e.value.code == 409
    finally:
        for s in (server.live.sessions.values() if server.live else []):
            if not s.done:
                s.controls.put({"op": "stop"})
        server.shutdown()
        server.server_close()


def test_live_sessions_need_a_loopback_host(tmp_path):
    server = dashboard.serve(tmp_path, host="0.0.0.0", port=0, static=tmp_path)
    try:
        assert server.live is None
    finally:
        server.server_close()
