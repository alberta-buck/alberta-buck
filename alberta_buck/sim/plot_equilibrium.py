"""Render the equilibrium-sim result to images/equilibrium-sim.png.

Workflow:
  1.  make sim-run-equilibrium    # writes test/vectors/equilibrium-sim.json
  2.  make sim-plot-equilibrium   # reads JSON, writes images/equilibrium-sim.png

Seven stacked panes (shared x = Day) tell the BUCK-K feedback story of a run
that is repeatedly perturbed by periodic regime changes and re-settles:

  1. basketValueInBuck vs the 1.0 parity setpoint, with buckK (the K-scaled
     LTV cap the controller moves) on a twin axis -- the headline.  Applied
     experiment interventions are marked as labelled vertical lines.
  2. buck_usd: the floating BUCK/USDC spot vs 1.0 parity -- the
     discount/premium the counter-cyclical savers trade against.
  3. PID internals P (ppm error), D (ppm dError) left + I (ppm*s) twin.
  4. Monetary aggregates: BUCK supply / DM outstanding / saver-held BUCK.
  5. Borrower issuance channel: drawn vs K-scaled limit, the rolling reserve
     (held / required / pending) + cumulative throttle hits -- shows WHY
     issuance lives or dies.
  6. Regime timeline: mean borrower util_target + mean saver base_rate, with
     light vertical lines every REGIME_DAYS marking the regime windows.
  7. Per-token TOKEN/USDC tracking error (spot vs CSV reference).

A vector with meta.experiment also gets the eqmetrics acceptance verdict
printed after the plot.  Input vector / output image are overridable via
EQ_VECTOR / EQ_OUT so a short smoke run and a long run can be plotted
independently.
"""

import json
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

# 6-month regime window (mirrors equilibrium_agents.REGIME_DAYS); kept as a
# local literal so plotting never imports/registers the agent module.
REGIME_DAYS = 182


def _resolve(p: Path) -> Path:
    return p if p.is_absolute() else (REPO / p)


DATA = _resolve(Path(os.environ.get(
    "EQ_VECTOR", REPO / "test" / "vectors" / "equilibrium-sim.json")))
OUT = _resolve(Path(os.environ.get(
    "EQ_OUT", REPO / "images" / "equilibrium-sim.png")))

E6 = 10 ** 6
E18 = 10 ** 18


def _cols(frames):
    """Return (n, col) where col(key, default) extracts a column regardless
    of whether `frames` is a list-of-frames or a dict-of-arrays."""
    if isinstance(frames, dict):
        n = len(frames.get("day", []))

        def col(key, default=0):
            v = frames.get(key)
            return list(v) if v is not None else [default] * n
    else:
        n = len(frames)

        def col(key, default=0):
            return [f.get(key, default) for f in frames]
    return n, col


