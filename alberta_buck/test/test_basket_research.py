"""Accounting identities, causal shadow diagnostics and pinned historical feeds."""
import copy
import csv
import math

import numpy as np
import pytest

from alberta_buck.sim.control_lab import historical
from alberta_buck.sim.control_lab.basket_research import ARMS, HISTORICAL, attribution
from alberta_buck.sim.control_lab.historical import historical_inputs
from alberta_buck.sim.control_lab.interactive import basket_performance
from alberta_buck.sim.control_lab.model import DEFAULTS, feed_rows, dependencies
from alberta_buck.sim.control_lab.noise import LOG_TICK, NoiseComparison, RouteFilter, shadow_frames


def test_historical_bridge_keeps_endpoints_and_past_only_reference():
    cfg=copy.deepcopy(DEFAULTS)
    data=historical_inputs(cfg,HISTORICAL)
    rows=data["rows"]
    assert len(rows)==481
    assert rows[0]["target_usd"]==rows[-1]["target_usd"]
    assert len(data["history"])==1280
    assert all(r["day"]<0 for r in data["history"])
    means=np.mean([r["prices"] for r in data["history"]],axis=0)
    assert means==pytest.approx([100,100,100],abs=1e-6)
    assert rows[-1]["historical_unadjusted_usd"]!=rows[0]["historical_unadjusted_usd"]
    assert feed_rows(cfg,HISTORICAL)==rows
    assert dependencies(cfg,HISTORICAL,"basket")["historical_source"]==data["provenance"]
    for i,change in enumerate(data["provenance"]["removed_log_change"]):
        f=rows[200]
        assert f["target_usd"][i]==pytest.approx(f["historical_unadjusted_usd"][i]*math.exp(-f["day"]/120*change),abs=2e-6)


def test_historical_future_changes_scenario_but_not_bootstrap(tmp_path,monkeypatch):
    monkeypatch.setattr(historical,"PRICE_DIR",tmp_path)
    cfg=copy.deepcopy(DEFAULTS);cfg["days"]=10
    profile=dict(name="history",history=dict(files=["a.csv","b.csv","c.csv"],start_day=1280))
    def write(last):
        for i,name in enumerate(profile["history"]["files"]):
            with (tmp_path/name).open("w") as f:
                w=csv.writer(f);w.writerow(["day","close_usd_micro"])
                for day in range(1291):w.writerow([day,int((100+i+day*.01+(last if day==1290 else 0))*1e6)])
    write(0);a=historical_inputs(cfg,profile);key=dependencies(cfg,profile,"basket")
    write(10);b=historical_inputs(cfg,profile)
    assert a["history"]==b["history"]
    assert a["provenance"]["historical_means_usd"]==b["provenance"]["historical_means_usd"]
    assert a["rows"][0]["target_usd"]==b["rows"][0]["target_usd"]
    assert a["rows"][1]["target_usd"]!=b["rows"][1]["target_usd"]
    assert key!=dependencies(cfg,profile,"basket")


def test_noise_proxies_distinguish_shared_noise_from_route_disagreement():
    rng=np.random.default_rng(914)
    quiet=NoiseComparison();noisy=NoiseComparison();independent=NoiseComparison()
    for i in range(601):
        day=i/4;level=np.array([.002*day,-.001*day,0.])
        a=level+rng.normal(0,.008,3);b=level+rng.normal(0,.008,3)
        q=quiet.update(day,[level,level]);n=noisy.update(day,[a,a]);d=independent.update(day,[a,b])
    assert q["ready"] and n["ready"]
    assert max(p["roughness_bp"] for p in q["pairs"])<1e-8
    assert all(70<p["roughness_bp"]<180 for p in n["pairs"])
    assert all(p["disagreement_bp"]==0 for p in n["pairs"])
    assert all(p["disagreement_bp"]>60 for p in d["pairs"])
    # Identical venues cannot double confidence by masquerading as two votes.
    for p in n["pairs"]:
        assert p["high_bp_day"]-p["rate_bp_day"]==pytest.approx(2*p["route_rate_sd_bp_day"][0])


def test_kalman_preserves_pair_identities_covariance_and_causality():
    rng=np.random.default_rng(17);a=RouteFilter();b=RouteFilter()
    for i in range(100):
        day=i*.25+(i%2)*.025
        logs=np.sin(np.arange(3)+day*.2)*.1+rng.normal(0,.002,3)
        x=a.update(day,logs);y=b.update(day,logs)
        assert x==y
        assert x["level"][1]==pytest.approx(x["level"][0]+x["level"][2],abs=1e-12)
        assert x["rate"][1]==pytest.approx(x["rate"][0]+x["rate"][2],abs=1e-12)
        assert np.min(np.linalg.eigvalsh(a.p))>=-1e-15
        assert np.min(np.linalg.eigvalsh(a.r))>0
    past=copy.deepcopy(x)
    a.update(25.,[1,2,3]);b.update(25.,[3,2,1])
    assert x==past  # later inputs never revise a published observation
    with pytest.raises(ValueError,match="advance time"):a.update(25.,[1,2,3])


