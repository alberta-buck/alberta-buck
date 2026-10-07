"""Audit and render the observer phase using saved EVM JSON only."""
import json
import math
import os
from pathlib import Path
import tempfile

from .cli import atomic_json
from .interactive import plot_data, render as render_interactive


def measurements(result):
    observations = [f for f in result["frames"] if f["day"] >= 10 and f["pair_signal"]["ready_mask"] == 3]
    errors, old_errors, covered, declared, wrong = [], [], [], [], []
    for frame in observations:
        for p in frame["pair_signal"]["pairs"]:
            truth = p["truth_bp_day"]
            if truth is None:
                continue
            i, j = p["tokens"]
            old = (frame["signal"]["velocity"][i][0] - frame["signal"]["velocity"][j][0]) * math.log(1.0001) * 10000
            errors.append((p["rate_bp_day"] - truth)**2)
            old_errors.append((old - truth)**2)
            covered.append(p["low_bp_day"] <= truth <= p["high_bp_day"])
            sign = 1 if p["low_bp_day"] > 0 else -1 if p["high_bp_day"] < 0 else 0
            declared.append(bool(sign))
            if sign:
                wrong.append(sign * truth <= 0)
    gas = [f["pair_signal"]["gas"] for f in result["frames"]]
    return dict(profile=result["profile"]["name"], stage=result["stage"],
        rate_rmse_bp_day=math.sqrt(sum(errors)/len(errors)) if errors else None,
        ema5_rmse_bp_day=math.sqrt(sum(old_errors)/len(old_errors)) if old_errors else None,
        allowance_coverage=sum(covered)/len(covered) if covered else None,
        directional_fraction=sum(declared)/len(declared) if declared else None,
        wrong_direction_fraction=sum(wrong)/len(wrong) if wrong else None,
        ready_observations=len(observations), mean_observer_gas=sum(gas)/len(gas), max_observer_gas=max(gas))


