"""Render saved control-lab JSON. This module never imports web3 or pyrevm."""
from __future__ import annotations

import html
import json
import math
import os
from pathlib import Path
import tempfile

from .model import STAGES


EXPLANATIONS = {
    "feeds": "Only the external market maker acts. Dashed lines are the selected feeds; dotted lines are TOKEN/USDC spot. The internal TOKEN/BUCK and BUCK/USDC pools are left alone. Their failure to follow is intentional: observing an external price does not transmit it into another pool.",
    "monitor": "This is an instrument test, not a market model. The feed driver explicitly replays the same numeric trace into TOKEN/BUCK by real swaps, with no arbitrage, desk or private flow. The monitor reads those pools. The lower panes show the actual published reference and daily relative-price excursions. A circle marks a four-of-six turn quorum; it is not a trade. All EMA levels start from explicit pre-run history built from the known mean prices and baseline oscillation, excluding experiment perturbations.",
    "monitor-k": "Calibration: service the real K controller against the same replay, with no arbitrage or private/desk response. This isolates reference normalization and oracle precision from market flow. In the unperturbed three-phase case, the reference remains near one within integer-weight and V3-tick precision. K can integrate that small quantization error; its input and integral are not forcibly recentered. Compare this with linked/K before interpreting a larger drift as a filter defect.",
    "linked": "Direct replay into TOKEN/BUCK is now removed. One bounded, profit-seeking consistency arbitrage actor links TOKEN/USDC, TOKEN/BUCK and BUCK/USDC. Only TOKEN/USDC is externally driven. Internal prices can lag the feed or remain separated by fees. K is held fixed and the basket's investment policy remains off.",
    "k": "The same market now services the real direct K controller. Its input is the live basket TWAP, not an EMA. There is still no price-sensitive buyer or issuer and no desk: a change in a credit limit cannot itself execute a market order. Compare this arm with linked, including its actual pool-price differences, rather than assuming K must move prices.",
    "desk": "Add the separate equity desk to the K arm. Its outer entry/release bands are 3%/2% by default. Operations are financed by its own inventory or permitted credit, with a finite turnover programme and capital floor. The capital pane distinguishes remaining spending authority from the actual signed BUCK account. Before/after-policy pool prices are retained in JSON; K sees a completed operation through a later TWAP.",
    "demand": "Branch from K without the desk. One buyer has a steady desire to accumulate BUCK, funded by a finite USDC budget. Its rate is positive at fair value and backs off exponentially at a premium; a discount increases the rate up to a daily cap. The reference is the basket's current external value. There is no private supplier, resale rule or replenishment. Buying outstanding BUCK is absorption into holdings, not a burn of total supply.",
    "supply": "Branch from K without the desk or buyer. One issuer steadily sells BUCK for USDC using activated, fixed-face credit and current spendable balance. Its rate is positive at fair value and backs off exponentially at a discount; a premium increases the rate up to a daily cap. K changes available headroom. This simple flow agent never buys back to deleverage: a falling limit can leave an existing lien above the new limit. It is not a mortgage or liquidation model.",
    "both": "Enable the buyer and issuer together, still without the desk. Each acts at most once per market step. Their call order alternates, both rate limits are per day, and neither receives new funding during the run. This distinguishes two-sided private response from the isolated demand and supply cases.",
    "all": "Add the same desk to the two-sided private market. Compare both versus all for its incremental effect, and desk versus all for the private agents' effect. Every arm uses the same feed, pool liquidity, starting K and passive basket. The actors trade against current external basket value, not an assumed permanent USDC peg.",
    "basket": "Finally enable the replacement basket's Daily/Sync/Respond work as well. Its initially idle TOKEN can be paired and rebalanced; changes in liquidity and basket credit now join the feedback loop. This additional arm separates the basket response from the earlier monetary experiments. Allocation and equity appear in the last pane.",
}


