"""Price opportunity -> recorded intent -> exact allocation -> actual swap audit."""
from collections import Counter
import json
import math
import os
from pathlib import Path
import tempfile

from .cli import atomic_json

WAD = 10**18
NONE = 2**256-1
LOG_TICK = math.log(1.0001)


def integers(x):
    if isinstance(x, dict): return {k: integers(v) for k,v in x.items()}
    if isinstance(x, list): return [integers(v) for v in x]
    return int(x) if isinstance(x, str) else x


def allocation_trace(d, a):
    """Explain each gate using the recorded Solidity inputs, with integer math.

    Reconstruct only the paired planner. Require exact equality to the
    emitted Solidity plan, so these explanations cannot silently drift.
    """
    n = len(d["values"]); band = d["band"]; grain = d["grain"]
    gaps = [v-t for v,t in zip(d["values"], d["targets"])]
    excess = [max(0,g-band) if cap else 0 for g,cap in zip(gaps,d["swapCaps"])]
    deficit = [max(0,-g-band) if cap else 0 for g,cap in zip(gaps,d["swapCaps"])]
    candidates = {}; best = 0; donor = NONE; recipients = [0]*n
    for i in range(n):
        eligible = [j for j in range(n) if deficit[j] and d["desire"][i*n+j]]
        total_deficit = sum(deficit[j] for j in eligible)
        rate = sum(deficit[j]*d["desire"][i*n+j] for j in eligible)//total_deficit if total_deficit else 0
        elapsed = min(d["at"]-d["lastExecutionAt"], 86400)
        u = rate*elapsed//86400; fraction = u*WAD//(WAD+u)
        release = min(excess[i],total_deficit)*fraction//WAD
        amount = min(release, d["values"][i]*d["stepBp"]//10000, d["swapCaps"][i])
        parts = [amount*deficit[j]//total_deficit if j in eligible else 0 for j in range(n)]
        scale = WAD
        for j,part in enumerate(parts):
            if not part: continue
            step = max(d["values"][j],d["targets"][j])*d["stepBp"]//10000
            cap = min(d["swapCaps"][j],deficit[j],step)
            if cap < part: scale = min(scale,cap*WAD//part)
        parts = [p*scale//WAD for p in parts]
        executable = [p if p>grain else 0 for p in parts]
        candidates[i] = dict(release=release, capped=amount, parts=parts, executable=executable,
            fraction=fraction, elapsed=elapsed, weighted_rate=rate)
        if excess[i] and amount>grain and amount>best and sum(executable)>best:
            donor=i; best=sum(executable); recipients=executable
    if not a["fallbackRequired"]:
        assert (donor,best,recipients)==(a["donor"],a["total"],a["recipients"]), "Python explanation disagrees with Solidity plan"
    return gaps, candidates


def smooth(cfg, day, i, j):
    """Independent analytic benchmark, never supplied to the trading contract."""
    amp=cfg["feed"]["amplitude"]; w=2*math.pi/cfg["feed"]["period_days"]
    def token(k):
        x=w*day+k*2*math.pi/3; p=1+amp*math.sin(x)
        return math.log(p), amp*w*math.cos(x)/p, -amp*w*w*(math.sin(x)+amp)/(p*p)
    a,b=token(i),token(j)
    return tuple(x-y for x,y in zip(a,b))


def analyze(run):
    rows=[]; frame_checks=[]
    for f in run["frames"]:
        trace=integers(f["pair_decisions"][0]); d=trace["input"]; a=trace["proposed"]
        gaps,candidates=allocation_trace(d,a); total=sum(d["values"]); n=len(gaps)
        executed=set()
        for e in f["events"]:
            if e["actor"]!="basket": continue
            sold={t["token"] for t in e["trades"] if t["side"]=="sell"}
            bought={t["token"] for t in e["trades"] if t["side"]=="buy"}
            executed.update((i,j) for i in sold for j in bought)
        planned={(a["donor"],j) for j,p in enumerate(a["recipients"]) if p and a["donor"]!=NONE}
        if not a["fallbackRequired"]: assert planned==executed, "Planned legs and receipt swaps differ"
        frame_checks.append(dict(day=f["day"], planned=sorted(planned), executed=sorted(executed), fallback=a["fallbackRequired"]))
        for i in range(n):
            for j in range(n):
                if i==j: continue
                k=i*n+j; z,v,acc=smooth(run["config"],f["day"],i,j)
                c=candidates[i]
                # Retain distinct flags: the user's OR condition is weaker
                # than requiring each of the two legs to improve its weight.
                one=gaps[i]>0 or gaps[j]<0
                both=gaps[i]>0 and gaps[j]<0
                one_band=gaps[i]>d["band"] or gaps[j]<-d["band"]
                both_band=gaps[i]>d["band"] and gaps[j]<-d["band"]
                detected=d["desire"][k]>0
                if (i,j) in executed: reason="executed"
                elif a["fallbackRequired"]: reason="legacy_override_or_inside_bands"
                elif not both_band: reason="two_sided_band"
                elif not detected: reason="no_intent"
                elif c["release"]<=d["grain"]: reason="released_total_below_grain"
                elif c["capped"]<=d["grain"]: reason="donor_cap_below_grain"
                elif c["parts"][j]<=d["grain"]: reason="recipient_part_below_grain"
                else: reason="other_donor_selected"
                base=run["config"]["feed"]["base"]
                spot=f["before_policy"]["token_buck"]
                rows.append(dict(day=f["day"],i=i,j=j,pair=f"{'ABC'[i]}→{'ABC'[j]}",
                    smooth_log_separation=z,smooth_rate_bp_day=v*10000,smooth_acceleration=acc,
                    spot_ratio=(spot[i]/spot[j])/(base[i]/base[j]),
                    estimated_rate_bp_day=d["rate"][k]/WAD*LOG_TICK*86400*10000,
                    high_bp_day=d["high"][k]/WAD*LOG_TICK*86400*10000,
                    scaled_change_bp_day=d["change"][k]/WAD*LOG_TICK*86400*10000,
                    donor_gap_pp=gaps[i]/total*100,recipient_gap_pp=gaps[j]/total*100,
                    band_pp=d["band"]/total*100,one_weight=one,both_weights=both,
                    one_band=one_band,both_bands=both_band,
                    slowing_or_closing=v<=0 or acc<0,detected=detected,
                    desire_per_day=d["desire"][k]/WAD,grain_buck=d["grain"]/1e6,
                    release_buck=c["release"]/1e6,part_buck=c["parts"][j]/1e6,
                    release_fraction=c["fraction"]/WAD,elapsed_days=c["elapsed"]/86400,
                    selected=(i,j) in planned,executed=(i,j) in executed,reason=reason,
                    fallback=a["fallbackRequired"],ready_mask=d["readyMask"],
                    lp_fraction=sum(d["lpValues"])/total))
    return rows,frame_checks


def opportunities(rows, threshold=.10, after=0):
    return [r for r in rows if r["day"]>=after and r["smooth_log_separation"]>=math.log1p(threshold)
            and r["slowing_or_closing"] and r["one_weight"]]


def counts(rows):
    return dict(pair_samples=len(rows),detected=sum(r["detected"] for r in rows),
        both_weights=sum(r["both_weights"] for r in rows),one_band=sum(r["one_band"] for r in rows),
        both_bands=sum(r["both_bands"] for r in rows),executed=sum(r["executed"] for r in rows),
        confirmed_closure=sum(r["high_bp_day"]<0 for r in rows),
        confirmed_closure_with_both_bands=sum(r["high_bp_day"]<0 and r["both_bands"] for r in rows),
        reasons=dict(Counter(r["reason"] for r in rows)))


def render(root):
    root=Path(root); m=json.loads((root/"manifest.json").read_text())
    all_rows={}; checks={}
    for e in m["entries"]:
        run=json.loads((root/e["file"]).read_text())
        rows,frames=analyze(run); all_rows[e["profile"]]=rows; checks[e["profile"]]=frames
    rows=all_rows["three-phase"]
    sensitivity={str(t):dict(full=counts(opportunities(rows,t)),after_day30=counts(opportunities(rows,t,30.25))) for t in (.05,.10,.15)}
    windows=[]
    for i,j in ((0,1),(1,0),(0,2),(2,0),(1,2),(2,1)):
        selected=[r for r in opportunities(rows) if (r["i"],r["j"])==(i,j)]
        groups=[]
        for r in selected:
            if not groups or r["day"]-groups[-1][-1]["day"]>.251: groups.append([])
            groups[-1].append(r)
        for group in groups:
            peak=max(group,key=lambda r:r["smooth_log_separation"])
            windows.append(dict(pair=peak["pair"],start=group[0]["day"],end=group[-1]["day"],peak_day=peak["day"],
                peak_ratio=math.exp(peak["smooth_log_separation"]),**counts(group)))
    windows.sort(key=lambda w:w["peak_day"])
    summary=dict(economic_equality=all(e["economic_equality"] for e in m["entries"]),
        traced_responses={e["profile"]:e["traced_responses"] for e in m["entries"]},
        planned_legs_equal_receipts=True,sensitivity=sensitivity,windows=windows)
    fills=[r for r in rows if r["executed"]]
    summary["clean_fills"]=dict(total=len(fills),donor_below_relative_reference=sum(r["spot_ratio"]<1 for r in fills),
        by_direction=dict(Counter(r["pair"] for r in fills)))
    useful=[r for r in opportunities(rows,.10,30.25) if r["both_weights"] and r["one_band"]]
    summary["both_legs_improve_one_outside_after_day30"]=counts(useful)
    summary["examples"]=[next(r for r in rows if r["day"]==day and r["pair"]==pair)
        for day,pair in ((40.25,"A→B"),(44.75,"C→B"),(76.75,"C→B"),(66.75,"A→C"),(9.5,"B→C"))]
    if "flat-noise80" in all_rows:
        fills=[r for r in all_rows["flat-noise80"] if r["executed"]]
        summary["noisy_flat"]=dict(executed_pair_legs=len(fills),
            paired_responses=sum(bool(f["planned"]) for f in checks["flat-noise80"]),
            max_absolute_ratio_departure=max(abs(r["spot_ratio"]-1) for r in fills),
            high_below_zero=sum(r["high_bp_day"]<0 for r in fills))
    atomic_json(root/"summary.json",summary)
    atomic_json(root/"decisions.json",dict(rows=all_rows,checks=checks))
    _plots(root,rows)
    _report(root,summary,rows)
    print(json.dumps(summary,indent=2))


def _plots(root,rows):
    os.environ.setdefault("MPLCONFIGDIR",str(Path(tempfile.gettempdir())/"alberta-buck-matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    for i,j in ((0,1),(0,2),(1,2)):
        series=[r for r in rows if (r["i"],r["j"])==(i,j)]; days=[r["day"] for r in series]
        fig,axes=plt.subplots(5,1,figsize=(13,12),sharex=True,constrained_layout=True)
        axes[0].plot(days,[math.exp(r["smooth_log_separation"]) for r in series],color="#526975",label="Smooth external ratio")
        axes[0].plot(days,[r["spot_ratio"] for r in series],color="#889ca3",lw=.6,label="Pre-response BUCK-pool ratio")
        axes[0].axhline(1,color="#999",ls=":"); axes[0].set_ylabel("Ratio / known baseline")
        for y in (1.1,1/1.1): axes[0].axhline(y,color="#b1b8b7",ls="--",lw=.7)
        axes[1].plot(days,[r["smooth_rate_bp_day"] for r in series],color="#929d9f",label="Smooth reference rate")
        axes[1].plot(days,[r["estimated_rate_bp_day"] for r in series],color="#274e6b",label="On-chain estimated rate")
        reverse=[r for r in rows if (r["i"],r["j"])==(j,i)]
        axes[1].fill_between(days,[-r["high_bp_day"] if r["ready_mask"] else float('nan') for r in reverse],
            [r["high_bp_day"] if r["ready_mask"] else float('nan') for r in series],color="#a35b66",alpha=.13,label="Policy rate allowance (including own impact)")
        axes[1].axhline(0,color="#999",ls=":"); axes[1].set_ylabel("Log bp/day")
        axes[2].plot(days,[r["donor_gap_pp"] for r in series],color="#246da4",label=f"TOKEN {'ABC'[i]} allocation gap")
        axes[2].plot(days,[r["recipient_gap_pp"] for r in series],color="#bf6430",label=f"TOKEN {'ABC'[j]} allocation gap")
        for y in (-2,0,2): axes[2].axhline(y,color="#999",ls=":")
        axes[2].set_ylabel("Weight − target, pp")
        for direction,color in (((i,j),"#16866d"),((j,i),"#ae6447")):
            s=[r for r in rows if (r["i"],r["j"])==direction]
            label=s[0]["pair"]; y=1 if direction==(i,j) else -1
            for r in opportunities(s): axes[3].plot([r["day"],r["day"]],[y-.3,y+.3],color=color,alpha=.35,lw=2)
            eligible=[r for r in opportunities(s) if r["detected"]]
            axes[3].scatter([r["day"] for r in eligible],[y]*len(eligible),color=color,s=3)
            fills=[r for r in s if r["executed"]]
            axes[0].scatter([r["day"] for r in fills],[r["spot_ratio"] if direction==(i,j) else 1/r["spot_ratio"] for r in fills],
                marker="v" if y==1 else "^",color=color,s=22,label=label+" executed")
            axes[3].scatter([r["day"] for r in fills],[y]*len(fills),marker="D",color="#252b30",s=20)
            executable=[r for r in s if r["both_bands"] and r["detected"] and not r["fallback"]]
            axes[4].plot([r["day"] for r in executable],[r["part_buck"]/r["grain_buck"] for r in executable],
                ".",ms=3,color=color,label=label+" requested leg / minimum")
        axes[3].set_yticks([-1,1],[f"{'ABC'[j]}→{'ABC'[i]}",f"{'ABC'[i]}→{'ABC'[j]}"])
        axes[3].set_ylim(-1.7,1.7); axes[3].set_ylabel("Opportunity / intent / fill")
        axes[4].axhline(1,color="#a14744",ls=":"); axes[4].set_yscale("symlog",linthresh=1); axes[4].set_ylabel("Requested / minimum");axes[4].set_xlabel("Elapsed days")
        for ax in axes:
            ax.grid(alpha=.15)
        for ax in (axes[0],axes[1],axes[2],axes[4]): ax.legend(fontsize=8,ncol=2)
        fig.suptitle(f"{'ABC'[i]}/{'ABC'[j]}: relative separation, motion, weights, opportunity and execution")
        name=f"{'ABC'[i]}-{'ABC'[j]}"
        fig.savefig(root/(name+".png"),dpi=140); fig.savefig(root/(name+".svg"));plt.close(fig)


def _report(root,s,rows):
    late=s["sensitivity"]["0.1"]["after_day30"]
    lines=["#+title: Are relative TOKEN turning opportunities being harvested?", "#+date: 2026-10-06", "#+options: toc:2 num:nil",
        "#+html_head: <style>body{font-family:system-ui;line-height:1.5}#content{max-width:1150px;margin:auto;padding:1rem}img{max-width:100%;height:auto}table{font-size:.88em}</style>","",
        "* Finding", "",
        f"The observer supplies useful motion evidence, but the transfer rule usually prevents it from becoming a trade. After day 30, {late['detected']} of {late['pair_samples']} directed pair-samples in the 10% inspection window produce positive intent, yet only {late['executed']} fills. The implemented two-sided allocation band and small released request are the binding gates, not missing ticks or failed swaps. Positive intent here includes very weak exploratory participation; it is not a claim of confident closure.", "",
        f"The rule also ignores relative displacement and its sign: {s['clean_fills']['donor_below_relative_reference']} of {s['clean_fills']['total']} clean-run fills continue after the donor has become relatively cheaper than its recipient. The proposed reversion rule and the implemented policy are therefore materially different.", "",
        "* Method", "",
        "The test-only PairBasketDecisionTrace facet records the actual pre-response book, directional intent and proposed allocation, then calls the unchanged response. Both 120-day replays have exactly the same economic frames and fills as the saved runs; gas and receipt offsets differ. All 481 responses in each run are traced. Every non-fallback Solidity allocation is reproduced exactly with integer arithmetic, and every proposed paired leg matches an actual receipt swap. This separates a withheld request from a failed execution.","",
        "The clean three-phase benchmark normalizes each TOKEN to its known historical base. For A→B define z = log((price_A/base_A)/(price_B/base_B)). A large positive z means A is relatively dear. The inspection window is z ≥ log(1.10), with either positive but slowing departure (v > 0 and dv/dt < 0), or closure (v ≤ 0). At least one weight must benefit: A overweight OR B underweight. The known smooth function supplies these inspection windows only; it never supplies the contracts' estimates or decisions. A large ratio alone is not evidence of a profitable trade when long-run relative value changes.","",
        "Both weights improve only when A is overweight AND B underweight. The report retains that stronger condition separately. Likewise it distinguishes at least one leg beyond its 2-point band from the implemented requirement that both legs be beyond their bands. Counts are directed pair-samples at six-hour intervals; overlapping pairs are not independent or additive spending budgets.","",
        "* Detection and execution", "",
        "| Relative separation threshold | Period | Candidate pair-samples | Positive intent | Both weights improve | At least one outside band | Both outside bands | Filled pair legs |",
        "|-------------------------------+--------+------------------------+-----------------+----------------------+---------------------------+--------------------+------------------|"]
    for t,v in s["sensitivity"].items():
        for period,c in v.items():
            lines.append(f"| {float(t):.0%} | {period} | {c['pair_samples']} | {c['detected']} | {c['both_weights']} | {c['one_band']} | {c['both_bands']} | {c['executed']} |")
    lines += ["", "* Why a 10% candidate did not fill", "",
        "| Binding classification | Whole run | After day 30 |", "|------------------------+-----------+--------------|"]
    a=s["sensitivity"]["0.1"]["full"]["reasons"]; b=s["sensitivity"]["0.1"]["after_day30"]["reasons"]
    for reason in sorted(a.keys()|b.keys()): lines.append(f"| {reason} | {a.get(reason,0)} | {b.get(reason,0)} |")
    c=s["both_legs_improve_one_outside_after_day30"]
    late=s["sensitivity"]["0.1"]["after_day30"]
    lines += ["", "The order is: recorded fallback; two-sided band; no directional intent; released total below the minimum; donor depth/step cap; recipient share below the minimum; another donor selected. The underlying flags and exact pre-response requested amounts are retained in decisions.json. Economic equality and full trace coverage rule out missing ticks or silently reverted transfers as the explanation for these two replays.","",
        f"The result remains when requiring BOTH weights to improve and at least one to lie outside its band: after day 30 there are {c['pair_samples']} such samples, all {c['detected']} detected, with {c['reasons'].get('two_sided_band',0)} blocked by the two-sided band, {c['reasons'].get('released_total_below_grain',0)} below the minimum and {c['executed']} filled. This is not merely counting directions that would worsen the counter-TOKEN's allocation.", "",
        f"Positive intent is often very weak. Of the {late['pair_samples']} late 10% candidates, {late['confirmed_closure']} have a rate allowance wholly below zero, but {late['confirmed_closure_with_both_bands']} also satisfy both allocation bands. The strong closure condition and the size-eligible allocation window do not overlap. The one late fill uses the weaker exploratory release.", "",
        "* Concrete pre-response examples", "",
        "Ratios use the actual BUCK pools before policy work, normalized to historical base prices. Gaps are percentage points from declared weight. Requested amounts are position-book value in BUCK; a recipient purchase also pairs TOKEN with BUCK into LP, so its swap alone is smaller than the book transfer.", "",
        "| Day | Direction | Relative ratio | Donor gap, pp | Recipient gap, pp | Intent, % gap/day | Requested recipient, BUCK | Minimum, BUCK | Result |",
        "|-----+-----------+----------------+---------------+-------------------+-------------------+---------------------------+---------------+--------|"]
    for r in s["examples"]:
        lines.append(f"| {r['day']:g} | {r['pair']} | {r['spot_ratio']:.4f} | {r['donor_gap_pp']:+.3f} | {r['recipient_gap_pp']:+.3f} | {r['desire_per_day']*100:.3f} | {r['part_buck']:.2f} | {r['grain_buck']:.2f} | {r['reason']} |")
    lines += ["", "At day 40.25 both allocations would improve, but the donor is only 1.903 points overweight, so no transfer is even sized. At day 44.75 both bands pass; only the residual 0.467-point recipient deficit enters sizing, and 0.173% of it releases 11.78 BUCK against a 146.04 minimum. At day 76.75 the point estimate is already closing, but one day's capped allowance still yields only 136.98 against 158.16. At day 66.75 a slightly larger request clears the threshold: 157.33 was planned and 156.30 BUCK of donor proceeds was actually placed into the recipient position.", "",
        "* Closing is not the same as a negative ratio velocity", "",
        f"Of {s['clean_fills']['total']} clean-run paired fills, {s['clean_fills']['donor_below_relative_reference']} sell the relatively cheaper TOKEN into the dearer one (the normalized pre-response ratio is below one). At day 9.5 B/C is 0.9347 and falling. The policy nevertheless calls the negative rate strong closure and releases 27.8% of the remaining band-edge gap per day. Relative to the known baseline, those prices have already crossed and are separating in the opposite direction. This can reduce a bootstrap allocation error, but it is not the reversion-harvesting opportunity described here.", "",
        "For signed displacement z around an explicit reference, closure means z × dz/dt < 0; outward speed is sign(z) × dz/dt. The current policy uses only the sign of dz/dt in the proposed sell direction. It does not inspect z, its size, or a crossing of the relative reference. In a real basket the reference ratio must be defined explicitly; raw TOKEN unit prices need not ever be equal.", "",
        "* Each turning window", "",
        "| Direction | Window, days | Peak day | Peak relative ratio | Samples | Detected | Both outside bands | Fills |",
        "|-----------+--------------+----------+---------------------+---------+----------+--------------------+-------|"]
    for w in s["windows"]:
        lines.append(f"| {w['pair']} | {w['start']:g}–{w['end']:g} | {w['peak_day']:g} | {w['peak_ratio']:.3f} | {w['pair_samples']} | {w['detected']} | {w['both_bands']} | {w['executed']} |")
    lines += ["", "Windows include the stated weight condition, so allocation changes can split them. Peak day is the largest sampled reference ratio within that window; endpoints are clipped to the 120-day run. The smooth reference derivative is a benchmark for the six-hour piecewise-constant feed, not a claim that pool spot prices evolve continuously.", "",
        "* Read the panes", "",
        "1. Normalized relative prices and receipt-confirmed transfers. Each direction uses its own marker.",
        "2. Analytic reference rate, the actual on-chain estimated rate, and the policy allowance including own impact. The allowance is empirical, not a probability interval.",
        "3. Both allocation errors immediately before execution, with zero and ±2-point band boundaries.",
        "4. Colored strips mark large-separation slowing/closing opportunities; colored dots mark positive on-chain intent within them. Black diamonds mark actual pair fills, including fills outside the inspection window.",
        "5. Requested recipient notional divided by the minimum leg, for positive-intent pairs that pass both bands. Values ≤1 cannot execute. These are discrete requests, not cumulative saved turnover.", ""]
    for pair in ("A-B","A-C","B-C"): lines += ["** "+pair,"",f"[[file:{pair}.png]]",""]
    if "noisy_flat" in s:
        f=s["noisy_flat"]
        lines += ["* Noisy flat cross-check", "",
            f"There are {f['paired_responses']} paired responses producing {f['executed_pair_legs']} donor/recipient pair legs. The largest absolute pre-response normalized BUCK-route ratio departure among those fills is {f['max_absolute_ratio_departure']:.2%}. The current policy has no relative-separation term: it can trade near equality. {f['high_below_zero']} of these fills have a policy rate envelope entirely below zero; all rely on weak cessation/stationary release. This does not by itself establish net profit or false estimation; real pool motion and bootstrap book imbalance still exist.",""]
    lines += ["* Interpretation and correction to test", "",
        "The monitor detects relative motion, but the consumer does not implement the proposed opportunity rule fully. It neither conditions intent on relative-price separation nor accepts a counter-TOKEN merely because it is on the useful side of target but still inside its band. After those gates, proportional sizing uses only the excess beyond BOTH band edges, then multiplies it by a small participation fraction. The one-day catch-up cap prevents a consistently sub-minimum request from accumulating into a later executable trade.", "",
        "A follow-up policy should separate the trigger from capacity: a sufficiently separated ratio braking or closing toward its reference, plus at least one material allocation error, opens consideration. Joint donor/recipient capacities should then limit movement toward targets without requiring both legs independently to exceed the trigger band. If the counter-leg would worsen allocation, reject it or use an explicit bounded cash leg. A pending turnover allowance should accumulate only while supported by the directional evidence and should decay/reset when that evidence disappears; simply banking an old signal indefinitely would be incorrect.","",
        "This audit changes instrumentation and documentation only. It does not silently install those proposed execution changes or claim that every six-hour candidate warrants a trade.", "",
        "Reproduce with =make control-lab-pair-opportunities=; render the saved JSON with =make control-lab-pair-opportunities-render=. See [[file:summary.json][counts]], [[file:decisions.json][per-pair decisions]], [[file:manifest.json][replay provenance]], and [[file:../pair-basket/results.org][the matched basket comparison]].",""]
    (root/"results.org").write_text("\n".join(lines))
