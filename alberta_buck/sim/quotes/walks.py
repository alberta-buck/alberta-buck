"""A small, reusable library of normalized random walks (Brownian bridges).

The quote engine turns each monthly anchor pair (A, B) into an hourly path by
laying a random walk over the straight A->B line.  We do NOT need an unbounded
supply of walks: a dozen *normalized* shapes, reused across all months, give
plenty of variety while staying fully deterministic and reproducible.

Each shape is a Brownian *bridge*: a random walk pinned to 0 at both ends, so
when it is overlaid on the A->B baseline the path lands exactly on A and B.
Shapes are grouped into roughness TIERS (smooth / medium / jagged).  A segment
picks a tier from its local month-to-month volatility and a shape within the
tier from a deterministic hash of (token, year, month) -- so the *amount* of
intra-month wiggle mirrors the real series' variability, while the *texture*
varies month to month and never repeats predictably.

Stored at a fixed resolution and sampled by fraction s in [0, 1]; the same
shape serves segments of any hour-count (months are 28-31 days).  Pure stdlib.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

RESOLUTION = 2048          # samples per normalized shape
MASTER_SEED = 0xB0C_2026   # fixes the entire library

# (tier name, EMA smoothing alpha, shapes in tier).  Lower alpha = the gaussian
# increments are more heavily smoothed = a smoother, lower-frequency wiggle;
# alpha=1.0 leaves the raw (jagged) bridge.  Total shapes = 4+4+4 = 12.
TIERS = [
    ("smooth", 0.06, 4),
    ("medium", 0.20, 4),
    ("jagged", 1.00, 4),
]


def _bridge(seed: int, alpha: float, n: int = RESOLUTION) -> list[float]:
    """One normalized Brownian bridge: pinned to 0 at both ends, unit RMS.

    alpha is an EMA smoothing factor applied to the gaussian increments before
    integration, controlling roughness (small alpha -> smooth).
    """
    rng = random.Random(seed)
    # Smoothed gaussian increments.
    incs = []
    ema = 0.0
    for _ in range(n):
        z = rng.gauss(0.0, 1.0)
        ema = alpha * z + (1.0 - alpha) * ema
        incs.append(ema)
    # Integrate to a walk, then subtract the straight line to its endpoint so
    # both ends sit at exactly 0 (the Brownian-bridge correction).
    walk = [0.0]
    acc = 0.0
    for i in range(1, n):
        acc += incs[i]
        walk.append(acc)
    end = walk[-1]
    bridge = [walk[i] - end * (i / (n - 1)) for i in range(n)]
    bridge[0] = 0.0
    bridge[-1] = 0.0
    # Normalize to unit RMS so a caller's amplitude maps to a known dispersion.
    rms = math.sqrt(sum(x * x for x in bridge) / n)
    if rms > 0:
        bridge = [x / rms for x in bridge]
    return bridge


@dataclass
class Shape:
    tier: int
    index: int
    samples: list[float]

    def at(self, s: float) -> float:
        """Sample the shape at fraction s in [0, 1] (linear interpolation)."""
        if s <= 0.0:
            return 0.0
        if s >= 1.0:
            return 0.0
        x = s * (RESOLUTION - 1)
        i = int(x)
        frac = x - i
        return self.samples[i] * (1.0 - frac) + self.samples[i + 1] * frac


class WalkLibrary:
    """The fixed dozen shapes, grouped by tier; deterministic per MASTER_SEED."""

    def __init__(self, master_seed: int = MASTER_SEED):
        self.tiers: list[list[Shape]] = []
        seed = master_seed
        for ti, (_name, alpha, count) in enumerate(TIERS):
            tier_shapes = []
            for si in range(count):
                seed += 1
                tier_shapes.append(Shape(ti, si, _bridge(seed, alpha)))
            self.tiers.append(tier_shapes)

    @property
    def n_tiers(self) -> int:
        return len(self.tiers)

    def shape(self, tier: int, index: int) -> Shape:
        t = self.tiers[tier]
        return t[index % len(t)]


_LIB: WalkLibrary | None = None


def library() -> WalkLibrary:
    """Process-wide singleton; the library is immutable and seed-fixed."""
    global _LIB
    if _LIB is None:
        _LIB = WalkLibrary()
    return _LIB