def plot_case(result, dest, price_limits, common_limits, k_limits):
    import matplotlib.pyplot as plt
    import numpy as np
    f, flags, cfg = result["frames"], result["flags"], result["config"]
    day = np.array([x["day"] for x in f])
    monitored = flags.get("monitor", False)
    count = 1 + (3 if monitored else 0) + sum(bool(flags.get(k)) for k in ("k", "desk", "basket"))
    count += int(bool(flags.get("demand") or flags.get("supply")))
    count += int(bool(flags.get("arb")))
    fig, axes = plt.subplots(count, 1, figsize=(12, 2.35 * count + .65), sharex=True,
                             squeeze=False, constrained_layout=True)
    axes = list(axes[:, 0])
    colors = ["#2563eb", "#d97706", "#0f9d78"]
    ax = axes.pop(0)
    for i, color in enumerate(colors):
        base = cfg["feed"]["base"][i]
        ax.plot(day, [x["target_usd"][i] / base for x in f], "--", color=color, lw=1.2, alpha=.75)
        ax.plot(day, [x["token_usdc"][i] / base for x in f], ":", color=color, lw=.9)
        ax.plot(day, [x["token_buck"][i] * x["buck_usdc"] / base for x in f],
                color=color, lw=1.4, label=f"TOKEN {'ABC'[i]}")
    ax.set(ylim=price_limits, ylabel="TOKEN price in USD\n/ initial base price")
    ax.set_title("Solid: TOKEN/BUCK × BUCK/USDC   ·   dashed: target   ·   dotted: TOKEN/USDC", fontsize=9, loc="left")
    ax.legend(loc="upper right", ncol=3, fontsize=8)
    if monitored:
        ax = axes.pop(0)
        for i, color in enumerate(colors):
            base = cfg["feed"]["base"][i]
            ax.plot(day, [x["token_buck"][i] / base for x in f], color=color, label=f"TOKEN {'ABC'[i]}")
        ax.set(ylim=price_limits, ylabel="TOKEN price in BUCK\n/ initial base price")
        ax.set_title("Native TOKEN/BUCK prices: the monetary response is visible here before USD conversion", fontsize=9, loc="left")
        ax.legend(loc="upper right", ncol=3, fontsize=8)
        ax = axes.pop(0)
        ax.plot(day, [x["bvib"] for x in f], color="#111827", label="Live basket TWAP (BUCK)")
        ax.plot(day, [x["signal"]["raw"] if x["signal"]["fresh"] else math.nan for x in f],
                color="#b91c1c", lw=.9, alpha=.8, label="Published bvib")
        ax.plot(day, [x["buck_usdc"] for x in f], "--", color="#7c3aed", label="BUCK/USDC")
        ax.plot(day, [x["reference"]["market_usd"] for x in f],
                ":", color="#64748b", label="Reference basket (USD)")
        for level in (1.03, .97):
            ax.axhline(level, color="#dc2626", alpha=.18, lw=.8)
        ax.axhline(1, color="#9ca3af", lw=.6)
        bad = [x["day"] for x in f if x["signal"]["invalid_after_move"]]
        if bad:
            ax.scatter(bad, [common_limits[0] + .01] * len(bad), marker="x", s=13,
                       color="#dc2626", label="Rejected immediately after move")
        ax.set(ylim=common_limits, ylabel="Reference costs\nand BUCK/USD")
        ax.legend(loc="upper right", fontsize=7, ncol=2)
        ax = axes.pop(0)
        # A tick is log base 1.0001. Express excursions as log percentage
        # points so this pane's units are readable, without calling it ROI.
        scale = math.log(1.0001) * 100
        for i, color in enumerate(colors):
            exc = np.array([x["signal"]["excursion"][i] * scale for x in f])
            ax.plot(day, exc, color=color, label=f"{'ABC'[i]} excursion")
            turned = np.array([x["signal"]["fresh"] and x["signal"]["votes"][i] >= 4 for x in f])
            ax.scatter(day[turned], exc[turned], s=9, facecolors="none", edgecolors=color, linewidths=.6)
        ax.axhline(0, color="#9ca3af", lw=.6)
        ax.set(ylabel="Daily excursion\n(log percentage points)")
        ax.legend(loc="upper right", fontsize=8, ncol=3)
    if flags.get("k"):
        ax = axes.pop(0)
        k0 = cfg["controller"]["k0"]
        ax.plot(day, [x["k"] for x in f], color="#111827", label="K")
        ax.plot(day, [k0 + x["p_contribution"] for x in f], "--", color="#0284c7", label="K₀ + P contribution")
        ax.plot(day, [k0 + x["i_contribution"] for x in f], ":", color="#be123c", label="K₀ + I contribution")
        ax.axhline(k0, color="#9ca3af", lw=.6)
        ax.set(ylim=k_limits, ylabel="K / credit fraction")
        ax.set_title(f"Configured rails: {cfg['controller']['kmin']:.2f}–{cfg['controller']['kmax']:.2f}; shared scale across this profile", fontsize=8, loc="left")
        ax.legend(loc="lower left", fontsize=8, ncol=3)
    if flags.get("arb"):
        ax = axes.pop(0)
        initial = result["bootstrap"]["market"]
        ax.plot(day, [x["market"]["arb_held_buck"] / 1000 for x in f], color="#7c3aed", label="BUCK held as arb profit")
        ax.plot(day, [(x["market"]["pool_minus_virtual_buck"] - initial["pool_minus_virtual_buck"]) / 1000 for x in f],
                color="#d97706", label="Change in pool BUCK outside active depth")
        ax.plot(day, [(initial["active_buck"] - x["market"]["active_buck"]) / 1000 for x in f],
                ":", color="#111827", label="Decrease in active BUCK reserves")
        ax.set(ylabel="Market BUCK balances\n(thousands)")
        ax.set_title("At fixed liquidity, pool BUCK outside active depth accrues as uncollected fees", fontsize=8, loc="left")
        ax.legend(loc="upper left", fontsize=7)
    if flags.get("desk"):
        ax = axes.pop(0)
        for key, color, label in (("capital", "#059669", "Net capital (low mark)"),
                                   ("budget", "#7c3aed", "Remaining turnover budget"),
                                   ("account", "#d97706", "Signed BUCK account")):
            ax.plot(day, [x["desk"][key] / 1e6 for x in f], color=color, label=label)
        ax.axhline(cfg["desk"]["capital_floor_buck"] / 1e6, color="#dc2626", ls=":", label="Capital floor")
        ax.set(ylabel="Desk\n(million BUCK)")
        ax.legend(loc="upper right", fontsize=7, ncol=2)
    if flags.get("demand") or flags.get("supply"):
        ax = axes.pop(0)
        if flags.get("demand"):
            ax.plot(day, [x["cumulative"]["demand_usdc"] / 1000 for x in f], color="#2563eb", label="Cumulative USDC spent buying BUCK")
            ax.plot(day, [x["agents"]["demand_cash"] / 1000 for x in f], "--", color="#2563eb", label="Buyer USDC remaining")
        if flags.get("supply"):
            ax.plot(day, [x["cumulative"]["supply_buck"] / 1000 for x in f], color="#d97706", label="Cumulative BUCK sold")
            ax.plot(day, [x["agents"]["supply_headroom"] / 1000 for x in f], "--", color="#d97706", label="Issuer spendable BUCK")
        ax.set(ylabel="Private actors\n(thousand native units)")
        ax.legend(loc="upper left", fontsize=7, ncol=2)
    if flags.get("basket"):
        ax = axes.pop(0)
        for i, color in enumerate(colors):
            ax.plot(day, [x["basket"]["weights"][i] / 100 for x in f], color=color, label=f"TOKEN {'ABC'[i]}")
        ax.axhline(100 / 3, color="#9ca3af", ls=":")
        ax.set(ylabel="Basket allocation (%)", ylim=(0, 100))
        ax.legend(loc="upper right", fontsize=8, ncol=3)
    for ax in fig.axes:
        ax.grid(alpha=.14)
        ax.set_xlim(0, cfg["days"])
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(labelsize=8)
    fig.axes[-1].set_xlabel("Elapsed days (one market step every %.1f hours)" % (24 / cfg["ticks_per_day"]))
    fig.suptitle(f"{result['profile']['name']}  ·  {flags['title']}", fontsize=14, fontweight="bold")
    fig.savefig(dest.with_suffix(".png"), dpi=135)
    fig.savefig(dest.with_suffix(".svg"))
    plt.close(fig)


