"""Render the pair-observer lab strictly from cached JSON."""
from __future__ import annotations

from collections import defaultdict
import html
import gzip
import json
from pathlib import Path

import numpy as np


def figure(result, destination):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rows = result["records"]
    t = np.array([r["day"] for r in rows])
    fig, ax = plt.subplots(7, 1, figsize=(13, 19), sharex=True, layout="constrained")
    colors = ("#156899", "#b24a29", "#568542")
    names = result["names"]
    pairname = f"{names[0]}/{names[1]}"
    for q, venue in enumerate(("BUCK", "USDC")):
        p = [r["prices"].get(venue, [np.nan])[0] / result["means"][0] for r in rows]
        ax[0].plot(t, p, color=colors[q], label=f"{names[0]}/{venue}, normalized")
        ax[0].plot(t, [r["base_values"][q] for r in rows], color=colors[q], ls=":", label=f"{venue} latent value (test truth)")
    ax[0].set(ylabel="Relative to reference", title="1. Raw pool quotes and baseline currencies")
    for k, (i, j) in enumerate(result["pairs"][:3]):
        truth = np.array([r["truth"][k] for r in rows]) * 100
        est = np.array([r["fused"]["pairs"][k]["spread"] for r in rows]) * 100
        ax[1].plot(t, truth, color=colors[k], ls=":", alpha=.65)
        ax[1].plot(t, est, color=colors[k], label=f"{names[i]}/{names[j]}")
    ax[1].set(ylabel="Normalized log ratio (%)", title="2. TOKEN/TOKEN spreads: estimate (solid), latent truth (dotted)")
    pair = [r["fused"]["pairs"][0] for r in rows]
    for panel, field, truthfield, sigmaidx, title, unit in (
        (2, "velocity", "truth_velocity", 1, "Relative velocity", "% log ratio / day"),
        (3, "acceleration", "truth_acceleration", 2, "Change in relative velocity", "% log ratio / day²")):
        mean = np.array([p[field] for p in pair]) * 100
        sigma = np.array([p["sigma"][sigmaidx] for p in pair]) * 100
        ax[panel].fill_between(t, mean - 2*sigma, mean + 2*sigma, color=colors[0], alpha=.18, label="Model ±2σ")
        ax[panel].plot(t, [r[truthfield][0] * 100 for r in rows], color="black", ls=":", label="Latent truth")
        ax[panel].plot(t, mean, color=colors[0], label="Fused observer")
        ax[panel].set(ylabel=unit, title=f"{panel+1}. {pairname}: {title}")
    ax[2].plot(t, [r["ema"][0]["velocity"][0] * 100 for r in rows], color="#a86c16", alpha=.8, label="Existing daily EMA5")
    ax[2].plot(t, [r["ema"][0]["matched_velocity"][0] * 100 for r in rows], color="#7e5299", alpha=.8, label="6-hour / two-route EMA5")
    ax[2].plot(t, [r["buck_only"]["pairs"][0]["velocity"] * 100 for r in rows], color="#777777", alpha=.6, lw=.8, label="BUCK-only observer")
    if "impact" in result["case"]:
        ax[2].plot(t, [r["uncorrected"]["pairs"][0]["velocity"] * 100 for r in rows], color="red", alpha=.6, label="No own-impact input")
    ax[4].plot(t, [p["inward_score"] for p in pair], color=colors[0], label="Closure evidence score")
    ax[4].plot(t, [p["cessation"]["release"] for p in pair], color="#b24a29", label="Release of departure quench")
    ax[4].step(t, [r["ema"][0]["votes"] / 6 for r in rows], color="#a86c16", alpha=.8, label="EMA votes / 6", where="post")
    ax[4].fill_between(t, 0, 1, where=[p["regime"] == "slowing-departure" for p in pair], color="#f6b453", alpha=.2, label="Departing but slowing")
    ax[4].set(ylabel="Score (not probability)", ylim=(-.05, 1.05), title=f"5. {pairname}: closing evidence versus sign quorum")
    ax[5].plot(t, [r["fused"]["innovation_nis"] for r in rows], label="Innovation / dof", color=colors[0])
    ax[5].plot(t, [r["fused"]["disagreement_nis"] for r in rows], label="Route disagreement / dof", color=colors[1])
    ax[5].axhline(1, color="black", ls=":", lw=.8)
    ax[5].set_yscale("symlog", linthresh=1)
    ax[5].set(ylabel="Normalized residual²", title="6. Quality diagnostics: disagreement identifies a conflict, not which venue is right")
    for i, name in enumerate(names[:3]):
        ax[6].plot(t, [(r["weights"][i] - 1/len(names)) * 100 for r in rows], color=colors[i], alpha=.75, label=f"{name} weight gap")
    requests = ax[6].twinx()
    requests.plot(t, [sum(p["nav_fraction"] for p in r["candidates"]) * 100 for r in rows], color="black", ls="--", label="Fixed scale: candidate (right)")
    requests.plot(t, [sum(p["nav_fraction"] for p in r["cycle_candidates"]) * 100 for r in rows], color="#7e5299", ls="--", label="History scale: candidate (right)")
    requests.set(ylabel="Candidate NAV % / 6-hour tick", ylim=(0, None))
    requests.legend(loc="lower right", fontsize=8)
    ax[6].set(ylabel="Allocation gap (pp)", xlabel="Day", title="7. Fixed-quantity diagnostic book: candidate transfers are NOT executed")
    for index, a in enumerate(ax):
        a.grid(alpha=.2)
        if index:
            a.axhline(0, color="grey", lw=.5)
        a.legend(loc="upper left", ncol=3, fontsize=8)
        a.set_xlim(0, result["days"])
        for r in rows:
            if not r["fused"]["fresh"]:
                a.axvspan(r["day"], r["day"] + .25, color="grey", alpha=.1, linewidth=0)
    fig.suptitle(f"{result['case']} · quote noise σ={result['noise']*100:g}% · seed {result['seed']}\n"
                 "Causal sensor experiment · fixed parameters · all pairs in JSON; first three shown", fontsize=14)
    for suffix in ("png", "svg"):
        fig.savefig(destination.parent / f"{destination.name}.{suffix}", dpi=125)
    plt.close(fig)


