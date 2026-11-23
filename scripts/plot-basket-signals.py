"""Render pair-policy readings and the basket's recorded TOKEN buys and sells to PDF."""
import argparse
from collections import Counter
import itertools
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.transforms import offset_copy
import numpy as np


def read(path):
    return json.loads(path.read_text())


def lab_series(root, profile):
    manifest = read(root / "manifest.json")
    entry = next(e for e in manifest["entries"].values() if (e["profile"], e["sizing"]) == (profile, "lab"))
    result = read(root / entry["file"])
    assert result["cache_key"] == entry["cache_key"]
    scores = read(root / "scores.json")[profile]
    gain = scores["lab"]["reference"] - scores["maintenance"]["reference"]
    rows = []
    for f in result["frames"]:
        pairs = []
        for p in f["pair_signal"]["pairs"]:
            i, j = p["tokens"]
            desire = f["pair_policy"]["desire"]
            pairs.append(dict(tokens=[i, j], rate=p["rate_bp_day"], low=p["low_bp_day"],
                              high=p["high_bp_day"], ready=p["ready_mask"],
                              forward=desire[i][j], reverse=desire[j][i]))
        trades = [dict(side=e["action"], token=e[e["action"] + "_token"]) for e in f["events"]
                  if e["actor"] == "basket" and e["action"] in ("buy", "sell")]
        rows.append(dict(day=f["day"], prices=f["target_usd"], pairs=pairs, trades=trades,
                         weights=f["basket"]["weights"], targets=[3333, 3333, 3334]))
    assert sum(len(r["trades"]) for r in rows) == scores["lab"]["trades"]
    title = "Control lab  /  " + profile
    subtitle = (f"120 days · four steps/day · half tilt · {gain:+,.0f} USD of terminal book value vs maintenance "
                f"at external marks")
    return title, subtitle, ["A", "B", "C"], rows, "declared weight", entry["file"]


def replay_series(path):
    replay = read(path)
    rows = replay["frames"]
    seed = replay["config"]["scenario"]["seed"]
    share, hold = [100 * (rows[-1][k] / rows[0][k] - 1) for k in ("share", "hold")]
    subtitle = (f"Seed {seed} · {len(rows)} daily closes · share {share:+.2f}% (TWAP) · "
                f"passive holdings {hold:+.2f}% (LOW) · returns in BUCK")
    return ("Current economy  /  " + path.stem, subtitle, replay["tokens"], rows,
            "leaned portfolio weight", path.name)


BLUE, RUST = "#145a87", "#a74d20"


def comparison_page(pdf, title, active, maintenance):
    days = [r["day"] for r in active]
    if days != [r["day"] for r in maintenance] or any(
        a["prices"] != b["prices"] for a, b in zip(active, maintenance)
    ):
        raise ValueError("comparison requires the same days and external price paths")
    fig, axes = plt.subplots(2, 1, figsize=(11.7, 8.3), sharex=True)
    fig.subplots_adjust(left=.11, right=.96, bottom=.16, top=.84, hspace=.27)
    fig.text(.11, .94, title, size=20, weight="bold")
    excess = 100 * (active[-1]["share"] / active[0]["share"]
                    - maintenance[-1]["share"] / maintenance[0]["share"])
    fig.text(.11, .885, f"Pair response enabled vs maintenance    •    Share return difference {excess:+.3f} pp", size=12)
    for rows, name, color in ((active, "Active", BLUE), (maintenance, "Maintenance", RUST)):
        for key, label, style in (("share", "share", "-"), ("hold", "passive TOKENs", ":")):
            change = [100 * (r[key] / rows[0][key] - 1) for r in rows]
            axes[0].plot(days, change, style, color=color, label=f"{name} {label}   {change[-1]:+.2f}%")
        for key, style in (("buys", "-"), ("sells", "--")):
            axes[1].plot(days, [r[key] for r in rows], style, color=color, label=f"{name} {key}   {rows[-1][key]}")
    axes[0].set_ylabel("Return from opening close (%)")
    axes[1].set_ylabel("Cumulative basket trades")
    axes[1].set_xlabel("Simulation day")
    for ax in axes:
        ax.legend(loc="upper left", ncols=2, fontsize=9)
        ax.grid(axis="y", alpha=.2)
        ax.spines[["top", "right"]].set_visible(False)
    fig.text(.11, .074, "Maintenance sets the discretionary pair rate to zero. Both arms can still resize the book, "
             "enforce weights and execute arbitrage.", size=10)
    fig.text(.11, .044, "Shares use TWAP marks. Passive opening TOKENs use each world's LOW marks. "
             "All returns are in BUCK before a holder's exit costs.", size=10)
    pdf.savefig(fig)
    plt.close(fig)