def test_actual_arb_income_is_already_inside_equity_not_added_twice():
    result=dict(config=DEFAULTS,frames=[dict(buck_usdc=1.02,basket=dict(equity=1_010_000,
        arb_income=dict(beneficiary="basket",net_buck=8_000,net_usd=8_080)))])
    p=basket_performance(result)
    assert p["final_net_buck"]==1_010_000
    assert p["gain_buck"]==10_000 and p["other_gain_buck"]==2_000
    assert p["arb_income_usd"]==8_080
    assert p["other_gain_usd"]+p["arb_income_usd"]==pytest.approx(p["gain_usd"])


def test_shadow_reads_interval_cumulatives_handles_wrap_and_resets_on_missing_route():
    metadata=[dict(token0=i%2==0,decimal_offset_ticks=500) for i in range(6)]
    frames=[]
    for step in range(14):
        samples=[]
        for i,m in enumerate(metadata):
            tick=(1000*(i%3+1)-500)*(1 if m["token0"] else -1)
            cumulative=((1<<55)-100+step*21600*tick+(1<<55))%(1<<56)-(1<<55)
            samples.append(dict(cumulative=str(cumulative),tick=999999))
        frames.append(dict(day=step/4,pair_signal=dict(ready_mask=3,pool_samples=samples,
            venues=[dict(endpoint=step*21600,readable=True,midpoint_day=(step-.5)/4) for _ in range(2)])))
    result=dict(config=DEFAULTS,observer_routes=metadata,observer_params=dict(max_gap=86400),frames=frames)
    out=shadow_frames(result)
    assert out[0] is None and out[-1]["ready"]
    assert out[1]["pairs"][0]["level_bp"]==pytest.approx(-1000*LOG_TICK*10000)
    assert all(abs(p["rate_bp_day"])<1e-8 for p in out[-1]["pairs"])
    changed=copy.deepcopy(result)
    # Post-trade spots are irrelevant to the interval estimator.
    for f in changed["frames"]:
        for sample in f["pair_signal"]["pool_samples"]:sample["tick"]=-888888
    assert shadow_frames(changed)==out
    changed["frames"][-1]["pair_signal"]["venues"][1]["readable"]=False
    after=shadow_frames(changed)
    assert after[:-1]==out[:-1] and after[-1] is None
    repeated=copy.deepcopy(result)
    repeated["frames"].insert(10,copy.deepcopy(frames[9]))
    duplicate=shadow_frames(repeated)
    assert duplicate[9]==duplicate[10] and duplicate[11:]==out[10:]


def test_factorial_attribution_keeps_the_interaction_and_currency_effects():
    values=[(1_001_000,1),(1_003_000,1.01),(1_006_000,.99),(1_011_000,1.02)]
    runs={}
    for (arm,(_,owner,_)),(equity,fx) in zip(ARMS.items(),values):
        cfg=copy.deepcopy(DEFAULTS);cfg["market"]["arb_beneficiary"]=owner
        runs[arm]=dict(config=cfg,profile=dict(name="test"),frames=[dict(day=0,target_usd=[100]*3,buck_usdc=fx,basket=dict(equity=equity))])
    f=attribution(runs)["frames"][0]
    assert f["buck"]==dict(baseline=1000,rebalancing=2000,arb_ownership=5000,interaction=3000,total=11000)
    assert f["usd"]["total"]==pytest.approx(31_220)
    for currency in ("buck","usd"):
        assert sum(v for k,v in f[currency].items() if k!="total")==pytest.approx(f[currency]["total"])
    assert "reference" not in f
    # With each arm's book recorded, the same decomposition holds at reference
    # prices, and is indifferent to each arm's own BUCK/USDC exchange rate.
    for r,held in zip(runs.values(),(10_010,10_030,10_060,10_110)):
        frame=r["frames"][0]
        frame.update(token_buck=[100]*3,reference=dict(target_usd=1.))
        frame["basket"].update(idle=[held,0,0],liquidity=["0"]*3,account=0.,relief=0.)
    f=attribution(runs)["frames"][0]
    assert f["reference"]==pytest.approx(dict(baseline=1000,rebalancing=2000,arb_ownership=5000,interaction=3000,total=11000))
    assert f["usd"]["total"]==pytest.approx(31_220)
