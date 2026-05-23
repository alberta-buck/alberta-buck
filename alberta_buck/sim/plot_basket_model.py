"""Render the basket-model sim result to images/basket-model.png."""

import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DATA = REPO / "test" / "vectors" / "basket-model.json"
OUT = REPO / "images" / "basket-model.png"


@pytest.mark.skipif(not DATA.exists(), reason="run basket_model.py first")
def test_basket_model_plot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = json.loads(DATA.read_text())
    names = d["tokens"]
    fr = d["frames"]
    days = [f["day"] for f in fr]
    colors = ["tab:orange", "tab:blue", "tab:green"]

    fig, axes = plt.subplots(4, 1, figsize=(13, 14), sharex=True)

    # ---- Panel 1: pool prices (BUCK per token) ------------------------ #
    ax = axes[0]
    for t, sym in enumerate(names):
        pp = [f["poolPrices"].get(sym, 0) for f in fr]
        ax.plot(days, pp, color=colors[t], linewidth=1.4, label=f"{sym}")
    ax.set_ylabel("BUCK / token")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.set_title("Pool prices (BUCK per whole token)")

    # ---- Panel 2: value weights (actual vs target) -------------------- #
    ax = axes[1]
    handles = []
    for t, sym in enumerate(names):
        aw = [f["actualWeights"].get(sym, 0) for f in fr]
        tw = [f["targetWeights"].get(sym, 0) for f in fr]
        l1, = ax.plot(days, aw, color=colors[t], linewidth=1.4, label=f"{sym} actual")
        l2, = ax.plot(days, tw, color=colors[t], linewidth=0.8, linestyle="--",
                      label=f"{sym} target")
        handles.extend([l1, l2])
    ax.axhline(1.0 / 3, color="black", alpha=0.15, linewidth=0.5)
    ax.set_ylabel("value weight")
    ax.legend(handles=handles, loc="upper left", fontsize=7, ncol=2)
    ax.grid(True, alpha=0.3)
    ax.set_title("Pool value weights: actual vs basket target")

    # ---- Panel 3: pool BUCK reserves ---------------------------------- #
    ax = axes[2]
    for t, sym in enumerate(names):
        br = [f["poolBuckRes"].get(sym, 0) for f in fr]
        ax.plot(days, br, color=colors[t], linewidth=1.4, label=f"{sym} BUCK")
    ax.set_ylabel("BUCK reserves")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.set_title("Pool BUCK reserves (arb flow between pools)")

    # ---- Panel 4: treasury compounding -------------------------------- #
    ax = axes[3]
    handles4 = []
    nav = [f["nav"] for f in fr]
    l1, = ax.plot(days, nav, color="tab:blue", linewidth=1.5,
                  label="total NAV (BUCK)")
    handles4.append(l1)
    out = [f["totalOutstanding"] for f in fr]
    l2, = ax.plot(days, out, color="tab:orange", linewidth=1.2, linestyle="--",
                  label="outstanding (BUCK principal)")
    handles4.append(l2)
    tb = [f["treasuryBuck"] for f in fr]
    l3, = ax.plot(days, tb, color="tab:green", linewidth=1.4,
                  label="treasury BUCK (retained profit)")
    handles4.append(l3)
    ax.set_ylabel("BUCK")
    ax.set_xlabel("Day")
    ax.grid(True, alpha=0.3)

    ax2 = ax.twinx()
    dm_a = [f["dmActive"] for f in fr]
    l4, = ax2.plot(days, dm_a, color="tab:red", linewidth=1.0, linestyle=":",
                   label="active DM agents")
    handles4.append(l4)
    ax2.set_ylabel("active DM agents", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")

    ax.legend(handles=handles4, loc="upper left", fontsize=7)
    ax.set_title("Treasury compounding & DM agent activity")

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=120)
    plt.close(fig)

    f = fr[-1]
    print(f"\nWrote {OUT.relative_to(REPO)}  ({len(days)} days)")
    print(f"  NAV: {f['nav']:,.0f}  outstanding: {f['totalOutstanding']:,.0f}")
    print(f"  treasury: {f['treasuryBuck']:,.0f} BUCK  "
          f"({f['treasuryBuck']/f['nav']*100:.1f}% of NAV)")
    print(f"  DM: {f['dmExited']} exited, {f['dmActive']} active")
    for s in names:
        print(f"  {s:5s}  price={f['poolPrices'].get(s,0):.4f}  "
              f"actual={f['actualWeights'].get(s,0):.4f}  "
              f"target={f['targetWeights'].get(s,0):.4f}")


if __name__ == "__main__":
    test_basket_model_plot()
