"""What is there to harvest on the control lab's clean three-phase feed?

A frictionless arithmetic check, not an EVM run: 1,000,000 USD of TOKEN held
directly (no LP, no credit), equal declared weights, four samples a day for
120 days. Each rule moves value between the three TOKENs at the feed price,
with and without a 0.3% fee on each of a transfer's two swaps. Pool depth and
price impact are not modelled, so these are ceilings for the basket's own
result, which also holds most of its TOKEN inside TOKEN/BUCK liquidity.

Run: python3 doc/review/rebalance-benchmark.py
     PYTHONPATH=. python3 doc/review/rebalance-benchmark.py commodities

The second form applies the same rules to the lab's commodity profile
(doc/control-lab.toml). Those prices do not return to their start, so its
columns are the gain over holding.
"""
import math
import sys

AMPLITUDE, PERIOD, DAYS, STEPS_PER_DAY = 0.12, 30.0, 120, 4


def feed():
    return [[1 + AMPLITUDE * math.sin(2 * math.pi * (k / STEPS_PER_DAY) / PERIOD + 2 * math.pi * i / 3)
             for i in range(3)] for k in range(DAYS * STEPS_PER_DAY + 1)]


def run(prices, rule, fee=0.0):
    """Returns the ending value, the value moved and the number of rebalances."""
    held = [1e6 / 3 / prices[0][i] for i in range(3)]
    moved_total, count, state = 0.0, 0, {}
    for step, price in enumerate(prices):
        value = [held[i] * price[i] for i in range(3)]
        total = sum(value)
        target = rule(step, price, value, total, state)
        if target is None:
            continue
        moved = sum(max(target[i] - value[i], 0) for i in range(3))
        if moved < 1:
            continue
        scale = (total - 2 * fee * moved) / total        # a sale and a purchase
        held = [target[i] * scale / price[i] for i in range(3)]
        moved_total += moved
        count += 1
    return sum(held[i] * prices[-1][i] for i in range(3)), moved_total, count


def hold(step, price, value, total, state):
    return None


def every(steps):
    def rule(step, price, value, total, state):
        return [total / 3] * 3 if step and step % steps == 0 else None
    return rule


def band(bp):
    def rule(step, price, value, total, state):
        return [total / 3] * 3 if max(abs(v / total - 1 / 3) for v in value) * 1e4 > bp else None
    return rule


def after_turn(retreat):
    """Causal: act once any pair's log ratio has come back `retreat` from its running extreme."""
    def rule(step, price, value, total, state):
        fire = False
        for a in range(3):
            for b in range(a + 1, 3):
                x = math.log(price[a] / price[b])
                high, low, side = state.get((a, b), (x, x, 0))
                high, low = max(high, x), min(low, x)
                if side >= 0 and high - x > retreat:
                    fire, side, low = True, -1, x
                elif side <= 0 and x - low > retreat:
                    fire, side, high = True, 1, x
                state[a, b] = (high, low, side)
        return [total / 3] * 3 if fire else None
    return rule


RULES = [("Hold", hold),
         ("Rebalance to equal weights every sample (6 hours)", every(1)),
         ("Rebalance every 5 days", every(5 * STEPS_PER_DAY)),
         ("Rebalance every 10 days", every(10 * STEPS_PER_DAY)),
         ("Rebalance every 15 days", every(15 * STEPS_PER_DAY)),
         ("Rebalance when a weight is 2 points from target", band(200)),
         ("Rebalance when a weight is 3 points from target", band(300)),
         ("Rebalance after a pair retreats 1% from its extreme", after_turn(0.01)),
         ("Rebalance after a pair retreats 3% from its extreme", after_turn(0.03))]


def configured(name):
    """A profile's feed from the lab configuration, as multiples of its base prices."""
    from alberta_buck.sim.control_lab.model import feed_rows, load_config
    cfg = load_config("doc/control-lab.toml")
    profile = next(p for p in cfg["profiles"] if p["name"] == name)
    return [[p / b for p, b in zip(row["target_usd"], cfg["feed"]["base"])] for row in feed_rows(cfg, profile)]


def main(argv):
    prices = configured(argv[0]) if argv else feed()
    held, _, _ = run(prices, hold)
    print(f"{argv[0] if argv else 'three-phase'}: holding ends at {held:,.0f}; the columns are gains over that")
    print(f"{'rule':52s} {'no fee':>8s} {'0.3%/swap':>9s} {'moved':>10s} {'times':>5s}")
    for name, rule in RULES:
        free, _, _ = run(prices, rule)
        net, moved, count = run(prices, rule, 0.003)
        print(f"{name:52s} {100 * (free / held - 1):+7.2f}% {100 * (net / held - 1):+8.2f}% {moved:10,.0f} {count:5d}")


if __name__ == "__main__":
    main(sys.argv[1:])
