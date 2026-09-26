#!/usr/bin/env python3
"""Generate TruthStake simulation plots using real XAUUSD data.

Run: python -m alberta_buck.test.gen_truthstake_plots
Produces plots and tables for doc/README-truthstake.org.
"""

import csv
import math
import random
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from alberta_buck.truthstake import KalmanOracle, Oracle, Reporter

PLOT_DIR = Path(__file__).parent
CSV_PATH = PLOT_DIR / "XAUUSD_Hourly.csv"


def load_gold_prices(stride=4):
    """Load Gold prices from hourly XAUUSD CSV, sampled every *stride* hours."""
    rows = []
    with open(CSV_PATH) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(float(row["Close"]))
    prices = rows[::stride]
    return prices


def run_scenario(prices, honest_specs, adversary_specs=None,
                 challenger_spec=None, challenge_threshold=None,
                 Q=0.0005, min_stake=10.0, tolerance=0.03, seed=42):
    """Run oracle simulation over price series.

    honest_specs:    list of (name, noise_std) for honest reporters
    adversary_specs: list of (name, noise_std, bias_fn) -- bias_fn(round_id) -> bias
    challenger_spec: (name,) or None
    challenge_threshold: sigma threshold to trigger challenge, or None

    Returns dict with all results.
    """
    initial = prices[0]
    oracle = Oracle(initial_estimate=initial, Q=Q, min_stake=min_stake,
                    tolerance=tolerance, early_halflife=3.0)

    honest = []
    for name, noise_std in honest_specs:
        r = Reporter(name, stake=100000)
        oracle.register(r)
        honest.append((r, noise_std))

    adversaries = []
    if adversary_specs:
        for name, noise_std, bias_fn in adversary_specs:
            r = Reporter(name, stake=100000)
            oracle.register(r)
            adversaries.append((r, noise_std, bias_fn))

    challenger = None
    if challenger_spec:
        challenger = Reporter(challenger_spec[0], stake=100000)
        oracle.register(challenger)

    rng = random.Random(seed)

    estimates = []
    errors = []
    pnl_honest = []
    pnl_adversary = []
    pnl_challenger = []
    h_total = a_total = c_total = 0
    challenged_rounds = []

    for rid, price in enumerate(prices):
        oracle.open_round()

        # Build and shuffle submissions
        all_subs = []
        for r, noise_std in honest:
            noise = rng.gauss(0, noise_std)
            value = price * (1.0 + noise)
            all_subs.append((r, value))

        for r, noise_std, bias_fn in adversaries:
            bias = bias_fn(rid)
            noise = rng.gauss(0, noise_std) if noise_std > 0 else 0.0
            value = price * (1.0 + noise + bias)
            all_subs.append((r, value))

        if challenger:
            noise = rng.gauss(0, 0.003)
            value = price * (1.0 + noise)
            all_subs.append((challenger, value))

        rng.shuffle(all_subs)
        for r, value in all_subs:
            try:
                oracle.submit(r, value, stake=min_stake)
            except ValueError:
                pass

        # Challenge logic
        if challenger and challenge_threshold and rid >= 3:
            est = oracle.kalman.x
            P = oracle.kalman.P
            if P > 0:
                sigma = math.sqrt(P)
                triggered = False
                for sub in oracle.current_round.submissions:
                    norm_val = oracle._normalize(sub.value)
                    if sigma > 0 and abs(norm_val - est) > challenge_threshold * sigma:
                        try:
                            oracle.challenge(challenger)
                            challenged_rounds.append(rid)
                            # Honest reporters re-submit after challenge
                            for hr, ns in honest:
                                noise = rng.gauss(0, ns)
                                val = price * (1.0 + noise)
                                try:
                                    oracle.submit(hr, val, stake=min_stake)
                                except ValueError:
                                    pass
                            # Challenger also re-submits
                            noise = rng.gauss(0, 0.003)
                            val = price * (1.0 + noise)
                            try:
                                oracle.submit(challenger, val, stake=min_stake)
                            except ValueError:
                                pass
                            triggered = True
                        except ValueError:
                            pass
                        break

        result = oracle.settle(true_price=price)
        estimates.append(result["settled_value"])
        errors.append(result["estimate_error"] * 100 if result["estimate_error"] else 0)

        # Track P&L
        payouts = result["payouts"]
        for name, payout in payouts.items():
            net = payout - min_stake
            if any(name == r.name for r, _ in honest):
                h_total += net
            elif any(name == r.name for r, _, _ in adversaries):
                a_total += net
            elif challenger and name == challenger.name:
                c_total += net
        pnl_honest.append(h_total)
        pnl_adversary.append(a_total)
        pnl_challenger.append(c_total)

    return {
        "estimates": estimates,
        "errors": errors,
        "pnl_honest": pnl_honest,
        "pnl_adversary": pnl_adversary,
        "pnl_challenger": pnl_challenger,
        "challenged_rounds": challenged_rounds,
        "honest": honest,
        "adversaries": adversaries,
        "challenger": challenger,
    }