def spectral_figure(result, destination):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(2, 1, figsize=(12, 8), layout="constrained")
    for panel, objects, labels in ((0, result["spectra"]["tokens"], result["names"]),
        (1, result["spectra"]["pairs"][:3], [f"{result['names'][i]}/{result['names'][j]}" for i, j in result["pairs"][:3]])):
        for s, label in zip(objects, labels):
            frequency = np.asarray(s["frequency_per_day"])
            use = frequency > 0
            line, = ax[panel].loglog(1 / frequency[use], np.maximum(np.asarray(s["power"])[use], 1e-20), label=label)
            if s["resolved"]:
                ax[panel].axvline(s["period_days"], color=line.get_color(), alpha=.3)
        ax[panel].set(xlabel="Period (days)", ylabel="Power per frequency bin", xlim=(2, 1280))
        ax[panel].grid(alpha=.2)
        ax[panel].legend(ncol=3)
    ax[0].set_title("Centered TOKEN spectra: each includes motion of the other TOKENs")
    ax[1].set_title("Pair spectra: markers show power-weighted characteristic periods")
    fig.suptitle(f"{result['case']}: only days -1280 through -1; no live-run data\n"
                 "Hann periodogram, linear detrend; observed time scales are not physical production lags")
    for suffix in ("png", "svg"):
        fig.savefig(destination.parent / f"{destination.name}.{suffix}", dpi=125)
    plt.close(fig)


