"""Render and check the isolated gate comparison from saved EVM JSON."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import tempfile

from .cli import atomic_json
from .event_view import price_markers
from .events import execution_counts

LABELS = {"original": "Four-of-six quorum gate", "band-only": "Band only · no trend veto",
          "fast-velocity": "Shortest-EMA velocity gate"}


def render(root):
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "alberta-buck-matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    runs = [json.loads((root / e["file"]).read_text()) for e in manifest["entries"]]
    control = runs[0]
    summaries = []
    for result in runs:
        assert result["config"] == control["config"], "gate comparison changed another input"
        assert result["bootstrap"] == control["bootstrap"], "gate comparison changed the starting books"
        assert [f["target_usd"] for f in result["frames"]] == [f["target_usd"] for f in control["frames"]]
        counts = execution_counts(result["frames"])
        assert counts["arb_cycles"] == result["summary"]["arb_cycles"]
        assert counts["desk_ops"] == result["summary"]["desk_ops"]
        quarters = Counter()
        token_counts = Counter()
        for frame in result["frames"]:
            for event in frame["events"]:
                if event["actor"] == "basket":
                    for trade in event["trades"]:
                        quarters[min(3, int(frame["day"] // 30)), trade["side"]] += 1
                        token_counts[trade["token"], trade["side"]] += 1
        end = result["frames"][-1]
        summaries.append(dict(mode=result["gate"], counts=counts,
            token_trades={"ABC"[i]:{side:token_counts[i,side] for side in ("buy","sell")} for i in range(3)},
            period_trades=[{side:quarters[q,side] for side in ("buy","sell")} for q in range(4)],
            ending_equity_buck=end["basket"]["equity"], ending_equity_usd=end["basket"]["equity"]*end["buck_usdc"],
            ending_lien_buck=max(0,-end["basket"]["account"]), end_k=end["k"],
            mean_abs_bvib_error=result["summary"]["mean_abs_parity_error"],
            end_bvib=end["bvib"], end_supplier_headroom=end["agents"]["supply_headroom"]))
    atomic_json(root / "comparison.json", dict(paired_inputs=True, summaries=summaries))

    fig, axes = plt.subplots(3, 3, figsize=(15, 9), sharex=True, sharey=True, constrained_layout=True)
    for column, result in enumerate(runs):
        frames = result["frames"]
        days = [f["day"] for f in frames]
        groups = price_markers(result, native=True)
        for token in range(3):
            ax = axes[token, column]
            ax.plot(days, [f["token_buck"][token] / result["config"]["feed"]["base"][token] for f in frames],
                    color="#526975", lw=1)
            for group in groups:
                if group["id"] not in ("basket-buy", "basket-sell"):
                    continue
                points = [p for p in group["points"] if p["token"] == token]
                ax.scatter([days[p["index"]] for p in points], [p["value"] for p in points],
                    marker="^" if group["shape"] == "up" else "v", color=group["color"], s=18, alpha=.85, zorder=3)
            tc = summaries[column]["token_trades"]["ABC"[token]]
            ax.set_title(f"{LABELS[result['gate']]}\nTOKEN {'ABC'[token]} · {tc['buy']} buys / {tc['sell']} sells", fontsize=10)
            ax.axhline(1, color="#99a6ab", ls=":", lw=.7)
            ax.grid(alpha=.15)
            if column == 0:
                ax.set_ylabel("TOKEN price in BUCK / baseline mean")
            if token == 2:
                ax.set_xlabel("Elapsed days")
            ax.set_xlim(0, result["config"]["days"])
    fig.suptitle("Same clean three-phase feed and initial books · executed basket TOKEN swaps", fontsize=14)
    fig.legend(handles=[Line2D([],[],marker="^",ls="",color="#098c9e",label="TOKEN buy"),
                        Line2D([],[],marker="v",ls="",color="#d46c17",label="TOKEN sell")], loc="outside lower center", ncol=2)
    fig.savefig(root / "gate-trades.png", dpi=140)
    fig.savefig(root / "gate-trades.svg")
    plt.close(fig)

    fig, axes = plt.subplots(4, 1, figsize=(12, 10), sharex=True, constrained_layout=True)
    for result, color in zip(runs, ("#526975", "#d46c17", "#098c9e")):
        f = result["frames"]; day = [x["day"] for x in f]; label = LABELS[result["gate"]]
        for ax, values in zip(axes, ([x["basket"]["equity"]*x["buck_usdc"] / 1e6 for x in f],
                                    [max(0,-x["basket"]["account"]) / 1e6 for x in f],
                                    [x["bvib"] for x in f], [x["k"] for x in f])):
            ax.plot(day, values, color=color, label=label)
    for ax, label in zip(axes, ("Marked equity (million USD)", "Basket lien (million BUCK)", "bvib", "K")):
        ax.set_ylabel(label); ax.grid(alpha=.15)
    axes[0].legend(fontsize=9)
    axes[2].axhline(1, color="#99a6ab", lw=.7, ls=":")
    axes[-1].set_xlabel("Elapsed days")
    fig.suptitle("Harvesting and monetary response · marked equity includes positions, excludes gas", fontsize=13)
    fig.savefig(root / "gate-outcomes.png", dpi=140)
    fig.savefig(root / "gate-outcomes.svg")
    plt.close(fig)

    report = ["#+TITLE: Clean three-phase basket: execution-gate comparison", "#+OPTIONS: ^:nil toc:nil", "",
              "These are three real 120-day pyrevm runs. The feed, initial books, medium buyer/seller rates, target lean, allocation band and execution limits are identical. Only the basket's running(i, side) gate is replaced through a test adapter. The main control-lab policy remains unchanged.", "",
              "| Gate | TOKEN buys | TOKEN sells | Swaps after day 30 | Ending marked equity, USD | Ending basket lien, BUCK | Mean absolute bvib error | Ending K |",
              "|------+------------+-------------+--------------------+---------------------------+--------------------------+--------------------------+----------|"]
    for s in summaries:
        later = sum(sum(p.values()) for p in s["period_trades"][1:])
        report.append(f"| {s['mode']} | {s['counts']['basket_buys']} | {s['counts']['basket_sells']} | {later} | {s['ending_equity_usd']:,.0f} | {s['ending_lien_buck']:,.0f} | {s['mean_abs_bvib_error']:.3%} | {s['end_k']:.4f} |")
    report += ["", "#+ATTR_ORG: :width 1300", "[[file:gate-trades.png]]", "",
               "The fast gate uses the sign of the existing 5-day EMA velocity, combined with the excursion's side of the historical anchor. The original four-of-six turn vote still controls its existing response-size adjustment. The band-only arm retains the same leaned targets; it removes only the trend veto.", "",
               "#+ATTR_ORG: :width 1100", "[[file:gate-outcomes.png]]", "",
               "Ending equity is an on-chain mark converted using the observed BUCK/USDC price. Remaining LP/inventory is included, and swap fees and execution price impact are reflected in actual balances. It is not realized trading profit or a cash liquidation result. Gas is excluded. Different policies produce different leverage and private flows, and both alternative arms nearly exhaust the supplier's spendable BUCK. These monetary effects prevent treating the higher equity mark alone as a general equilibrium improvement.", ""]
    for entry in manifest["entries"]:
        report.append(f"- [[file:{entry['file']}][{entry['mode']} raw EVM JSON]]")
    (root / "results.org").write_text("\n".join(report) + "\n")
    print(f"Checked and rendered three gate experiments → {root / 'results.org'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="doc/control-lab/gate-comparison")
    render(ap.parse_args().out)