def main():
    # Sample every 4 hours -> ~75 rounds
    prices = load_gold_prices(stride=4)
    n = len(prices)
    print(f"Loaded {n} price samples (4-hourly): ${min(prices):.0f} - ${max(prices):.0f}")

    # ======================================================================
    # Scenario 1: All honest -- baseline tracking
    # ======================================================================
    s1 = run_scenario(
        prices,
        honest_specs=[(f"honest_{i}", 0.003) for i in range(5)],
    )

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True,
                                    gridspec_kw={"height_ratios": [2, 1]})
    ax1.plot(range(n), prices, "k-", linewidth=2, label="True Gold price (XAU/USD)")
    ax1.plot(range(n), s1["estimates"], "b--", linewidth=1.5, label="Oracle estimate")
    ax1.set_ylabel("Price (USD)")
    ax1.set_title("Scenario 1: 5 Honest Reporters -- Oracle Tracks Real Gold Prices")
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    ax2.plot(range(n), s1["errors"], "r-", linewidth=1)
    ax2.axhline(y=3, color="orange", linestyle="--", alpha=0.5, label="3% tolerance")
    ax2.set_ylabel("Error (%)")
    ax2.set_xlabel("Round (4-hour intervals)")
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    plt.tight_layout()
    fig.savefig(str(PLOT_DIR / "truthstake_s1_honest.png"), dpi=150)
    plt.close(fig)
    print(f"S1: avg error = {sum(s1['errors'])/n:.3f}%")

    # ======================================================================
    # Scenario 2: Single persistent manipulator (+5% bias)
    # ======================================================================
    s2 = run_scenario(
        prices,
        honest_specs=[(f"honest_{i}", 0.003) for i in range(4)],
        adversary_specs=[("adversary", 0.0, lambda rid: 0.05)],
    )

    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True,
                              gridspec_kw={"height_ratios": [2, 1, 1.5]})
    axes[0].plot(range(n), prices, "k-", linewidth=2, label="True price")
    axes[0].plot(range(n), s2["estimates"], "b--", linewidth=1.5, label="Oracle estimate")
    axes[0].set_ylabel("Price (USD)")
    axes[0].set_title("Scenario 2: 4 Honest + 1 Adversary (+5%) -- Liar Bleeds Stake")
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(range(n), s2["errors"], "r-", linewidth=1)
    axes[1].axhline(y=3, color="orange", linestyle="--", alpha=0.5, label="3% tolerance")
    axes[1].set_ylabel("Error (%)")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    axes[2].plot(range(n), s2["pnl_honest"], "g-", linewidth=2, label="Honest (4 total)")
    axes[2].plot(range(n), s2["pnl_adversary"], "r-", linewidth=2, label="Adversary (+5%)")
    axes[2].axhline(y=0, color="black", linewidth=0.5)
    axes[2].set_ylabel("Cumulative P&L")
    axes[2].set_xlabel("Round (4-hour intervals)")
    axes[2].legend()
    axes[2].grid(True, alpha=0.3)
    plt.tight_layout()
    fig.savefig(str(PLOT_DIR / "truthstake_s2_manipulator.png"), dpi=150)
    plt.close(fig)
    print(f"S2: adversary P&L = {s2['pnl_adversary'][-1]:.0f}, "
          f"honest P&L = {s2['pnl_honest'][-1]:.0f}")

    # ======================================================================
    # Scenario 3: Sleeper cartel -- honest for 20 rounds, then +8% attack
    # ======================================================================
    attack_start = 20

    def sleeper_bias(rid):
        return 0.0 if rid < attack_start else 0.08

    s3 = run_scenario(
        prices,
        honest_specs=[(f"honest_{i}", 0.003) for i in range(3)],
        adversary_specs=[
            (f"cartel_{i}", 0.002, sleeper_bias) for i in range(3)
        ],
        challenger_spec=("challenger",),
        challenge_threshold=2.0,
    )

    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True,
                              gridspec_kw={"height_ratios": [2, 1, 1.5]})

    ax = axes[0]
    ax.plot(range(n), prices, "k-", linewidth=2, label="True price")
    ax.plot(range(n), s3["estimates"], "b--", linewidth=1.5, label="Oracle estimate")
    ax.axvline(x=attack_start, color="red", linestyle=":", alpha=0.5, label="Attack begins")
    for cr in s3["challenged_rounds"]:
        ax.axvline(x=cr, color="purple", alpha=0.2, linewidth=1)
    ax.set_ylabel("Price (USD)")
    ax.set_title("Scenario 3: Sleeper Cartel (3) -- Honest 20 Rounds, Then +8% Attack")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.plot(range(n), s3["errors"], "r-", linewidth=1)
    ax.axhline(y=3, color="orange", linestyle="--", alpha=0.5, label="3% tolerance")
    ax.axvline(x=attack_start, color="red", linestyle=":", alpha=0.5)
    for cr in s3["challenged_rounds"]:
        ax.axvline(x=cr, color="purple", alpha=0.2, linewidth=1)
    ax.set_ylabel("Error (%)")
    ax.legend()
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    ax.plot(range(n), s3["pnl_honest"], "g-", linewidth=2, label="Honest (3 total)")
    ax.plot(range(n), s3["pnl_adversary"], "r-", linewidth=2, label="Cartel (3 total)")
    ax.plot(range(n), s3["pnl_challenger"], "b-", linewidth=1.5, label="Challenger")
    ax.axhline(y=0, color="black", linewidth=0.5)
    ax.axvline(x=attack_start, color="red", linestyle=":", alpha=0.5)
    for cr in s3["challenged_rounds"]:
        ax.axvline(x=cr, color="purple", alpha=0.2, linewidth=1)
    ax.set_ylabel("Cumulative P&L")
    ax.set_xlabel("Round (4-hour intervals; red = attack start, purple = challenge)")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    fig.savefig(str(PLOT_DIR / "truthstake_s3_cartel.png"), dpi=150)
    plt.close(fig)
    print(f"S3: cartel P&L = {s3['pnl_adversary'][-1]:.0f}, "
          f"honest P&L = {s3['pnl_honest'][-1]:.0f}, "
          f"challenger P&L = {s3['pnl_challenger'][-1]:.0f}, "
          f"challenges = {len(s3['challenged_rounds'])}")

    # ======================================================================
    # Scenario 4: Brief manipulation burst -- 5 rounds of attack
    # ======================================================================
    burst_start = 30
    burst_end = 35

    def burst_bias(rid):
        return 0.10 if burst_start <= rid < burst_end else 0.0

    s4 = run_scenario(
        prices,
        honest_specs=[(f"honest_{i}", 0.003) for i in range(4)],
        adversary_specs=[
            (f"burst_{i}", 0.0, burst_bias) for i in range(2)
        ],
    )

    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True,
                              gridspec_kw={"height_ratios": [2, 1, 1.5]})

    ax = axes[0]
    ax.plot(range(n), prices, "k-", linewidth=2, label="True price")
    ax.plot(range(n), s4["estimates"], "b--", linewidth=1.5, label="Oracle estimate")
    ax.axvspan(burst_start, burst_end, color="red", alpha=0.1, label="Attack window")
    ax.set_ylabel("Price (USD)")
    ax.set_title("Scenario 4: Brief Manipulation Burst (5 rounds at +10%)")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.plot(range(n), s4["errors"], "r-", linewidth=1)
    ax.axhline(y=3, color="orange", linestyle="--", alpha=0.5, label="3% tolerance")
    ax.axvspan(burst_start, burst_end, color="red", alpha=0.1)
    ax.set_ylabel("Error (%)")
    ax.legend()
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    ax.plot(range(n), s4["pnl_honest"], "g-", linewidth=2, label="Honest (4 total)")
    ax.plot(range(n), s4["pnl_adversary"], "r-", linewidth=2, label="Burst attackers (2)")
    ax.axhline(y=0, color="black", linewidth=0.5)
    ax.axvspan(burst_start, burst_end, color="red", alpha=0.1)
    ax.set_ylabel("Cumulative P&L")
    ax.set_xlabel("Round (4-hour intervals)")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    fig.savefig(str(PLOT_DIR / "truthstake_s4_burst.png"), dpi=150)
    plt.close(fig)
    print(f"S4: burst attacker P&L = {s4['pnl_adversary'][-1]:.0f}, "
          f"honest P&L = {s4['pnl_honest'][-1]:.0f}")

    # ======================================================================
    # Summary: combined 4-panel chart
    # ======================================================================
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    ax = axes[0, 0]
    ax.plot(range(n), prices, "k-", linewidth=2, label="True price")
    ax.plot(range(n), s1["estimates"], "g--", linewidth=1, alpha=0.8, label="S1: all honest")
    ax.plot(range(n), s2["estimates"], "b--", linewidth=1, alpha=0.8, label="S2: +1 adversary")
    ax.set_ylabel("Price (USD)")
    ax.set_title("Oracle Accuracy: Honest vs. Under Attack")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[0, 1]
    ax.plot(range(n), s1["errors"], "g-", linewidth=1, alpha=0.7, label="S1: all honest")
    ax.plot(range(n), s2["errors"], "b-", linewidth=1, alpha=0.7, label="S2: +1 adversary")
    ax.plot(range(n), s3["errors"], "r-", linewidth=1, alpha=0.7, label="S3: sleeper cartel")
    ax.axhline(y=3, color="orange", linestyle="--", alpha=0.5)
    ax.set_ylabel("Estimate Error (%)")
    ax.set_title("Oracle Error Across All Scenarios")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[1, 0]
    ax.plot(range(n), s2["pnl_honest"], "g-", linewidth=2, label="Honest reporters")
    ax.plot(range(n), s2["pnl_adversary"], "r-", linewidth=2, label="Adversary (+5%)")
    ax.axhline(y=0, color="black", linewidth=0.5)
    ax.set_ylabel("Cumulative P&L")
    ax.set_xlabel("Round")
    ax.set_title("S2: Single Manipulator -- Honest Profits")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[1, 1]
    ax.plot(range(n), s3["pnl_honest"], "g-", linewidth=2, label="Honest (3)")
    ax.plot(range(n), s3["pnl_adversary"], "r-", linewidth=2, label="Cartel (3)")
    ax.plot(range(n), s3["pnl_challenger"], "b-", linewidth=1.5, label="Challenger")
    ax.axhline(y=0, color="black", linewidth=0.5)
    ax.axvline(x=attack_start, color="red", linestyle=":", alpha=0.5)
    ax.set_ylabel("Cumulative P&L")
    ax.set_xlabel("Round")
    ax.set_title("S3: Sleeper Cartel -- Challenge Restores Truth")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    plt.suptitle("TruthStake Oracle Simulations on Real Gold (XAUUSD) Prices, May 2025",
                 fontsize=13, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(str(PLOT_DIR / "truthstake_scenarios.png"), dpi=150)
    plt.close(fig)

    print(f"\nAll plots saved to {PLOT_DIR}/")


if __name__ == "__main__":
    main()
