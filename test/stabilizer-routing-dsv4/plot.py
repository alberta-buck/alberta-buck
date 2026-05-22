#!/usr/bin/env python3
"""Plot the stabilizer routing simulation snapshot."""
import json, os, sys
try:
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib not available"); sys.exit(0)

HERE = os.path.dirname(__file__)
with open(os.path.join(HERE, "snapshot.json")) as f:
    d = json.load(f)

fig, axes = plt.subplots(2, 2, figsize=(14, 10))
fig.suptitle("Stabilizer Routing -- 90-Day Arbitrage Simulation", fontsize=14)

for ax, tok, label in [
    (axes[0,0], "paxg", "PAXG/USDC"),
    (axes[0,1], "cbbtc", "cbBTC/USDC"),
    (axes[1,0], "aoil", "AOIL/USDC"),
]:
    ax.plot(d["day"], [v/1e6 for v in d[f"{tok}_s"]], label="spot", lw=1)
    ax.plot(d["day"], [v/1e6 for v in d[f"{tok}_r"]], label="ref", lw=1, ls="--")
    ax.set_title(label); ax.legend(fontsize=8)

# Panel 4: BUCK/USDC spot.
ax = axes[1,1]
ax.plot(d["day"], [v/1e6 for v in d.get("buck_s", d["paxg_s"])], lw=1.5, color="green")
ax.set_title("BUCK/USDC Spot"); ax.set_xlabel("Day")

out = os.path.join(HERE, "plot.png")
plt.tight_layout(); plt.savefig(out, dpi=120)
print(f"Plot saved to {out}")