def render(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    runs = [json.loads((root / e["file"]).read_text()) for e in manifest["entries"].values()]
    controls = [json.loads((root / e["file"]).read_text()) for e in manifest["controls"].values()]
    compared = []
    for control in controls:
        observed = next(r for r in runs if r["stage"] == control["stage"] and r["profile"] == control["profile"])
        assert observed["bootstrap"] == control["bootstrap"], "observer changed initial books"
        for a, b in zip(observed["frames"], control["frames"], strict=True):
            # Extra transactions necessarily change event transaction counters.
            # Compare every non-telemetry field, and economic event contents.
            exclude = {"pair_signal", "gas", "transactions", "events"}
            assert {k: v for k, v in a.items() if k not in exclude} == {k: v for k, v in b.items() if k not in exclude}, (control["stage"], a["day"])
            def economic(events):
                return [{k: v for k, v in e.items() if k != "transaction"} for e in events]
            assert economic(a["events"]) == economic(b["events"]), (control["stage"], a["day"], "events")
        compared.append(control["stage"])
    summaries = [measurements(r) for r in runs]
    for result in runs:
        for frame in result["frames"]:
            ps = frame["pair_signal"]["pairs"]
            if frame["pair_signal"]["ready_mask"]:
                assert abs(ps[0]["rate_bp_day"] + ps[2]["rate_bp_day"] - ps[1]["rate_bp_day"]) < 1e-7
                assert abs(ps[0]["level_log"] + ps[2]["level_log"] - ps[1]["level_log"]) < 1e-12
            for p in ps:
                assert p["low_bp_day"] <= p["rate_bp_day"] <= p["high_bp_day"]
    baseline = []
    baseline_path = root / "baseline-v1-manifest.json"
    if baseline_path.exists():
        old = json.loads(baseline_path.read_text())
        baseline = [measurements(json.loads((root/e["file"]).read_text()))
                    for e in old["entries"].values() if e["stage"] == "monitor"]
    atomic_json(root / "summary.json", dict(passive_controls_equal=compared, summaries=summaries,
        baseline_v1=baseline, execution_authority=False, decision="lag-corrected-observer-ready-for-isolated-basket-experiment"))
    render_interactive(root)

    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "alberta-buck-matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    report = ["#+title: EVM TOKEN-pair observation: measured results", "#+options: toc:2 num:nil",
        "#+html_head: <style>body{font-family:system-ui;line-height:1.5}#content{max-width:1150px;margin:auto;padding:1rem}img{max-width:100%;height:auto}pre,table{overflow:auto}</style>", "",
        "[[file:index.html][Interactive JavaScript graphs and signal selectors]]", "",
        "The observer is passive. Existing basket, arb and desk policies retain trading authority. Each run uses real V3 pools in pyrevm, with four external market updates per day. Separate Solidity tests cover 12-second service, irregular intervals, equal timestamps and missing venues.", "",
        "* Implementation decision", "",
        "The first implementation stopped before trading because its allowance undercounted deterministic rate lag. The corrected implementation retains the same level/rate estimates and stores the EWMA of absolute rate acceleration. It multiplies this by the analytically derived lag timescale, rather than by one sample's duration. These worlds remain passive; the subsequent [[file:../pair-basket/index.html][basket experiment]] is separate.", "",
        "For constant acceleration a and sampling interval dt, the steady rate error of these alpha-beta gains is (alpha/beta - 1/2)*a*dt = (2*T + dt/2)*a. The new empirical allowance includes (2*T + dt/2)*EWMA(abs(delta rate / dt)), plus the existing price-residual term and floor. This is a model-based lag allowance, not a guaranteed confidence bound for arbitrary shocks.", "",
        "A deterministic Solidity acceleration test reproduces the original defect independently of the six-hour simulator: truth is 23.94 ticks/second, the estimate is 22.6795, and the old upper edge was 23.6632. The corrected upper edge is 24.8029. The rate estimate itself is unchanged. Baseline-v1 JSON is retained so widening the interval cannot be mistaken for improved rate accuracy.", "",
        "* Matched controls", "",
        f"Observer-on and observer-off runs have identical economic frames and event contents for: {', '.join(compared)}. Extra transaction gas and event transaction counters are excluded from that equality check.", "",
        "* Signal measurements", "",
        "Scoring starts on day 10, after initialization. In replay arms the reference is the known smooth input derivative at the observation midpoint. The driver holds pool prices between updates; this discretization contributes error. Noise is shared between the two replay routes and must not count as independent corroboration. In free market arms no latent truth is asserted.", "",
        "| Replay profile | Rate RMSE, bp/day | Existing EMA5 RMSE | Allowance coverage | Nonzero direction | Wrong sign among declared directions |",
        "|----------------+-------------------+--------------------+--------------------+-------------------+--------------------------------------|"]
    for row in summaries:
        if row["stage"] != "monitor" or row["rate_rmse_bp_day"] is None:
            continue
        wrong = "—" if row["wrong_direction_fraction"] is None else f"{row['wrong_direction_fraction']:.1%}"
        report.append(f"| {row['profile']} | {row['rate_rmse_bp_day']:.2f} | {row['ema5_rmse_bp_day']:.2f} | {row['allowance_coverage']:.1%} | {row['directional_fraction']:.1%} | {wrong} |")
    report += ["", "Allowances are heuristic; coverage is measured rather than presumed. A narrower allowance is not automatically a better signal. For flat-noise80 any declared nonzero direction is false relative to the constant underlying input.", "",
        "* Original allowance versus lag-aware allowance", "",
        "| Replay profile | Original coverage | Corrected coverage | Original directional fraction | Corrected directional fraction |",
        "|----------------+-------------------+--------------------+-------------------------------+--------------------------------|"]
    for old in baseline:
        new = next(s for s in summaries if s["profile"] == old["profile"] and s["stage"] == "monitor")
        if new["allowance_coverage"] is not None:
            report.append(f"| {old['profile']} | {old['allowance_coverage']:.1%} | {new['allowance_coverage']:.1%} | {old['directional_fraction']:.1%} | {new['directional_fraction']:.1%} |")
    report += ["", "The correction trades some participation for better allowance coverage. It does not remove the responsiveness/noise tradeoff: on a noisy flat input the new rate estimate is noisier than the slower existing EMA comparator, while its interval withholds directional declarations in this run.", "",
        "* Gas", "", "These are transaction receipt costs for the six-pool observation wheel, including reads, writes and wheel overhead. They are not costs for trading or a whole ecosystem cycle.", "",
        "| Stage / profile | Mean gas | Maximum gas |", "|-----------------+----------+-------------|"]
    for row in summaries:
        report.append(f"| {row['stage']} / {row['profile']} | {row['mean_observer_gas']:,.0f} | {row['max_observer_gas']:,} |")
    for result in sorted(runs, key=lambda r: (r["stage"], r["profile"]["name"])):
        data = plot_data(result)
        ids = {"price", "pair-rate-0", "pair-rate-1", "pair-rate-2", "pair-error-0", "pair-quality"}
        panes = [p for p in data["panes"] if p["id"] in ids]
        fig, axs = plt.subplots(len(panes), 1, figsize=(12, 2.4*len(panes)), sharex=True, constrained_layout=True)
        for ax, pane in zip(axs, panes):
            for line in pane["lines"]:
                ax.plot(data["days"], [v if v is not None else math.nan for v in line["values"]],
                    color=line["color"], linestyle="--" if line["dash"] else "-", linewidth=.9, label=line["label"])
            ax.set_title(pane["title"], loc="left", fontsize=10)
            ax.set_ylabel(pane["unit"], fontsize=8); ax.grid(alpha=.15); ax.legend(fontsize=6, ncol=3)
        axs[-1].set_xlabel("Elapsed days at publication")
        name = result["profile"]["name"] + "-" + result["stage"]
        fig.suptitle(name + " · passive on-chain pair observer", fontsize=13)
        fig.savefig(root / (name + ".png"), dpi=120); fig.savefig(root / (name + ".svg")); plt.close(fig)
        report += ["", "* " + name, "", "#+attr_org: :width 1100", f"[[file:{name}.png]]", ""]
    report += ["* Next phases", "", "Before allowing the observer to gate basket orders, review lag, incorrect directional declarations, and route disagreement in these panes. Then evaluate bounded allocation sizing, followed by separate arb and desk changes. Monetary outcomes and net receipts require their own paired tests; signal RMSE alone does not establish ROI.", ""]
    (root / "results.org").write_text("\n".join(report))
    print(json.dumps(dict(passive_controls_equal=compared, summaries=summaries), indent=2))
