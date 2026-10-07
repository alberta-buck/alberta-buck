"""Experimental causal relative-price observer; no trading or EVM side effects.

All prices are QUOTE units per TOKEN. Each venue snapshot must cover the
same tokens at the same effective time. State lives in N-1 fixed Helmert
contrasts; every pair is a projection of that one state and covariance.
Requires the optional plotting/analysis stack (NumPy).
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from itertools import combinations
import math

import numpy as np


def contrasts(n: int) -> np.ndarray:
    """N x (N-1), orthonormal columns orthogonal to the all-ones vector."""
    if n < 2:
        raise ValueError("need at least two tokens")
    b = np.zeros((n, n - 1))
    for j in range(n - 1):
        b[:j + 1, j] = 1 / math.sqrt((j + 1) * (j + 2))
        b[j + 1, j] = -(j + 1) / math.sqrt((j + 1) * (j + 2))
    return b


@dataclass(frozen=True)
class ObserverConfig:
    jerk_variance: float = 1e-6  # integrated white jerk, log-price^2 / day^5
    noise_floor: float = 5e-4   # per contrast measurement standard deviation
    noise_prior: float = .003
    venue_correlation_prior: float = .5
    noise_memory_days: float = 5.
    max_age_days: float = 1.
    spread_band: float = .002
    speed_floor: float = .0002


@dataclass(frozen=True)
class ResponseConfig:
    release_speed: float = .002  # log ratio/day, quench decay scale
    release_rate: float = .02    # gap fraction/day at a well-observed stall
    closure_gain: float = 20.    # closure log-rate -> gap participation rate
    uncertainty_multiple: float = 1.
    speed_floor: float = .0002
    acceleration_floor: float = .0001
    minimum_release: float = .05

    def __post_init__(self):
        if (not all(math.isfinite(v) for v in asdict(self).values())
                or min(self.release_speed, self.release_rate, self.closure_gain,
                       self.speed_floor, self.acceleration_floor) <= 0
                or self.uncertainty_multiple < 0 or not 0 <= self.minimum_release <= 1):
            raise ValueError("invalid response configuration")


def motion_desire(outward_speed, inward_acceleration, sigma_v, sigma_a, config=None):
    """Release quenching BEFORE reversal; amplify size after closure starts.

    Slow but constant reliable departure remains quenched by the braking
    factor. Near a stall, release need not disappear when acceleration also
    settles to zero. These are heuristic rates, not calibrated probabilities.
    """
    c = config or ResponseConfig()
    upper_departure = max(outward_speed + c.uncertainty_multiple * sigma_v, 0.)
    quench_release = math.exp(-upper_departure / c.release_speed)
    braking = float(np.clip((inward_acceleration - c.uncertainty_multiple * sigma_a)
                           / max(sigma_a, c.acceleration_floor), 0, 1))
    stall = math.exp(-min((max(outward_speed, 0) / c.speed_floor)**2, 700))
    release = quench_release * max(braking, stall)
    closing = max(-outward_speed - c.uncertainty_multiple * sigma_v, 0.)
    eligible = release >= c.minimum_release or closing > 0
    rate = c.release_rate * release + c.closure_gain * closing if eligible else 0.
    return dict(release=release, closing_speed=closing, rate=rate, eligible=eligible)


def transition(dt: float, m: int, jerk_variance: float):
    f = np.array([[1, dt, dt * dt / 2], [0, 1, dt], [0, 0, 1.]])
    q = np.array([[dt**5 / 20, dt**4 / 8, dt**3 / 6],
                  [dt**4 / 8, dt**3 / 3, dt**2 / 2],
                  [dt**3 / 6, dt**2 / 2, dt]]) * jerk_variance
    return np.kron(f, np.eye(m)), np.kron(q, np.eye(m))


class PairObserver:
    """Level/velocity/acceleration filter with correlated venue errors.

    Full-batch covariance is intentional in this Python reference. It avoids
    assimilating redundant pairs as independent measurements. Noise is
    estimated from past third differences: for temporally white measurement
    noise, Var(diff^3 noise) = 20 R. This is a MODEL assumption, not a way to
    identify all market noise. Fast genuine dynamics or serially correlated
    venue errors can invalidate it; retain innovations and coverage metrics.
    """

    def __init__(self, means, venues=("BUCK", "USDC"), config=None):
        self.config = config or ObserverConfig()
        c = self.config
        if (not all(math.isfinite(v) for v in asdict(c).values())
                or min(c.jerk_variance, c.noise_floor, c.noise_prior,
                       c.noise_memory_days, c.max_age_days, c.speed_floor) <= 0
                or not 0 <= c.venue_correlation_prior < 1 or c.spread_band < 0):
            raise ValueError("invalid observer configuration")
        self.means = np.asarray(means, dtype=float)
        if self.means.ndim != 1 or not np.all(np.isfinite(self.means) & (self.means > 0)):
            raise ValueError("positive finite historical reference prices required")
        self.venues = tuple(venues)
        if not self.venues or len(set(self.venues)) != len(self.venues):
            raise ValueError("unique venues required")
        self.b = contrasts(len(self.means))
        self.m = self.b.shape[1]
        self.pairs = list(combinations(range(len(self.means)), 2))
        self.edges = np.array([self.b[i] - self.b[j] for i, j in self.pairs])
        self.x = np.zeros(3 * self.m)
        self.p = np.kron(np.diag([.2**2, .03**2, .02**2]), np.eye(self.m))
        correlation = np.full((len(self.venues), len(self.venues)), c.venue_correlation_prior)
        np.fill_diagonal(correlation, 1.)
        self.r = c.noise_prior**2 * np.kron(correlation, np.eye(self.m))
        self.history = deque(maxlen=4)
        self.last_time = None
        self.last_observed = None
        self.last_dt = None

    def observe(self, day, prices, impact=None, impact_std=None):
        """One epoch, at most once. A missing venue is absent or None.

        impact is a per-venue vector of predicted CURRENT log-price offsets,
        not a flow or an instruction to subtract the last swap repeatedly.
        impact_std supplies uncertainty in those offsets in log-price units.
        No truth, future data, weights or target allocations enter this API.
        """
        if not math.isfinite(day) or (self.last_time is not None and day <= self.last_time):
            raise ValueError("observation epochs must strictly increase")
        impact, impact_std = impact or {}, impact_std or {}
        indices, observations, extra = [], [], []
        routes = {}
        for k, venue in enumerate(self.venues):
            values = prices.get(venue)
            if values is None:
                continue
            values = np.asarray(values, dtype=float)
            u = np.asarray(impact.get(venue, np.zeros(len(self.means))), dtype=float)
            s = np.asarray(impact_std.get(venue, np.zeros(len(self.means))), dtype=float)
            if (values.shape != self.means.shape or u.shape != values.shape or s.shape != values.shape
                    or not np.all(np.isfinite(values) & (values > 0))
                    or not np.all(np.isfinite(u)) or not np.all(np.isfinite(s) & (s >= 0))):
                raise ValueError("snapshot and impact vectors must be finite, complete and correctly oriented")
            z = self.b.T @ (np.log(values / self.means) - u)
            routes[venue] = (self.edges @ z).tolist()
            observations.extend(z)
            indices.extend(range(k * self.m, (k + 1) * self.m))
            extra.append(self.b.T @ np.diag(s * s) @ self.b)
        dt = 0. if self.last_time is None else day - self.last_time
        if dt:
            f, q = transition(dt, self.m, self.config.jerk_variance)
            self.x = f @ self.x
            self.p = f @ self.p @ f.T + q
        self.last_time = day
        nis = None
        disagreement = None
        if indices:
            z = np.asarray(observations)
            h = np.zeros((len(z), len(self.x)))
            for k in range(len(extra)):
                h[k * self.m:(k + 1) * self.m, :self.m] = np.eye(self.m)
            r = self.r[np.ix_(indices, indices)].copy()
            for k, block in enumerate(extra):
                sl = slice(k * self.m, (k + 1) * self.m)
                r[sl, sl] += block
            residual = z - h @ self.x
            innovation = h @ self.p @ h.T + r
            gain = np.linalg.solve(innovation, h @ self.p).T
            nis = float(residual @ np.linalg.solve(innovation, residual) / len(z))
            # Joseph form preserves covariance under finite arithmetic better
            # than subtracting two nearly equal covariance matrices.
            self.x += gain @ residual
            a = np.eye(len(self.x)) - gain @ h
            self.p = a @ self.p @ a.T + gain @ r @ gain.T
            self.p = (self.p + self.p.T) / 2
            self.last_observed = day
            if len(extra) == 2:
                d = z[:self.m] - z[self.m:]
                rd = r[:self.m, :self.m] + r[self.m:, self.m:] - r[:self.m, self.m:] - r[self.m:, :self.m]
                disagreement = float(d @ np.linalg.solve(rd, d) / self.m)
            # Update R AFTER assimilation; this observation cannot inflate its
            # own noise estimate to hide its innovation. Gaps reset differences.
            complete = len(indices) == len(self.r)
            if not complete or (self.last_dt is not None and not math.isclose(dt, self.last_dt)):
                self.history.clear()
            if complete:
                self.history.append(z.copy())
                if len(self.history) == 4 and dt > 0:
                    a0, a1, a2, a3 = self.history
                    d3 = a3 - 3 * a2 + 3 * a1 - a0
                    alpha = -math.expm1(-dt / self.config.noise_memory_days)
                    estimate = (1 - alpha) * self.r + alpha * np.outer(d3, d3) / 20
                    eigenvalues, eigenvectors = np.linalg.eigh(estimate)
                    self.r = (eigenvectors * np.maximum(eigenvalues, self.config.noise_floor**2)) @ eigenvectors.T
        else:
            self.history.clear()
        self.last_dt = dt
        age = None if self.last_observed is None else day - self.last_observed
        fresh = age is not None and age <= self.config.max_age_days
        estimates = self.x.reshape(3, self.m) @ self.edges.T
        sigmas = np.array([np.sqrt(np.maximum(0, np.einsum("ij,jk,ik->i", self.edges,
                           self.p[k*self.m:(k+1)*self.m, k*self.m:(k+1)*self.m], self.edges)))
                           for k in range(3)])
        pairs = []
        for k, (i, j) in enumerate(self.pairs):
            spread, velocity, acceleration = estimates[:, k]
            sd, sv, sa = sigmas[:, k]
            direction = np.sign(spread)
            closing = -direction * velocity
            closing_acceleration = -direction * acceleration
            zvel = closing / max(sv, self.config.speed_floor)
            zacc = closing_acceleration / max(sa, self.config.speed_floor)
            resolved = abs(spread) > max(self.config.spread_band, 2 * sd)
            score = float(np.clip((zvel - 1) / 2, 0, 1)) if fresh and resolved else 0.
            if not fresh:
                regime = "stale"
            elif not resolved:
                regime = "near-reference/uncertain"
            elif zvel > 2:
                regime = "closing"
            elif zvel < -2:
                regime = "slowing-departure" if zacc > 2 else "departing"
            else:
                regime = "unresolved-turn"
            pairs.append(dict(i=i, j=j, spread=float(spread), velocity=float(velocity),
                acceleration=float(acceleration), sigma=sigmas[:, k].tolist(),
                closure=float(closing), closure_acceleration=float(closing_acceleration),
                inward_score=score, regime=regime,
                cessation=motion_desire(float(direction * velocity), float(closing_acceleration), float(sv), float(sa))))
        return dict(day=day, fresh=fresh, age_days=age, venues=list(routes), routes=routes,
                    pairs=pairs, innovation_nis=nis, disagreement_nis=disagreement,
                    noise_sd=np.sqrt(np.diag(self.r)).tolist())


def allocation_candidates(snapshot, weights, targets, band=.002, elapsed_days=.25, config=None, pair_configs=None):
    """Diagnostic matched allocations, bounded across ALL pairs per leg.

    These are candidate NAV fractions, not fills or expected returns. Actual
    weight gaps choose direction, independently of historical price anchors.
    """
    config = config or ResponseConfig()
    weights, targets = np.asarray(weights, dtype=float), np.asarray(targets, dtype=float)
    if (weights.shape != targets.shape or weights.ndim != 1 or len(weights) < 2
            or not np.all(np.isfinite(weights)) or not np.all(np.isfinite(targets))
            or np.any(weights < 0) or np.any(targets < 0)
            or not np.isclose(weights.sum(), 1) or not np.isclose(targets.sum(), 1)
            or not math.isfinite(band) or band < 0 or not math.isfinite(elapsed_days) or elapsed_days <= 0):
        raise ValueError("normalized allocation vectors and nonnegative band required")
    over = np.maximum(weights - targets - band, 0)
    under = np.maximum(targets - weights - band, 0)
    proposals, eligible = [], {}
    if not snapshot["fresh"]:
        return proposals
    for pair in snapshot["pairs"]:
        i, j = pair["i"], pair["j"]
        pair_config = (pair_configs or {}).get((i, j), config)
        if over[j] > 0 and under[i] > 0:
            i, j = j, i
            outward, acceleration = -pair["velocity"], pair["acceleration"]
        else:
            outward, acceleration = pair["velocity"], -pair["acceleration"]
        desire = motion_desire(outward, acceleration, pair["sigma"][1], pair["sigma"][2], pair_config)
        if over[i] > 0 and under[j] > 0 and desire["eligible"]:
            eligible.setdefault(i, []).append((j, desire))
    totals_buy = np.zeros(len(weights))
    for i, destinations in eligible.items():
        deficit = sum(under[j] for j, _ in destinations)
        rate = sum(under[j] * d["rate"] for j, d in destinations) / deficit
        sale = min(over[i], deficit) * -math.expm1(-rate * elapsed_days)
        for j, desire in destinations:
            amount = float(sale * under[j] / deficit)
            proposals.append(dict(sell=i, buy=j, nav_fraction=amount, **desire))
            totals_buy[j] += amount
    # One common safety scale preserves each seller's proportional allocation
    # among eligible recipients even when multiple sellers compete for one.
    # It may leave spare capacity: this is a conservative diagnostic allocator.
    scale = min([1.] + [under[j] / amount for j, amount in enumerate(totals_buy) if amount > 0])
    for proposal in proposals:
        proposal["nav_fraction"] *= scale
    return proposals