def plot_ladders(result, dest):
    import matplotlib.pyplot as plt
    f = result["frames"]
    day = [x["day"] for x in f]
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True, constrained_layout=True)
    windows = (5, 10, 20, 40, 80, 160, 1280)
    for i, ax in enumerate(axes):
        origin = result["history"]["mean_relative_ticks"][i]
        scale = math.log(1.0001) * 100
        ax.plot(day, [(x["signal"]["relative"][i] - origin) * scale for x in f], color="black", lw=1.2, label="Daily relative observation")
        for k, window in enumerate(windows):
            ax.plot(day, [(x["signal"]["ema"][i][k] - origin) * scale for x in f], lw=.9,
                    label=f"{window} d EMA", alpha=.8)
        ax.set_ylabel(f"TOKEN {'ABC'[i]}\nrelative log offset (%)")
        ax.axhline(0, color="#9ca3af", lw=.6)
        ax.grid(alpha=.15)
    axes[0].legend(fontsize=7, ncol=4)
    axes[-1].set_xlabel("Elapsed days")
    fig.suptitle(f"{result['profile']['name']} · monitor's seven EMA rungs · known historical mean = 0", fontsize=13)
    fig.savefig(dest.with_suffix(".png"), dpi=135)
    fig.savefig(dest.with_suffix(".svg"))
    plt.close(fig)