def render(pdf_path, title, subtitle, labels, rows, target_name, source, maintenance=None):
    days = np.array([r["day"] for r in rows])
    prices = np.array([r["prices"] for r in rows], dtype=float)
    prices = 100 * prices / prices[0]
    gaps = (np.array([r["weights"] for r in rows]) - np.array([r["targets"] for r in rows])) / 100
    counts = [Counter((t["token"], t["side"]) for t in r["trades"]) for r in rows]
    pairs = list(itertools.combinations(range(len(labels)), 2))
    pages = [(pair, days[-1]) for pair in pairs]
    if days[-1] >= 60:
        pages.insert(0, (pairs[0], 30))
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(pdf_path, metadata={"Title": title, "Subject": "Recorded basket trades and pair policy"}) as pdf:
        if maintenance is not None:
            comparison_page(pdf, title, rows, maintenance)
        for (i, j), end in pages:
            fig, axes = plt.subplots(4, 1, figsize=(11.7, 8.3), sharex=True)
            fig.subplots_adjust(left=.115, right=.97, bottom=.13, top=.87, hspace=.34)
            fig.text(.115, .949, f"{title}    ·    {labels[i]} / {labels[j]}", size=17, weight="bold")
            fig.text(.115, .905, subtitle, size=10)
            rates = [next(p for p in r["pairs"] if p["tokens"] == [i, j]) for r in rows]
            for idx, color in ((i, BLUE), (j, RUST)):
                axes[0].plot(days, prices[:, idx], lw=1.35, color=color, label=labels[idx])
                for side, marker in (("buy", "^"), ("sell", "v")):
                    n = np.array([c[(idx, side)] for c in counts])
                    seen = n > 0
                    offset = -3.8 if side == "buy" else 3.8
                    transform = offset_copy(axes[0].transData, fig=fig, y=offset, units="points")
                    axes[0].scatter(days[seen], prices[seen, idx], transform=transform,
                                    marker=marker, s=17 + 7 * np.sqrt(n[seen]), color=color,
                                    edgecolor="white", linewidth=.30, zorder=3)
                axes[1].plot(days, gaps[:, idx], lw=1.3, color=color)
            handles, _ = axes[0].get_legend_handles_labels()
            handles += [Line2D([], [], ls="", marker=m, color="#333333", label=label)
                        for m, label in (("^", "Buy"), ("v", "Sell"))]
            axes[0].legend(handles=handles, ncol=4, loc="upper right", fontsize=8)
            axes[0].set_ylabel("External price\n(open = 100)")
            axes[1].set_ylabel("Weight gap\n(pp)")
            axes[1].set_title(f"Held weight less {target_name}", loc="left", fontsize=9)
            axes[1].axhline(0, color=".6", lw=.6)
            ready = np.array([p["ready"] == 3 for p in rates])
            def observed(key):
                return np.where(ready, [p[key] for p in rates], np.nan)
            axes[2].fill_between(days, observed("low"), observed("high"), color="#c7daea")
            axes[2].plot(days, observed("rate"), color=BLUE, lw=1.2)
            axes[2].axhline(0, lw=.6, color=".35")
            axes[2].set_title(f"Filtered relative velocity of {labels[i]} / {labels[j]} and its allowance", loc="left", fontsize=9)
            axes[2].set_ylabel("Basis points\nper day")
            for key, a, b, color in (("forward", i, j, BLUE), ("reverse", j, i, RUST)):
                axes[3].plot(days, 100*observed(key), color=color, lw=1.25,
                             label=f"Sell {labels[a]} toward {labels[b]}")
            axes[3].set_ylim(bottom=-2, top=75)
            axes[3].legend(ncol=2, loc="upper right", fontsize=8)
            axes[3].set_ylabel("Pair release rate\n(% of gap / day)")
            axes[3].set_xlabel("Simulation day")
            for ax in axes:
                ax.set_xlim(days[0], end)
                ax.grid(axis="y", alpha=.2)
                ax.spines[["top", "right"]].set_visible(False)
            fig.text(.115, .054, "Triangles mark intervals with basket trades. Pair readings are sampled at interval end. "
                     "Gaps in the signal lines indicate unavailable two-route observations.", size=8)
            fig.text(.115, .030, f"Trades can serve other pairs or portfolio needs. Source: {source}", size=8)
            pdf.savefig(fig)
            plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab", type=Path, help="archived basket-sizing campaign directory")
    parser.add_argument("--profile", default="three-phase")
    parser.add_argument("--replay", type=Path, help="JSON from capture-basket-signals.py")
    parser.add_argument("--maintenance", type=Path, help="matched replay using BasketUpkeepMaintenanceControl")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.lab) == bool(args.replay):
        parser.error("choose one of --lab and --replay")
    if args.maintenance and not args.replay:
        parser.error("--maintenance requires --replay")
    series = lab_series(args.lab, args.profile) if args.lab else replay_series(args.replay)
    maintenance = None
    if args.maintenance:
        control = read(args.maintenance)
        config = control["config"]
        if config["machine"].pop("upkeep") != "BasketUpkeepMaintenanceControl":
            parser.error("expected the maintenance facet")
        active = read(args.replay)["config"]
        active.setdefault("machine", {}).pop("upkeep", None)
        if config != active:
            parser.error("expected matching experiment settings")
        maintenance = control["frames"]
    render(args.output, *series, maintenance=maintenance)


if __name__ == "__main__":
    main()