def render(out):
    out = Path(out)
    manifest = json.loads((out / "manifest.json").read_text())
    grouped = defaultdict(list)
    for e in manifest["entries"]:
        grouped[e["case"], e["noise"]].append(e)
    summaries, figures = [], []
    for (case, noise), entries in sorted(grouped.items()):
        entry = min(entries, key=lambda e: e["seed"])
        with gzip.open(out / entry["file"], "rt") as f:
            result = json.load(f)
        stem = f"{case}-noise-{noise:g}"
        figure(result, out / stem)
        spectral_figure(result, out / f"{stem}-spectrum")
        metrics = {}
        for variant in ("fused", "buck_only", "uncorrected", "ema5", "ema_matched"):
            metrics[variant] = {}
            for key in entry["metrics"][variant]:
                values = [e["metrics"][variant][key] for e in entries if e["metrics"][variant][key] is not None]
                metrics[variant][key] = float(np.mean(values)) if values else None
        summaries.append(dict(case=case, noise=noise, seeds=len(entries), metrics=metrics))
        for policy in ("candidates", "cycle_candidates"):
            metrics[policy] = {}
            for key in entry["metrics"][policy]:
                values = [e["metrics"][policy][key] for e in entries if e["metrics"][policy][key] is not None]
                metrics[policy][key] = float(np.mean(values)) if values else None
        figures.append(dict(case=case, noise=noise, image=f"{stem}.png", svg=f"{stem}.svg", spectrum=f"{stem}-spectrum.png", data=entry["file"], seed=entry["seed"]))
    (out / "summary.json").write_text(json.dumps(summaries, indent=2, allow_nan=False) + "\n")
    org = ["#+title: Pairwise price observer: generated sensor results", "#+options: toc:2", "",
        "This is a causal Python sensor comparison, not an EVM trading backtest.",
        "No allocations are executed and no ROI is measured. See [[file:../../PAIR-OBSERVER.org][the design and limitations]].", "",
        f"{len(manifest['entries'])} runs, {manifest['days']} live days each. Parameters are fixed across cases, seeds and noise levels.",
        "Quote noise is log-price standard deviation per observation (0, 20 or 80 bp in the default grid).", "",
        "* Scoring", "",
        "Days 10 through the end, all TOKEN pairs; seed means below. Velocity RMSE is in bp/day.",
        "Coverage is the empirical fraction inside the observer's model ±2σ interval; it is not assumed to be 95%.",
        "The daily EMA baseline has less frequent samples. The matched EMA also uses both corrected routes every six hours,",
        "with a five-day span; it uses equal route weights, while the observer uses covariance weights.",
        "'Wrong close' is the fraction of strong closure-score samples whose latent spread is not actually closing.",
        "It is undefined when there are no strong signals. These are sample counts, not independent trade events.", "",
        "| Case | Noise bp | Fused RMSE | BUCK-only RMSE | Daily EMA RMSE | Matched EMA RMSE | ±2σ coverage | Wrong close |",
        "|------+----------+------------+----------------+----------------+------------------+--------------+-------------|"]
    table = []
    for s in summaries:
        m = s["metrics"]
        wrong = m["fused"]["false_closing_fraction"]
        row = [s["case"], f"{s['noise']*1e4:g}"] + [f"{m[v]['velocity_rmse']*1e4:.2f}" for v in ("fused", "buck_only", "ema5", "ema_matched")]
        row += [f"{m['fused']['velocity_2sigma_coverage']:.1%}", "—" if wrong is None else f"{wrong:.1%}"]
        org.append("| " + " | ".join(row) + " |")
        table.append("<tr>" + "".join(f"<td>{html.escape(x)}</td>" for x in row) + "</tr>")
    org += ["", "* Cessation and cycle scales", "",
        "The allocator starts releasing suppression during credible deceleration before velocity changes sign.",
        "Closure adds a rate term; an overweight seller's eligible recipients share its sale in proportion to shortfalls.",
        "A common final scale prevents overspending any recipient's shortfall without changing these proportions.",
        "The history-scaled arm freezes spectral parameters at day -1. Its different candidate intensity is not a return comparison.", "",
        "| Case | Noise bp | Fixed: candidate NAV sum | History: candidate NAV sum | Fixed: share departing | History: share departing |",
        "|------+----------+--------------------------+----------------------------+------------------------+--------------------------|"]
    for s in summaries:
        a, b = (s["metrics"][k] for k in ("candidates", "cycle_candidates"))
        def percent(v):
            return "—" if v is None else f"{v:.1%}"
        org.append(f"| {s['case']} | {s['noise']*1e4:g} | {a['candidate_nav_sum']:.3f} | {b['candidate_nav_sum']:.3f} | {percent(a['share_while_departing'])} | {percent(b['share_while_departing'])} |")
    org += ["", "Candidate NAV sums are repeated hypothetical requests against a fixed-quantity book, NOT traded turnover.",
        "Departing requests can be intentional when departure is slowing; inspect the separate slowing share in summary.json.",
        "", "* Panes", "", "Figures use the first listed seed; summary metrics include all listed seeds.", ""]
    for f in figures:
        org += [f"** {f['case']}: noise {f['noise']*1e4:g} bp", "", f"[[file:{f['data']}][Compressed JSON]] · [[file:{f['svg']}][Vector figure]]", "", f"[[file:{f['image']}]]", "", f"[[file:{f['spectrum']}]]", ""]
    (out / "results.org").write_text("\n".join(org))
    title = "Pairwise price observer lab"
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><style>body{{font:16px system-ui;max-width:1400px;margin:2em auto;padding:0 1em;color:#213547}}select{{padding:.5em;margin:.3em 1em .3em 0}}img{{width:100%;height:auto}}table{{border-collapse:collapse;font-size:13px}}td,th{{padding:.4em;border:1px solid #ddd;text-align:right}}.scroll{{overflow:auto}}a{{color:#156899}}</style>
<h1>{title}</h1><p>Causal Python sensor experiments; no trades executed and no ROI measured.
<a href="../../PAIR-OBSERVER.html">Design and limitations</a> · <a href="results.org">Org results</a> · <a href="manifest.json">Manifest</a></p>
<p>Each figure shows one seed; the table averages all seeds. A ±2σ band describes the model, whose coverage must be measured.</p>
<label>Scenario <select id="case"></select></label><label>Quote noise <select id="noise"></select></label>
<p id="links"></p><img id="figure" alt="Seven aligned panes: pool quotes, pair spreads, velocity, acceleration, turn evidence, quality, and candidate allocation"><details><summary>Historical frequency graphs used for response scales</summary><img id="spectrum" alt="Historical TOKEN and pair frequency spectra"></details>
<details><summary>All measured results</summary><p>Velocity RMSE: bp/day. Matched EMA uses two corrected routes every six hours; daily EMA uses BUCK once daily. Wrong close counts strong-score samples, not trades. Scoring starts at day 10.</p><div class="scroll"><table><thead><tr>{''.join('<th>'+x+'</th>' for x in ['Case','Noise bp','Fused RMSE','BUCK-only','Daily EMA','Matched EMA','±2σ coverage','Wrong close'])}</tr></thead><tbody>{''.join(table)}</tbody></table></div></details>
<script>const figures={json.dumps(figures)};
const c=document.getElementById('case'), n=document.getElementById('noise');
for(const x of [...new Set(figures.map(f=>f.case))]) c.add(new Option(x,x));
for(const x of [...new Set(figures.map(f=>f.noise))]) n.add(new Option(`${{x*10000}} bp`,x));
function draw(){{const f=figures.find(f=>f.case===c.value&&f.noise===Number(n.value));if(!f)return;
document.getElementById('figure').src=f.image;document.getElementById('spectrum').src=f.spectrum;const links=document.getElementById('links');links.replaceChildren(document.createTextNode(`Seed ${{f.seed}} · `));
for(const [label,url] of [['Compressed JSON',f.data],['SVG',f.svg]]){{const a=document.createElement('a');a.href=url;a.textContent=label;links.append(a,document.createTextNode(' · '));}}}}
c.value=figures.some(f=>f.case==='three-phase')?'three-phase':c.value;c.onchange=n.onchange=draw;draw();</script></html>"""
    (out / "index.html").write_text(page)
    print(f"Rendered {len(figures)} pane/spectrum figure sets from {len(manifest['entries'])} cached runs: {out / 'index.html'}", flush=True)