def render(manifest_path):
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "alberta-buck-matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    path = Path(manifest_path)
    manifest = json.loads(path.read_text())
    root = path.parent.parent
    plots = root / "plots"
    plots.mkdir(parents=True, exist_ok=True)
    records = [json.loads((path.parent / e["file"]).read_text()) for e in manifest["entries"]]
    report = ["#+TITLE: Control lab — rendered experiment results", "#+OPTIONS: ^:nil toc:2", "",
              "These figures and tables were rendered from saved pyrevm JSON. No EVM runs are performed by the renderer.",
              f"Implementation fingerprint: ={manifest['implementation']['sha256']}=.", "",
              "Solid TOKEN curves use TOKEN/BUCK spot multiplied by BUCK/USDC spot, so every curve in the first pane has the same USD basis. Native TOKEN/BUCK prices are retained in JSON. The reference basket's USD cost can move: BUCK is not assumed to remain worth one USDC.", ""]
    gallery = []
    for profile in dict.fromkeys(r["profile"]["name"] for r in records):
        cases = [r for r in records if r["profile"]["name"] == profile]
        targets, common, ks = [], [], []
        for r in cases:
            base = r["config"]["feed"]["base"]
            for f in r["frames"]:
                targets += [f["target_usd"][i] / base[i] for i in range(3)]
                targets += [f["token_buck"][i] * f["buck_usdc"] / base[i] for i in range(3)]
                targets += [f["token_buck"][i] / base[i] for i in range(3)]
                common += [f["bvib"], f["buck_usdc"], f["reference"]["market_usd"]]
                k0 = r["config"]["controller"]["k0"]
                ks += [f["k"], k0 + f["p_contribution"], k0 + f["i_contribution"]]
        price_limits = (min(targets) - .035, max(targets) + .035)
        common_limits = (min(.95, min(common)) - .02, max(1.05, max(common)) + .02)
        k_limits = (min(ks) - .015, max(ks) + .015)
        report += [f"* {profile}", "",
                   "Price and common-mode axes are held constant across this profile's stages. A different number of panes reflects the additional mechanism, not a changed price scale.", "",
                   "| Stage | Mean absolute bvib error | Final K | Desk ops | Buyer USDC spent | Issuer BUCK sold | Arb profit (BUCK) | Runtime (s) |",
                   "|-------+--------------------------+---------+----------+------------------+------------------+-------------------+-------------|"]
        for r in cases:
            s = r["summary"]
            report.append(f"| {r['stage']} | {s['mean_abs_parity_error']:.2%} | {s['end_k']:.4f} | {s['desk_ops']} | {s['demand_usdc']:,.0f} | {s['supply_buck']:,.0f} | {s['arb_profit_buck']:,.0f} | {s['seconds']:.2f} |")
        report += ["", "The feeds and replay arms are diagnostic controls, not equilibrium competitors. These errors describe this run; they are not a profitability or superiority test. Arb profit sums actual completed cycles after pool fees, before gas costs; it is not desk or depositor ROI.", "",
                   "All venues begin at the same unperturbed day-zero phase. The arb retains its profit in BUCK in this experiment. Without a returning flow, that custody and uncollected LP fees reduce active BUCK reserves and can make BUCK dear even though the external three-phase index is steady. The new balance pane makes this monetary effect explicit. With changing LP positions, pool-minus-virtual balances also include changes in finite-range offsets.", ""]
        for r in cases:
            stem = f"{profile}-{r['stage']}"
            plot_case(r, plots / stem, price_limits, common_limits, k_limits)
            entry = next(e for e in manifest["entries"] if e["profile"] == profile and e["stage"] == r["stage"])
            report += [f"** {r['flags']['title']}", "", EXPLANATIONS[r["stage"]], "",
                       f"[[file:results/{entry['file']}][Output JSON]] · [[file:plots/{stem}.svg][Vector figure]]", "",
                       "#+ATTR_ORG: :width 1100", f"[[file:plots/{stem}.png]]", ""]
            if r["stage"] == "monitor-k":
                fs = r["frames"]
                mean_bvib = sum(x["bvib"] for x in fs) / len(fs)
                precision = sum(x["bvib"] - x["reference"]["spot_buck"] for x in fs) / len(fs) * 10000
                report += [f"Calibration readings: mean raw bvib ={mean_bvib:.8f}=; mean TWAP quote minus exact spot reference ={precision:+.4f} bp=; terminal K ={fs[-1]['k']:.8f}=. These figures retain the contract's measured precision error.", ""]
            gallery.append(dict(profile=profile, stage=r["stage"], title=r["flags"]["title"],
                                plot=f"plots/{stem}.svg", text=EXPLANATIONS[r["stage"]],
                                json=f"results/{entry['file']}"))
            if r["stage"] == "monitor":
                plot_ladders(r, plots / f"{profile}-ladders")
                report += ["*** Inspect the actual filters", "",
                           "Each pane below follows one TOKEN. All seven EMA levels and the daily input are read from the contract; zero is the centered log-price corresponding to the supplied historical mean prices. Historical levels survive rejected samples and observation gaps. A gap clears velocity evidence; it does not reset the anchor to the next price.", "",
                           f"Bootstrap imports {len(r['history']['rows'])} daily baseline observations ending at day -1. The complete prices, known means, integer EMA state and source digest are in this arm's output JSON. Profile perturbations are excluded, and imported history grants no live persistence credit to the desk.", "",
                           "#+ATTR_ORG: :width 1100", f"[[file:plots/{profile}-ladders.png]]", ""]
    (root / "results.org").write_text("\n".join(report))
    # A file://-friendly convenience viewer over the same rendered figures.
    options = "".join(f'<option value="{i}">{html.escape(x["profile"] + " / " + x["title"])}</option>' for i, x in enumerate(gallery))
    data = json.dumps(gallery).replace("<", "\\u003c")
    (root / "index.html").write_text("""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>BUCK control lab</title>
<style>body{font:16px system-ui;margin:2rem auto;max-width:1200px;padding:0 1rem;color:#172033}
select{font:inherit;max-width:100%;padding:.5rem}p{line-height:1.6;max-width:90ch}img{width:100%;height:auto}
nav{position:sticky;top:0;background:#fff;padding:1rem 0}a{color:#2563eb}</style>
<h1>BUCK control lab</h1><p>Saved pyrevm experiments. Changing the selection reads a rendered figure; it does not run a simulation.</p>
<p><a href="interactive/index.html">Open the interactive parameter lab</a> for JavaScript charts, random-walk controls and buyer/seller quantity selectors.</p>
<nav><label for="case">Experiment </label><select id="case">""" + options + """</select> · <a id="json">Output JSON</a></nav>
<p id="description"></p><img id="plot" alt="Staged control experiment plots">
<script>const cases=""" + data + """;const menu=document.getElementById('case');
function show(){const c=cases[Number(menu.value)];document.getElementById('description').textContent=c.text;
document.getElementById('plot').src=c.plot;document.getElementById('json').href=c.json;}
menu.addEventListener('change',show);show();</script></html>""")
    print(f"rendered {len(records)} cases → {root / 'results.org'}", flush=True)
