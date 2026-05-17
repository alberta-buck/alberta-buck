#!/usr/bin/env python3
"""Plot the stabilizer routing simulation snapshot."""

import json
import os
import sys

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib not available; skipping plot")
    sys.exit(0)

HERE = os.path.dirname(__file__)
with open(os.path.join(HERE, "snapshot.json")) as f:
    d = json.load(f)

fig, axes = plt.subplots(2, 2, figsize=(14, 10))
fig.suptitle("Stabilizer Routing — 12-Month Arbitrage Simulation", fontsize=14)

# Panel 1: PAXG spot vs reference.
ax = axes[0, 0]
ax.plot(d["day"], [v / 1e6 for v in d["paxg_spot"]], label="PAXG spot", lw=1)
ax.plot(d["day"], [v / 1e6 for v in d["paxg_ref"]], label="PAXG ref", lw=1, ls="--")
ax.set_title("PAXG/USDC")
ax.set_ylabel("Price (USD)")
ax.legend(fontsize=8)

# Panel 2: cbBTC spot vs reference.
ax = axes[0, 1]
ax.plot(d["day"], [v / 1e6 for v in d["cbbtc_spot"]], label="cbBTC spot", lw=1)
ax.plot(d["day"], [v / 1e6 for v in d["cbbtc_ref"]], label="cbBTC ref", lw=1, ls="--")
ax.set_title("cbBTC/USDC")
ax.legend(fontsize=8)

# Panel 3: AOIL spot vs reference.
ax = axes[1, 0]
ax.plot(d["day"], [v / 1e6 for v in d["aoil_spot"]], label="AOIL spot", lw=1)
ax.plot(d["day"], [v / 1e6 for v in d["aoil_ref"]], label="AOIL ref", lw=1, ls="--")
ax.set_title("AOIL/USDC")
ax.set_xlabel("Day")
ax.set_ylabel("Price (USD)")
ax.legend(fontsize=8)

# Panel 4: Total agent USDC balance.
ax = axes[1, 1]
ax.plot(d["day"], [v / 1e6 for v in d["agent_usdc"]], lw=1.5, color="green")
ax.set_title("Agent USDC Balance")
ax.set_xlabel("Day")
ax.set_ylabel("USDC (millions)")
ax.axhline(y=10_000_000, color="gray", ls=":", lw=1)  # initial $10M

out = os.path.join(HERE, "plot.png")
plt.tight_layout()
plt.savefig(out, dpi=120)
print(f"Plot saved to {out}")
