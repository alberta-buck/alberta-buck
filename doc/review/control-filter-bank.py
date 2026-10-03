"""Analytical illustration of EquityTurnDirector's daily EMA ladder.

This is a signal-processing calculation, not an economic backtest. It uses
floating-point arithmetic and regular daily samples, rather than Solidity's
fixed-point rounding or the director's missing-day approximation.
Run: MPLCONFIGDIR=/tmp/buck-review-mpl python3 doc/review/control-filter-bank.py
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def main():
    out = Path(__file__).resolve().parent
    windows = np.array([5, 10, 20, 40, 80, 160, 1280])
    alpha = 2 / (windows + 1)
    periods = np.geomspace(2.01, 10000, 1000)
    omega = 2 * np.pi / periods
    response = alpha[:, None] / (
        1 - (1 - alpha[:, None]) * np.exp(-1j * omega[None, :])
    )
    corners = 2 * np.pi / np.arccos(1 - alpha**2 / (2 * (1 - alpha)))

    plt.rcParams.update({"font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False})
    fig, ax = plt.subplots(2, 2, figsize=(13, 8), layout="constrained")
    colors = plt.cm.viridis(np.linspace(0.05, 0.9, len(windows)))
    for k, w in enumerate(windows):
        ax[0, 0].semilogx(periods, abs(response[k]), color=colors[k], label=f"{w} d span")
        ax[1, 0].semilogx(periods, np.angle(response[k], deg=True), color=colors[k])
    ax[0, 0].axhline(1 / np.sqrt(2), color="grey", ls=":", lw=1)
    ax[0, 0].set(title="EMAs: overlapping low-pass responses", ylabel="Amplitude gain")
    ax[0, 0].legend(ncol=2, fontsize=9, loc="upper left")
    ax[1, 0].set(title="Each EMA also delays oscillations", ylabel="Phase shift (degrees)")

    for k in range(len(windows) - 1):
        ax[0, 1].semilogx(periods, abs(response[k] - response[k + 1]),
                         color=colors[k], label=f"{windows[k]} minus {windows[k+1]} d")
    ax[0, 1].set(title="Adjacent differences: broad, overlapping bands", ylabel="Amplitude gain")
    ax[0, 1].legend(ncol=2, fontsize=9)
    for a in (ax[0, 0], ax[0, 1], ax[1, 0]):
        a.set(xlabel="Input oscillation period (days)", xlim=(2, 10000))
        a.grid(alpha=0.2)

    # A single stationary 80-day sinusoid, warmed up for the longest EMA.
    times = np.arange(-15000, 161)
    x = np.cos(2 * np.pi * times / 80)
    m = np.full(7, x[0])
    excursion, votes = [], []
    for value in x:
        step = alpha * (value - m)
        m += step
        e = value - m[-1]
        excursion.append(e)
        votes.append(np.count_nonzero(step[:-1] * e < 0))
    mask = times >= 0
    t = times[mask]
    v = np.asarray(votes)[mask]
    ax[1, 1].plot(t, np.asarray(excursion)[mask], color="#204b73", label="Excursion from anchor")
    ax[1, 1].fill_between(t, -1.15, 1.15, where=v >= 4, step="mid",
                          color="#7db68c", alpha=0.3, label="4 of 6 vote toward anchor")
    ax[1, 1].axhline(0, color="grey", lw=0.7)
    ax[1, 1].set(title="The quorum is a turn gate, not a phase estimate",
                 xlabel="Time (days)", ylabel="Synthetic excursion", xlim=(0, 160), ylim=(-1.15, 1.15))
    ax[1, 1].legend(fontsize=9, loc="lower left")
    ax[1, 1].grid(alpha=0.2)
    fig.suptitle("BUCK EMA ladder: frequency interpretation at one sample per day", fontsize=15)
    fig.savefig(out / "control-filter-bank.svg")
    fig.savefig(out / "control-filter-bank.png", dpi=150)
    plt.close(fig)
    print("span_days,half_power_period_days,low_frequency_delay_days")
    for w, p in zip(windows, corners):
        print(f"{w},{p:.3f},{(w-1)/2:.3f}")


if __name__ == "__main__":
    main()