@pytest.mark.skipif(
    not DATA.exists(),
    reason="equilibrium-sim.json not generated yet; run: "
           "python -m alberta_buck.sim --scenario equilibrium",
)
def test_equilibrium_sim_plot():
    cache_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "alberta-buck-mpl"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = json.loads(DATA.read_text())
    tokens = d.get("tokens", [])
    meta = d.get("meta", {})
    ivs = [iv for iv in meta.get("interventions_applied", [])]
    n, col = _cols(d["frames"])
    days = col("day")

    fig, axes = plt.subplots(7, 1, figsize=(13, 26), sharex=True)

    # ---- Pane 1: basketValue vs parity + buckK ----------------------- #
    ax = axes[0]
    bval = [v / E18 for v in col("basketVal")]
    ax.axhline(1.0, color="black", linestyle="--", linewidth=1.2,
               label="parity setpoint (1.0)")
    ax.plot(days, bval, color="tab:blue", linewidth=1.5,
            label="basketValueInBuck")
    ax.set_ylabel("basket value (BUCK)")
    ax.grid(True, alpha=0.3)
    ax2 = ax.twinx()
    bk = [v / E18 for v in col("buckK")]
    ax2.plot(days, bk, color="tab:red", linewidth=1.4, label="buckK (LTV cap)")
    ax2.set_ylabel("buckK", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")
    l1, la1 = ax.get_legend_handles_labels()
    l2, la2 = ax2.get_legend_handles_labels()
    ax.legend(l1 + l2, la1 + la2, loc="upper left", fontsize=8)
    # Applied experiment interventions: labelled vertical markers.
    for iv in ivs:
        dd = iv.get("applied_day", iv.get("day", 0))
        ok = iv.get("ok", True)
        ax.axvline(dd, color="tab:red" if not ok else "tab:olive",
                   alpha=0.55, linewidth=1.0, linestyle="-.")
        ax.annotate(iv.get("action", "?"), xy=(dd, 1.0),
                    xycoords=("data", "axes fraction"),
                    xytext=(2, -2), textcoords="offset points",
                    rotation=90, va="top", ha="left", fontsize=6.5,
                    color="tab:red" if not ok else "tab:olive")
    ax.set_title("(1) BUCK-K feedback: basket value defended toward parity "
                 "by buckK"
                 + (f"  [{len(ivs)} interventions]" if ivs else ""))

    # ---- Pane 2: BUCK/USDC spot vs parity ---------------------------- #
    ax = axes[1]
    bu = [v / E6 for v in col("buck_usd")]     # USDC per BUCK
    ax.axhline(1.0, color="black", linestyle="--", linewidth=1.2,
               label="parity (1.0)")
    ax.plot(days, bu, color="tab:cyan", linewidth=1.4,
            label="BUCK/USDC spot")
    ax.set_ylabel("USDC per BUCK")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title("(2) BUCK/USDC spot: the discount (buy) / premium (sell) "
                 "the savers trade")

    # ---- Pane 3: PID internals --------------------------------------- #
    ax = axes[2]
    p = col("pid_p")
    i = col("pid_i")
    dd = col("pid_d")
    ax.axhline(0, color="black", alpha=0.3, linewidth=0.8)
    ax.plot(days, p, color="tab:green", linewidth=1.2, label="P (ppm error)")
    ax.plot(days, dd, color="tab:purple", linewidth=1.0, linestyle=":",
            label="D (ppm dError)")
    ax.set_ylabel("P / D (ppm)")
    ax.grid(True, alpha=0.3)
    ax3 = ax.twinx()
    ax3.plot(days, i, color="tab:orange", linewidth=1.4,
             label="I (ppm*s integral)")
    ax3.set_ylabel("I (ppm*s)", color="tab:orange")
    ax3.tick_params(axis="y", labelcolor="tab:orange")
    l1, la1 = ax.get_legend_handles_labels()
    l2, la2 = ax3.get_legend_handles_labels()
    ax.legend(l1 + l2, la1 + la2, loc="upper left", fontsize=8)
    ax.set_title("(3) PID internals (integral-dominant K trim)")

    # ---- Pane 4: monetary aggregates --------------------------------- #
    ax = axes[3]
    supply = [v / E6 for v in col("supply")]
    outb = [v / E6 for v in col("dmOutstanding")]
    sav = [v / E6 for v in col("saver_hold")]
    ax.plot(days, supply, color="tab:blue", linewidth=1.5,
            label="BUCK total supply")
    ax.plot(days, outb, color="tab:orange", linewidth=1.2, linestyle="--",
            label="DM outstanding principal")
    ax.plot(days, sav, color="tab:green", linewidth=1.2,
            label="saver-held BUCK")
    ax.set_ylabel("BUCK")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title("(4) Monetary aggregates: supply, outstanding, idle savings")

    # ---- Pane 5: borrower issuance channel ---------------------------- #
    ax = axes[4]
    f_lim = [v / E6 for v in col("fat_limit")]
    f_drw = [v / E6 for v in col("fat_drawn")]
    f_res = [v / E6 for v in col("fat_reserve_held")]
    f_req = [v / E6 for v in col("fat_reserve_req")]
    f_pnd = [v / E6 for v in col("fat_pending")]
    ax.plot(days, f_lim, color="tab:red", linewidth=1.2, linestyle="--",
            label="creditLimit (K-scaled)")
    ax.plot(days, f_drw, color="tab:blue", linewidth=1.5, label="drawn")
    ax.plot(days, f_res, color="tab:green", linewidth=1.1,
            label="reserve held (escrow)")
    ax.plot(days, f_req, color="tab:olive", linewidth=1.0, linestyle=":",
            label="reserve required (rolling)")
    ax.plot(days, f_pnd, color="tab:purple", linewidth=1.0, linestyle=":",
            label="pending release")
    ax.set_ylabel("BUCK")
    ax.grid(True, alpha=0.3)
    ax5c = ax.twinx()
    thr = col("fat_throttled")
    ax5c.step(days, thr, where="post", color="tab:brown", linewidth=1.0,
              label="throttle hits (cum)")
    ax5c.set_ylabel("throttled", color="tab:brown")
    ax5c.tick_params(axis="y", labelcolor="tab:brown")
    l1, la1 = ax.get_legend_handles_labels()
    l2, la2 = ax5c.get_legend_handles_labels()
    ax.legend(l1 + l2, la1 + la2, loc="upper left", fontsize=8, ncol=2)
    ax.set_title("(5) Borrower issuance channel: drawn vs limit, the rolling "
                 "funding reserve, throttle")

    # ---- Pane 6: regime timeline ------------------------------------- #
    ax = axes[5]
    rutil = col("regime_util")
    rsav = [v / E6 for v in col("regime_saver")]     # USDC/step
    ax.step(days, rutil, where="post", color="tab:red", linewidth=1.4,
            label="mean borrower util_target")
    ax.set_ylabel("util_target", color="tab:red")
    ax.tick_params(axis="y", labelcolor="tab:red")
    ax.grid(True, alpha=0.3)
    ax5 = ax.twinx()
    ax5.step(days, rsav, where="post", color="tab:brown", linewidth=1.2,
             label="mean saver base_rate")
    ax5.set_ylabel("base_rate (USDC/step)", color="tab:brown")
    ax5.tick_params(axis="y", labelcolor="tab:brown")
    # Light vertical lines every REGIME_DAYS mark the regime windows.
    if days:
        dmax = days[-1]
        k = REGIME_DAYS
        while k <= dmax:
            ax.axvline(k, color="grey", alpha=0.25, linewidth=0.9)
            k += REGIME_DAYS
    l1, la1 = ax.get_legend_handles_labels()
    l2, la2 = ax5.get_legend_handles_labels()
    ax.legend(l1 + l2, la1 + la2, loc="upper left", fontsize=8)
    ax.set_title("(6) Regime timeline: primary knobs shift every "
                 f"{REGIME_DAYS} days (grey lines)")

    # ---- Pane 7: per-token TOKEN/USDC tracking error ----------------- #
    ax = axes[6]
    spot = col("spotUsdc")
    ref = col("refUsd")
    ntok = len(tokens)
    if ntok == 0 and spot and isinstance(spot[0], list):
        ntok = len(spot[0])
    plotted = False
    for j in range(ntok):
        errs = []
        for t in range(n):
            sv = spot[t][j] if t < len(spot) and j < len(spot[t]) else 0
            rv = ref[t][j] if t < len(ref) and j < len(ref[t]) else 0
            errs.append(100.0 * (sv - rv) / max(1, rv))
        label = tokens[j] if j < len(tokens) else f"tok{j}"
        ax.plot(days, errs, linewidth=1.0, label=label)
        plotted = True
    ax.axhline(0, color="black", alpha=0.3, linewidth=0.8)
    ax.set_ylabel("tracking err (%)")
    ax.set_xlabel("Day")
    ax.grid(True, alpha=0.3)
    if plotted:
        ax.legend(loc="upper left", fontsize=8, ncol=2)
    ax.set_title("(7) TOKEN/USDC tracking error (spot vs CSV reference)")

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=120)
    plt.close(fig)

    # ---- Convergence summary ----------------------------------------- #
    try:
        shown = OUT.relative_to(REPO)
    except ValueError:
        shown = OUT
    print(f"\nWrote {shown}  ({len(days)} days, 7 panes)")

    bval_c = [v / E18 for v in col("basketVal")]
    buckk_c = [v / E18 for v in col("buckK")]
    pidi_c = col("pid_i")
    supply_c = [v / E6 for v in col("supply")]
    sav_c = [v / E6 for v in col("saver_hold")]

    def _row(label, idx):
        print(f"  {label:5s} day {days[idx]:4d}  "
              f"basketVal={bval_c[idx]:.6f}  buckK={buckk_c[idx]:.6f}  "
              f"I={pidi_c[idx]:,}  supply={supply_c[idx]:,.0f}  "
              f"saverHold={sav_c[idx]:,.0f}")

    if n:
        _row("first", 0)
        _row("mid", n // 2)
        _row("last", n - 1)
        print(f"  final basketVal deviation "
              f"{100 * (bval_c[-1] - 1.0):+.3f}% from parity")

    # ---- Acceptance verdict (parity-with-interior-K gate) ------------ #
    from alberta_buck.sim import eqmetrics
    st = eqmetrics.summarize(DATA)
    ok, reasons = eqmetrics.accept(st)
    print(f"  tail: basketVal {st['bv_tail_mean']:.4f} +/- "
          f"{st['bv_tail_std']:.4f}  K {st['k_tail_mean']:.3f} "
          f"(railed {100 * st['k_tail_rail_frac']:.0f}%)  "
          f"issued ${st['issued_tail_m']:.2f}M retired "
          f"${st['retired_tail_m']:.2f}M throttled "
          f"{100 * st['throttle_tail_frac']:.0f}%")
    print("  ACCEPTANCE: " + ("PASS" if ok else "FAIL -- " + "; ".join(reasons)))


if __name__ == "__main__":
    test_equilibrium_sim_plot()
