"""BuckBasket slow-rebalance policy model: deviation x MA-acceleration factor.

Premise (doc/BASKET-REDESIGN.md; alberta-buck-ethereum-basket.org): the BUCK basket
commodities trail the M2 money supply with per-commodity Cantillon lags --
monetary assets (cbBTC, PAXG) reprice in months, energy and construction in
about a year, labour and food in one to two years.  A constituent's *share* of
basket value therefore takes long, slow excursions away from its target weight
(fast responders overshoot while the laggards catch up), levels off, and
reverts.  A rebalancer that reacts to instantaneous deviation fights the
excursion all the way out; one that waits for the excursion to *level off*
commits its flow at maximum mispricing.

The policy modeled here scales per-constituent rebalancing effort by

    factor_i = |MA_i(deviation)| x accel-of-MA-toward-target, clipped at 0

-- HOW FAR the delayed (X_i-day moving average) share is from target, TIMES
how hard that average is accelerating BACK toward it.  While an excursion is
still accelerating away the factor is zero (quenched); as it decelerates and
levels off the factor turns on at maximum deviation, pouring assets into (or
out of) the constituent where it is most under-/over-priced.

The per-constituent window X_i derives from each commodity's measured
YoY-growth lag against M2SL (quotes/fetch_feedstock.py lag_scan; frozen in
LAG_TABLE below, re-fit with --fit-lags): the share-excursion timescale is the
commodity's lag *relative to the basket median* (fast movers lead the pack by
the median lag; laggards trail it), floored by the idiosyncratic
mean-reversion time, and the MA window is a third of that excursion.

Every signal is incremental and O(1) per constituent per step -- ring-buffer
SMA, strided finite differences, EMA normalizer -- so the policy ports
directly to a Solidity state machine of independently invocable low-gas steps
(sample / evaluate / one bounded trade).

A second gated variant, `vrate`, replaces the acceleration gate with the MA
velocity's *regime* -- receding (deviation still growing: don't trade), level
(turning: start, but not too fast), gaining (closing: complete the remaining
rebalancing) -- and sizes effort by *rate-matching*: trade at rho times the
natural closure rate observed by a 1-week EMA of d(deviation)/dt ("the rate
at which we'd lose half the distance to balance"), with a
half-the-gap-in-one-window prior while level.  Self-calibrating (no kappa, no
acceleration normalizer) and self-limiting (its own flow feeds the observed
closure rate, so raising rho saturates rather than overshoots).

A third gated variant, `pairs`, works in *differential mode*: instead of each
constituent's share vs its own target (which, measured on-chain from pool
reserves, carries BUCK-side flow noise), it watches the full graph of
CROSS-COMMODITY log price ratios -- the common numeraire cancels exactly --
through a per-leg ladder of K EMAs at geometric timescales (5..320d).  EMA
linearity means every pair's moving average at every scale is just the
difference of two legs' ladders (O(N*K) state, not O(N^2*K)).  A pair trades
when a QUORUM of scales votes that its divergence is decelerating back toward
equilibrium (the factor gate, per scale, voted): short windows catch the
mid-size swings a single long MA concedes to prop, long windows catch the
macro M2 excursions, and no single noisy scale can fire the gate alone.
Effort = kappa x |pairwise imbalance| x votes/K, executed as a matched pair
trade (sell rich leg, buy poor leg) -- self-financing, no cash residue, no
wash risk by construction.

Policies compared: hold (never rebalance), prop (continuous proportional to
instantaneous deviation -- what spot-based flow routing approximates), band
(threshold 5%, the BuckBasketRebalancerAgent approach), factor, vrate, and
pairs (this model).

Run:

    python -m alberta_buck.sim.rebalance_policy                # both modes
    python -m alberta_buck.sim.rebalance_policy --sweep        # + window sweep
    python -m alberta_buck.sim.rebalance_policy --fit-lags     # re-fit LAG_TABLE
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from collections import deque
from pathlib import Path
from typing import Callable, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CSV_DIR = HERE / "prices"
DEFAULT_OUT = REPO / "test" / "vectors" / "rebalance-policy.json"

DAYS_PER_MONTH = 30.44

# (symbol, hist csv, tau months, peak corr, note) -- YoY-growth Pearson lag vs
# M2SL, post-1995 window, scan lags [0, 36] months (see --fit-lags).  PAXG's
# fit is weak and censored at the scan edge (gold trades on real rates and
# crisis as much as on M2); it gets a fast "monetary asset" prior instead.
LAG_TABLE = [
    ("PAXG",  "hist-paxg.csv",   3.0, 0.10, "monetary prior; M2 fit weak/censored"),
    ("cbBTC", "hist-cbbtc.csv",  0.0, 0.08, "peak at lag 0; 178-month sample"),
    ("NRGC",  "hist-nrgc.csv",  13.0, 0.39, "US retail energy"),
    ("CNST",  "hist-cnst.csv",  10.0, 0.52, "US construction PPI; strongest fit"),
    ("FOOD",  "hist-food.csv",  23.0, 0.26, "US retail food"),
    ("LABR",  "hist-labr.csv",  15.0, 0.38, "US wage (AHETPI)"),
]
SYMS = [r[0] for r in LAG_TABLE]
HIST_CSV = {r[0]: r[1] for r in LAG_TABLE}
TAUS = {r[0]: r[2] for r in LAG_TABLE}

# The BuckBasket's charter admits only civilizational basics -- things physics
# forces to revert -- and warns in so many words against constituents built
# NOT to revert.  The default six include both cbBTC (x9 over 2020-25) and
# PAXG, so the historical replay is dominated by exactly the assets the
# charter would exclude, and any policy comparison run on it is really a
# comparison of trend behaviour.  BASICS is the charter-compliant subset:
# energy, construction, food, labour.
BASICS = ["NRGC", "CNST", "FOOD", "LABR"]


def set_syms(subset: Sequence[str]) -> None:
    """Restrict the run to `subset` (order preserved from LAG_TABLE).

    SYMS is module-global and read by the policies and the synthetic path
    generator at construction time, so this must be called ONCE before any
    policy or price series is built -- which is what `run()` does.
    """
    global SYMS
    unknown = [x for x in subset if x not in HIST_CSV]
    if unknown:
        raise SystemExit(f"unknown symbols: {unknown}")
    if len(subset) < 2:
        raise SystemExit("need at least two constituents")
    SYMS = [r[0] for r in LAG_TABLE if r[0] in set(subset)]

# Synthetic-mode daily idiosyncratic vol (OU component) and M2 pass-through
# elasticity per symbol.  Betas ~1 make deviations *transient* -- the premise
# under test is differential LAG, not differential long-run drift; cbBTC gets
# a mild overshoot so the leash still sees some secular pressure.  (Historical
# mode supplies the real thing: BTC x9 over 2020-25.)
SYNTH_SIGMA = {"PAXG": .010, "cbBTC": .035, "NRGC": .018,
               "CNST": .006, "FOOD": .004, "LABR": .0015}
SYNTH_BETA = {"PAXG": 1.0, "cbBTC": 1.1, "NRGC": 1.0,
              "CNST": 1.0, "FOOD": 1.0, "LABR": 1.0}
SYNTH_BASE = {"PAXG": 2600.0, "cbBTC": 65000.0, "NRGC": 320.0,
              "CNST": 780.0, "FOOD": 250.0, "LABR": 25.0}

WINDOW_MIN, WINDOW_MAX = 30, 180
EXCURSION_FRACTION = 3.0        # MA window = excursion duration / 3
IDIO_FLOOR_MONTHS = 4.0         # excursion floor from idiosyncratic reversion

SWEEP_WINDOWS = [30, 45, 60, 90, 120, 180]


def derive_windows(taus: dict[str, float]) -> dict[str, int]:
    """Ideal X_i-day MA window from the M2 lag table.

    A constituent's share excursion runs while the rest of the basket catches
    up (or while it catches up to the rest), so its timescale is the lag
    *relative to the basket median*, floored by the idiosyncratic reversion
    time; the MA must resolve the excursion's turn, so use a third of it.
    """
    med = statistics.median(taus.values())
    out: dict[str, int] = {}
    for sym, tau in taus.items():
        dur_m = max(abs(tau - med), IDIO_FLOOR_MONTHS)
        days = dur_m * DAYS_PER_MONTH / EXCURSION_FRACTION
        out[sym] = int(min(max(days, WINDOW_MIN), WINDOW_MAX))
    return out


# --------------------------------------------------------------- price paths

def load_hist_prices(syms: Sequence[str] = SYMS) -> list[list[float]]:
    """Daily close vectors from alberta_buck/sim/prices/hist-*.csv."""
    cols: list[list[float]] = []
    for sym in syms:
        path = CSV_DIR / HIST_CSV[sym]
        with path.open() as f:
            rows = list(csv.DictReader(f))
        cols.append([int(r["close_usd_micro"]) / 1e6 for r in rows])
    days = min(len(c) for c in cols)
    return [[c[t] for c in cols] for t in range(days)]


def synth_prices(seed: int, years: float,
                 taus: dict[str, float] = TAUS) -> list[list[float]]:
    """Synthetic daily paths: commodities as lagged responses to an M2 driver.

    log M2 grows at a base rate with occasional surge regimes (the 2020-style
    expansion); each commodity's log price relaxes toward beta_i * log M2 with
    time constant tau_i (its Cantillon lag) plus an OU idiosyncratic term.
    Ground-truth taus let the window sweep validate the derived X_i.
    """
    rng = random.Random(seed)
    days = int(years * 365)
    mu_base, mu_surge = 0.06 / 365, 0.20 / 365
    p_enter = 1.0 / (365 * 4)           # a surge roughly every four years
    theta = 180.0                       # idiosyncratic OU reversion (days)
    taud = {s: max(t * DAYS_PER_MONTH, 20.0) for s, t in taus.items()}

    logm2 = 0.0
    surge = 0
    p = {s: 0.0 for s in SYMS}
    x = {s: 0.0 for s in SYMS}
    out: list[list[float]] = []
    for _ in range(days):
        if surge > 0:
            surge -= 1
            mu = mu_surge
        elif rng.random() < p_enter:
            surge = rng.randint(180, 540)
            mu = mu_surge
        else:
            mu = mu_base
        logm2 += mu
        row = []
        for s in SYMS:
            p[s] += (SYNTH_BETA[s] * logm2 - p[s]) / taud[s]
            x[s] += -x[s] / theta + SYNTH_SIGMA[s] * rng.gauss(0.0, 1.0)
            row.append(SYNTH_BASE[s] * math.exp(p[s] + x[s]))
        out.append(row)
    return out


# ------------------------------------------------------------------ policies

class _Sma:
    """O(1) ring-buffer simple moving average (ports to a storage ring)."""

    def __init__(self, window: int):
        self.window = window
        self.buf = [0.0] * window
        self.i = 0
        self.n = 0
        self.sum = 0.0

    def push(self, v: float) -> float:
        if self.n < self.window:
            self.n += 1
        else:
            self.sum -= self.buf[self.i]
        self.buf[self.i] = v
        self.sum += v
        self.i = (self.i + 1) % self.window
        return self.sum / self.n

    @property
    def full(self) -> bool:
        return self.n == self.window


class _FactorState:
    """Per-constituent incremental signal state.

    m = X-day SMA of the share deviation; v, a = strided first/second
    differences of m (stride X/8 suppresses day-scale noise); gate = the
    positive part of the acceleration *toward* target, normalized by a running
    RMS of |a| so the gate saturates at ~1 for a typical strong turn.  All
    O(1) per step: one ring buffer, one small deque, one EMA.
    """

    WEEK_BETA = 2.0 / (7 + 1)       # 1-week EMA of the deviation's daily change

    def __init__(self, window: int):
        self.window = window
        self.sma = _Sma(window)
        self.stride = max(1, window // 8)
        self.hist: deque[float] = deque(maxlen=2 * self.stride + 1)
        self.beta = 2.0 / (window + 1)
        self.a2 = 0.0               # EMA of a^2 (gate normalizer)
        self.m = 0.0
        self.v = 0.0
        self.a = 0.0
        self.gate = 0.0
        self.prev_delta: float | None = None
        self.ddot = 0.0             # 1-week EMA of d(delta)/dt

    def update(self, delta: float) -> float:
        if self.prev_delta is not None:
            self.ddot += (delta - self.prev_delta - self.ddot) * self.WEEK_BETA
        self.prev_delta = delta
        self.m = self.sma.push(delta)
        self.hist.append(self.m)
        if not self.sma.full or len(self.hist) < (self.hist.maxlen or 0):
            self.gate = 0.0
            return 0.0
        s = self.stride
        m0, m1, m2 = self.hist[-1], self.hist[-1 - s], self.hist[0]
        self.v = (m0 - m1) / s
        self.a = (m0 - 2.0 * m1 + m2) / (s * s)
        if self.a2 == 0.0:
            self.a2 = self.a * self.a
        self.a2 = self.a2 * (1.0 - self.beta) + self.a * self.a * self.beta
        toward = -self.a if self.m > 0.0 else self.a
        scale = math.sqrt(self.a2)
        self.gate = min(1.0, max(0.0, toward) / max(scale, 1e-15))
        return self.gate

    @property
    def ready(self) -> bool:
        return self.sma.full and len(self.hist) >= (self.hist.maxlen or 0)


class FactorPolicy:
    """deviation x accel-toward-target, quenched while diverging.

    A deviation *leash* bounds the quench: a secular trend that never levels
    off (BTC 2020-25) would otherwise run the deviation away unboundedly while
    the gate stays shut.  Beyond |delta| > leash the policy trades at cap
    toward target regardless of the gate (hysteresis back to leash_inner) --
    the factor times trades *within* the leash; the leash enforces the mandate.
    """

    name = "factor"

    def __init__(self, windows: dict[str, int], kappa: float = 0.08,
                 cap: float = 0.005, deadband: float = 0.015,
                 leash: float = 0.30, leash_inner: float = 0.25):
        self.states = [_FactorState(windows[s]) for s in SYMS]
        self.kappa = kappa
        self.cap = cap
        self.deadband = deadband
        self.leash = leash
        self.leash_inner = leash_inner
        self.leashed = [False] * len(SYMS)

    def efforts(self, deltas: Sequence[float],
                prices: Sequence[float] | None = None) -> list[float]:
        out = []
        for i, (st, d) in enumerate(zip(self.states, deltas)):
            gate = st.update(d)
            if self.leashed[i]:
                self.leashed[i] = abs(d) > self.leash_inner
            else:
                self.leashed[i] = abs(d) > self.leash
            if self.leashed[i]:
                out.append(-math.copysign(self.cap, d))
                continue
            # Magnitude and direction come from the RAW deviation (how far the
            # constituent is from proportion right now); the MA contributes
            # only the timing gate.  Both must agree in sign -- once the raw
            # share has reverted through target the stale MA must not trade.
            if (gate <= 0.0 or abs(d) < self.deadband
                    or st.m * d <= 0.0):
                out.append(0.0)
                continue
            # gate^2 shapes effort toward decisive turns (a weak flicker of
            # toward-target acceleration barely trades); cap bounds the pour.
            e = min(self.kappa * abs(d) * gate * gate, self.cap)
            out.append(-math.copysign(e, d))
        return out


PAIR_LADDER = [5, 10, 20, 40, 80, 160, 320]     # geometric window ladder
_SQRT2 = math.sqrt(2.0)


class _EmaLadder:
    """Per-leg K-window EMA ladder over log price, with strided curvature.

    EMAs are linear, so any PAIR's moving average / velocity / curvature at
    any scale is the difference of two legs' ladders -- the full cross-
    commodity differential graph from O(N*K) state.
    """

    def __init__(self, windows: Sequence[int] = PAIR_LADDER):
        self.windows = list(windows)
        self.strides = [max(1, w // 8) for w in self.windows]
        self.emas: list[float | None] = [None] * len(self.windows)
        self.hist = [deque(maxlen=2 * s + 1) for s in self.strides]
        self.n = 0

    def update(self, logp: float) -> None:
        self.n += 1
        for k, w in enumerate(self.windows):
            beta = 2.0 / (w + 1)
            e = self.emas[k]
            e = logp if e is None else e + (logp - e) * beta
            self.emas[k] = e
            self.hist[k].append(e)

    def ready(self, k: int) -> bool:
        return (self.n >= self.windows[k]
                and len(self.hist[k]) == (self.hist[k].maxlen or 0))

    def ma(self, k: int) -> float:
        return self.hist[k][-1]

    def accel(self, k: int) -> float:
        s = self.strides[k]
        h = self.hist[k]
        return (h[-1] - 2.0 * h[-1 - s] + h[0]) / (s * s)

    def vel(self, k: int) -> float:
        s = self.strides[k]
        h = self.hist[k]
        return (h[-1] - h[-1 - s]) / s


class PairsPolicy:
    """Differential-mode multi-scale turn harvester.

    Signals live on cross-commodity log price ratios (numeraire cancels);
    each pair (i,j) is voted on by K timescales: a window votes when the
    pair's MA-gap deviation agrees in sign with the current pairwise
    imbalance AND its curvature points back toward equilibrium (the factor
    gate, per scale).  votes >= quorum opens the pair; effort is
    kappa x |imbalance| x votes/K, executed as a matched pair trade.
    A pairwise leash (with hysteresis) enforces the mandate through trends.
    """

    name = "pairs"

    def __init__(self, windows_ladder: Sequence[int] = PAIR_LADDER,
                 quorum: int = 4, kappa: float = 0.5, cap: float = 0.005,
                 deadband: float = 0.015, leash: float = 0.30,
                 leash_inner: float = 0.25, vote: str = "vel",
                 boundary: bool = False):
        self.legs = [_EmaLadder(windows_ladder) for _ in SYMS]
        self.K = len(windows_ladder)
        self.quorum = quorum
        self.vote = vote          # "curv": divergence decelerating (early);
                                  # "vel": gap already closing (confirmed)
        # Under proportional costs the optimal policy is a NO-TRADE REGION
        # and one trades only far enough to reach its boundary, never to the
        # target (Davis-Norman 1990; Shreve-Soner 1994).  With boundary=True
        # the deadband becomes that region and effort is sized on the excess
        # |d| - deadband; with False it is sized on |d| itself, i.e. toward
        # the target, which is what every policy in this module did.
        self.boundary = boundary
        self.kappa = kappa
        self.cap = cap
        self.deadband = deadband
        self.leash = leash
        self.leash_inner = leash_inner
        self.p0: list[float] | None = None
        self.leashed: dict[tuple[int, int], bool] = {}

    def _pair_effort(self, i: int, j: int, d: float) -> float:
        """Unsigned effort for pair (i,j) with pairwise imbalance d."""
        key = (i, j)
        was = self.leashed.get(key, False)
        self.leashed[key] = abs(d) > (self.leash_inner if was else self.leash)
        if self.leashed[key]:
            return self.cap
        if abs(d) < self.deadband:
            return 0.0
        li, lj = self.legs[i], self.legs[j]
        ref = self.p0[i] - self.p0[j]
        votes = 0
        for k in range(self.K):
            if not (li.ready(k) and lj.ready(k)):
                continue
            g = (li.ma(k) - lj.ma(k)) - ref
            if g * d <= 0.0:                      # scale disagrees with raw
                continue
            if self.vote == "vel":                # gap already closing
                x = li.vel(k) - lj.vel(k)
            else:                                 # divergence decelerating
                x = li.accel(k) - lj.accel(k)
            if x * math.copysign(1.0, d) < 0.0:
                votes += 1
        if votes < self.quorum:
            return 0.0
        mag = max(0.0, abs(d) - self.deadband) if self.boundary else abs(d)
        return min(self.kappa * mag * votes / self.K, self.cap)

    def efforts(self, deltas: Sequence[float],
                prices: Sequence[float] | None = None) -> list[float]:
        n = len(SYMS)
        logp = [math.log(p) for p in prices]
        if self.p0 is None:
            self.p0 = logp[:]
        for leg, lp in zip(self.legs, logp):
            leg.update(lp)

        # Pair efforts, then net per token; rescale any leg whose net
        # exceeds the cap (keeping every pair trade matched).
        pair_e: dict[tuple[int, int], float] = {}
        net = [0.0] * n
        for i in range(n):
            for j in range(i + 1, n):
                d = math.log((1.0 + deltas[i]) / (1.0 + deltas[j]))
                e = self._pair_effort(i, j, d)
                if e <= 0.0:
                    continue
                e = math.copysign(e, d)           # >0: i rich, sell i buy j
                pair_e[(i, j)] = e
                net[i] -= e
                net[j] += e
        scale = [1.0 if abs(v) <= self.cap else self.cap / abs(v) for v in net]
        net = [0.0] * n
        for (i, j), e in pair_e.items():
            s = min(scale[i], scale[j])
            net[i] -= e * s
            net[j] += e * s
        return net


def _jacobi_eig(a: list[list[float]], sweeps: int = 24
                ) -> tuple[list[float], list[list[float]]]:
    """Cyclic Jacobi eigendecomposition of a small symmetric matrix.

    Returns (eigenvalues, eigenvectors-as-rows), unsorted.  N is 4-6 here, so
    an O(N^3) pure-Python routine costs nothing and avoids a numpy dependency
    in a module the Solidity port has to mirror.
    """
    n = len(a)
    m = [row[:] for row in a]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(sweeps):
        off = sum(m[i][j] ** 2 for i in range(n) for j in range(n) if i != j)
        if off < 1e-24:
            break
        for pp in range(n - 1):
            for qq in range(pp + 1, n):
                if abs(m[pp][qq]) < 1e-18:
                    continue
                theta = (m[qq][qq] - m[pp][pp]) / (2.0 * m[pp][qq])
                t = math.copysign(1.0, theta) / (
                    abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                sn = t * c
                for k in range(n):
                    mkp, mkq = m[k][pp], m[k][qq]
                    m[k][pp] = c * mkp - sn * mkq
                    m[k][qq] = sn * mkp + c * mkq
                for k in range(n):
                    mpk, mqk = m[pp][k], m[qq][k]
                    m[pp][k] = c * mpk - sn * mqk
                    m[qq][k] = sn * mpk + c * mqk
                for k in range(n):
                    vkp, vkq = v[k][pp], v[k][qq]
                    v[k][pp] = c * vkp - sn * vkq
                    v[k][qq] = sn * vkp + c * vkq
    vals = [m[i][i] for i in range(n)]
    vecs = [[v[r][i] for r in range(n)] for i in range(n)]   # rows = vectors
    return vals, vecs


class ModesPolicy:
    """Differential mode in an ORTHOGONAL basis -- the polyphase transform.

    The `pairs` engine's weakness is its basis, not its gating.  N legs carry
    only N-1 independent differential modes, but pairs tracks N(N-1)/2 of
    them: at N=6 that is fifteen signals over five degrees of freedom.  The
    quorum therefore counts heavily overlapping evidence as if it were
    independent, and one leg's excursion appears in every pair containing it
    -- which is the mechanism behind the reported trend-regime failure, where
    "the quorum fires on BTC's consolidations" is one excursion voting five
    times.

    This is what a polyphase drive does about it, in two steps:

      Clarke (abc -> alpha-beta-zero): subtract the cross-sectional mean of
        log prices.  That removes the zero sequence -- BUCK's own valuation,
        which is the K-controller's business -- exactly, just as the pairwise
        log ratio does, but once for the whole vector instead of per pair.

      Park (alpha-beta -> d-q): rotate into the frame the data actually
        turns in.  Diagonalizing the EWMA covariance of the centred returns
        gives N-1 orthogonal modes; each is sized independently because they
        share no variance, so a quorum over modes counts independent
        evidence.  This is the eigenportfolio construction of Avellaneda and
        Lee (2010), with the market factor removed by construction rather
        than by discarding PC1.

    EMA linearity carries over intact: a mode's moving average, velocity and
    curvature at any scale is the projection of the per-leg ladders onto that
    mode, so the filter bank is unchanged and no extra state is needed.

    Because every mode is orthogonal to the all-ones vector, the per-leg
    efforts sum to zero identically -- the trades are self-financing and
    wash-proof for the same structural reason matched pairs are, without
    needing to be paired up explicitly.
    """

    name = "modes"

    def __init__(self, windows_ladder: Sequence[int] = PAIR_LADDER,
                 quorum: int = 4, kappa: float = 0.5, cap: float = 0.005,
                 deadband: float = 0.015, leash: float = 0.30,
                 leash_inner: float = 0.25, vote: str = "vel",
                 boundary: bool = False, cov_halflife: float = 250.0,
                 refit_days: int = 60, warmup_days: int = 120):
        self.n = len(SYMS)
        self.legs = [_EmaLadder(windows_ladder) for _ in SYMS]
        self.K = len(windows_ladder)
        self.quorum = quorum
        self.kappa = kappa
        self.cap = cap
        self.deadband = deadband
        self.leash = leash
        self.leash_inner = leash_inner
        self.vote = vote
        self.boundary = boundary
        self.refit_days = refit_days
        self.warmup_days = warmup_days
        self.beta_cov = 1.0 - 0.5 ** (1.0 / cov_halflife)
        self.cov = [[0.0] * self.n for _ in range(self.n)]
        self.prev_c: list[float] | None = None
        self.c0: list[float] | None = None
        self.basis: list[list[float]] = []      # rows: unit mode vectors
        self.refs: list[float] = []             # day-0 projection per mode
        self.leashed: dict[int, bool] = {}
        self.t = 0

    # -- basis ---------------------------------------------------------- #

    def _centre(self, logp: Sequence[float]) -> list[float]:
        mu = sum(logp) / self.n
        return [x - mu for x in logp]

    def _refit(self) -> None:
        """Diagonalize the centred-return covariance; keep the real modes.

        Centring puts the all-ones direction in the null space, so exactly
        one eigenvalue is ~0 and dropping it leaves the N-1 differential
        modes.  Sorted by variance so mode 0 is the dominant contrast (e.g.
        monetary-versus-sticky), which is a tradeable excursion, NOT the
        common mode -- that is already gone.
        """
        vals, vecs = _jacobi_eig(self.cov)
        order = sorted(range(self.n), key=lambda i: -vals[i])
        keep = [i for i in order if vals[i] > 1e-14][:self.n - 1]
        basis = []
        for i in keep:
            v = vecs[i]
            mu = sum(v) / self.n                 # re-orthogonalize vs ones
            v = [x - mu for x in v]
            nrm = math.sqrt(sum(x * x for x in v))
            if nrm > 1e-9:
                basis.append([x / nrm for x in v])
        if basis:
            self.basis = basis
            self.refs = [sum(b[i] * self.c0[i] for i in range(self.n))
                         for b in self.basis]

    # -- signal --------------------------------------------------------- #

    def _mode_effort(self, mi: int, b: Sequence[float], d: float) -> float:
        was = self.leashed.get(mi, False)
        self.leashed[mi] = abs(d) > (self.leash_inner if was else self.leash)
        if self.leashed[mi]:
            return self.cap
        if abs(d) < self.deadband:
            return 0.0
        votes = 0
        for k in range(self.K):
            if not all(leg.ready(k) for leg in self.legs):
                continue
            g = sum(b[i] * self.legs[i].ma(k)
                    for i in range(self.n)) - self.refs[mi]
            if g * d <= 0.0:
                continue
            if self.vote == "vel":
                x = sum(b[i] * self.legs[i].vel(k) for i in range(self.n))
            else:
                x = sum(b[i] * self.legs[i].accel(k) for i in range(self.n))
            if x * math.copysign(1.0, d) < 0.0:
                votes += 1
        if votes < self.quorum:
            return 0.0
        mag = max(0.0, abs(d) - self.deadband) if self.boundary else abs(d)
        return min(self.kappa * mag * votes / self.K, self.cap)

    def efforts(self, deltas: Sequence[float],
                prices: Sequence[float] | None = None) -> list[float]:
        n = self.n
        c = self._centre([math.log(p) for p in prices])
        if self.c0 is None:
            self.c0 = c[:]
        for leg, x in zip(self.legs, c):
            leg.update(x)
        if self.prev_c is not None:                # EWMA covariance of returns
            r = [c[i] - self.prev_c[i] for i in range(n)]
            b = self.beta_cov
            for i in range(n):
                for j in range(n):
                    self.cov[i][j] += (r[i] * r[j] - self.cov[i][j]) * b
        self.prev_c = c
        self.t += 1
        if self.t >= self.warmup_days and (
                not self.basis or self.t % self.refit_days == 0):
            self._refit()
        if not self.basis:
            return [0.0] * n

        # Deviation vector in the same centred coordinates as the basis.
        u = [math.log(1.0 + deltas[i]) for i in range(n)]
        mu = sum(u) / n
        u = [x - mu for x in u]

        # Scale the projection into PAIR-EQUIVALENT units before applying any
        # of the shared knobs.  A pair (i,j) is the unit contrast
        # b = (e_i - e_j)/sqrt(2), and PairsPolicy measures d_ij = u_i - u_j,
        # which is sqrt(2) times b.u.  Comparing the two policies at one
        # deadband/leash/kappa without this factor runs `modes` at a 1.41x
        # tighter deadband -- which showed up exactly as expected, in `modes`
        # making 5120 small trades against `pairs` 2966.
        net = [0.0] * n
        for mi, b in enumerate(self.basis):
            d = _SQRT2 * sum(b[i] * u[i] for i in range(n))
            e = self._mode_effort(mi, b, d)
            if e <= 0.0:
                continue
            e = math.copysign(e, d)          # d>0: over-exposed along +b
            for i in range(n):
                net[i] -= e * b[i]
        peak = max(abs(v) for v in net)
        if peak > self.cap:                  # one scalar: zero-sum preserved
            net = [v * self.cap / peak for v in net]
        return net


class VratePolicy:
    """Velocity-regime, rate-matched rebalancing.

    The MA's *velocity* classifies the excursion: still receding (deviation
    growing -- don't trade), level (turning -- start, but not too fast), or
    gaining (closing -- complete the remaining rebalancing).  Effort is
    matched to the *observed* closure rate: a 1-week EMA of d(delta)/dt gives
    the speed the gap is closing on its own; trade at rho times that rate
    (converted to NAV flow via the target weight).  In the level regime,
    where no closure is observed yet, the prior is "lose half the distance in
    one MA window" (c0 = ln2 * |delta| / X) started at half rate.  Effort
    therefore peaks just after the turn -- closure speeding up while the
    deviation is still near maximum -- and tapers as the gap closes.

    Self-calibrating: no kappa, no acceleration normalizer.  Same raw-vs-MA
    sign agreement and deviation leash as FactorPolicy.
    """

    name = "vrate"

    def __init__(self, windows: dict[str, int], weights: Sequence[float],
                 rho: float = 3.0, cap: float = 0.005,
                 deadband: float = 0.015, eps_frac: float = 0.25,
                 leash: float = 0.30, leash_inner: float = 0.25):
        self.states = [_FactorState(windows[s]) for s in SYMS]
        self.weights = list(weights)
        self.rho = rho
        self.cap = cap
        self.deadband = deadband
        self.eps_frac = eps_frac
        self.leash = leash
        self.leash_inner = leash_inner
        self.leashed = [False] * len(SYMS)

    def efforts(self, deltas: Sequence[float],
                prices: Sequence[float] | None = None) -> list[float]:
        out = []
        for i, (st, d) in enumerate(zip(self.states, deltas)):
            st.update(d)
            if self.leashed[i]:
                self.leashed[i] = abs(d) > self.leash_inner
            else:
                self.leashed[i] = abs(d) > self.leash
            if self.leashed[i]:
                st.gate = 1.0
                out.append(-math.copysign(self.cap, d))
                continue
            st.gate = 0.0
            if (not st.ready or abs(d) < self.deadband
                    or st.m * d <= 0.0):
                out.append(0.0)
                continue
            # Regime from the MA's velocity: u > 0 means the delayed view is
            # moving toward target.  eps scales with the speed that would
            # close the MA gap in one window -- no absolute threshold.
            u = -st.v if st.m > 0.0 else st.v
            eps = self.eps_frac * abs(st.m) / st.window
            if u < -eps:                        # still receding: quench
                out.append(0.0)
                continue
            c0 = math.log(2.0) * abs(d) / st.window     # half-gap-in-X prior
            if u <= eps:                        # level: start, not too fast
                c = 0.5 * c0
                st.gate = 0.5
            else:                               # gaining: match observed rate
                observed = -st.ddot if d > 0.0 else st.ddot
                c = max(observed, 0.5 * c0)
                st.gate = 1.0
            e = min(self.rho * c * self.weights[i], self.cap)
            out.append(-math.copysign(e, d))
        return out


class PropPolicy:
    """Continuous proportional to instantaneous deviation (naive baseline)."""

    name = "prop"

    def __init__(self, kappa: float = 0.02, cap: float = 0.005,
                 deadband: float = 0.015):
        self.kappa = kappa
        self.cap = cap
        self.deadband = deadband

    def efforts(self, deltas: Sequence[float],
                prices: Sequence[float] | None = None) -> list[float]:
        return [0.0 if abs(d) < self.deadband
                else -math.copysign(min(self.kappa * abs(d), self.cap), d)
                for d in deltas]


class BandPolicy:
    """Engage at |deviation| > outer, trade at cap until back inside inner."""

    name = "band"

    def __init__(self, outer: float = 0.05, inner: float = 0.01,
                 cap: float = 0.005):
        self.outer = outer
        self.inner = inner
        self.cap = cap
        self.engaged = [False] * len(SYMS)

    def efforts(self, deltas: Sequence[float],
                prices: Sequence[float] | None = None) -> list[float]:
        out = []
        for i, d in enumerate(deltas):
            if self.engaged[i]:
                self.engaged[i] = abs(d) > self.inner
            else:
                self.engaged[i] = abs(d) > self.outer
            out.append(-math.copysign(self.cap, d) if self.engaged[i] else 0.0)
        return out


class HoldPolicy:
    name = "hold"

    def efforts(self, deltas: Sequence[float],
                prices: Sequence[float] | None = None) -> list[float]:
        return [0.0] * len(deltas)


# ----------------------------------------------------------------- simulator

def simulate(prices: list[list[float]], policy, weights: Sequence[float],
             cost_bp: float = 30.0, nav0: float = 1_000_000.0,
             cash_frac: float = 0.02, cash_cap_frac: float = 0.05,
             capture_days: int = 90, trace_sym: str | None = None) -> dict:
    """Run one policy over daily price vectors; return series + metrics.

    The basket holds quantities q_i plus a small BUCK cash buffer; efforts are
    signed fractions of NAV per day.  Sells execute first (bounded by
    holdings), buys are scaled to available cash; both legs pay cost_bp.

    Cash above cash_cap_frac of NAV is recycled into the most-underweight
    constituents (proportional to underweight, capped per day) regardless of
    policy -- the standing permissionless sweepTreasury / investFromBucks
    behavior of BuckBasketProRata, not a policy choice.  Trades below
    MIN_TRADE_FRAC of NAV are skipped (an on-chain step has a gas floor).
    """
    MIN_TRADE_FRAC = 5e-5
    SWEEP_CAP_FRAC = 0.005
    n = len(SYMS)
    days = len(prices)
    fee = cost_bp / 1e4
    cash = nav0 * cash_frac
    q = [nav0 * (1.0 - cash_frac) * weights[i] / prices[0][i] for i in range(n)]
    a0 = q[:]                       # fixed-quantity reference index

    def index_at(t: int) -> float:
        return sum(a0[i] * prices[t][i] for i in range(n))

    ti = SYMS.index(trace_sym) if trace_sym else -1
    navs, meanabs, cashes = [], [], []
    trace = {"delta": [], "ma": [], "gate": [], "trades": []}
    trades: list[tuple[int, int, float, float]] = []   # (t, i, val, delta)

    for t in range(days):
        P = prices[t]
        V = sum(q[i] * P[i] for i in range(n)) + cash
        deltas = [q[i] * P[i] / V / weights[i] - 1.0 for i in range(n)]
        meanabs.append(sum(abs(d) for d in deltas) / n)
        efforts = policy.efforts(deltas, P)

        if ti >= 0:
            st = policy.states[ti] if hasattr(policy, "states") else None
            trace["delta"].append(deltas[ti])
            trace["ma"].append(st.m if st else 0.0)
            trace["gate"].append(st.gate if st else 0.0)

        min_trade = MIN_TRADE_FRAC * V
        sold_today = set()
        for i in range(n):                              # sells first
            if efforts[i] < 0.0:
                val = min(-efforts[i] * V, q[i] * P[i])
                if val < min_trade:
                    continue
                q[i] -= val / P[i]
                cash += val * (1.0 - fee)
                sold_today.add(i)
                trades.append((t, i, -val, deltas[i]))
                if i == ti:
                    trace["trades"].append((t, -val, deltas[i]))
        want = [(i, efforts[i] * V) for i in range(n) if efforts[i] > 0.0]
        need = sum(v for _, v in want) * (1.0 + fee)
        scale = 1.0 if need <= cash else (cash / need if need > 0.0 else 0.0)
        for i, val in want:
            val *= scale
            if val < min_trade:
                continue
            q[i] += val / P[i]
            cash -= val * (1.0 + fee)
            trades.append((t, i, val, deltas[i]))
            if i == ti:
                trace["trades"].append((t, val, deltas[i]))

        # sweepTreasury: recycle excess cash into clearly-underweight
        # constituents -- never one the policy sold today (a stale-MA sell
        # paired with a raw-underweight rebuy would wash-trade fees away).
        excess = cash - cash_cap_frac * V
        if excess > min_trade:
            under = [(i, weights[i] - q[i] * P[i] / V) for i in range(n)
                     if i not in sold_today
                     and q[i] * P[i] / V / weights[i] - 1.0 < -0.02]
            under = [(i, u) for i, u in under if u > 0.0]
            usum = sum(u for _, u in under)
            for i, u in under:
                val = min(excess * u / usum, SWEEP_CAP_FRAC * V)
                if val < min_trade:
                    continue
                q[i] += val / P[i]
                cash -= val * (1.0 + fee)
                trades.append((t, i, val, deltas[i]))
                if i == ti:
                    trace["trades"].append((t, val, deltas[i]))

        navs.append(sum(q[i] * P[i] for i in range(n)) + cash)
        cashes.append(cash)

    years = days / 365.0
    gross = sum(abs(v) for _, _, v, _ in trades)
    mean_nav = sum(navs) / days
    cagr = (navs[-1] / nav0) ** (1.0 / years) - 1.0

    cap_num = cap_den = tim_num = 0.0
    for t, i, val, d in trades:
        tim_num += abs(val) * abs(d)
        t2 = t + capture_days
        if t2 < days:
            rel = (math.log(prices[t2][i] / prices[t][i])
                   - math.log(index_at(t2) / index_at(t)))
            cap_num += val * rel
            cap_den += abs(val)

    return {
        "series": {"nav": navs, "meanAbsDev": meanabs, "cash": cashes},
        "trace": trace if ti >= 0 else None,
        "metrics": {
            "finalNav": navs[-1],
            "cagrPct": cagr * 100.0,
            "teMeanAbsPct": 100.0 * sum(meanabs) / days,
            "turnoverPerYr": gross / mean_nav / years,
            "costPaid": gross * fee,
            "trades": len(trades),
            "timingMeanAbsDevPct": 100.0 * tim_num / gross if gross else 0.0,
            "capture90Bp": 1e4 * cap_num / cap_den if cap_den else 0.0,
        },
    }


def _policies(windows: dict[str, int], kappa: float, cap: float,
              deadband: float, weights: Sequence[float],
              rho: float = 3.0, quorum: int = 4,
              pairs_kappa: float = 0.5,
              pairs_vote: str = "vel") -> dict[str, Callable[[], object]]:
    return {
        "hold": lambda: HoldPolicy(),
        "prop": lambda: PropPolicy(cap=cap, deadband=deadband),
        "band": lambda: BandPolicy(cap=cap),
        "factor": lambda: FactorPolicy(windows, kappa=kappa, cap=cap,
                                       deadband=deadband),
        "vrate": lambda: VratePolicy(windows, weights, rho=rho, cap=cap,
                                     deadband=deadband),
        "pairs": lambda: PairsPolicy(quorum=quorum, kappa=pairs_kappa,
                                     cap=cap, deadband=deadband,
                                     vote=pairs_vote),
        # Proposal 2: same signal, sized to the no-trade boundary.
        "pairs-nt": lambda: PairsPolicy(quorum=quorum, kappa=pairs_kappa,
                                        cap=cap, deadband=deadband,
                                        vote=pairs_vote, boundary=True),
        # Proposal 1: orthogonal modes instead of redundant pairs...
        "modes": lambda: ModesPolicy(quorum=quorum, kappa=pairs_kappa,
                                     cap=cap, deadband=deadband,
                                     vote=pairs_vote),
        # ... and both together.
        "modes-nt": lambda: ModesPolicy(quorum=quorum, kappa=pairs_kappa,
                                        cap=cap, deadband=deadband,
                                        vote=pairs_vote, boundary=True),
    }


# --------------------------------------------------------------------- modes

def run_historical(windows, kappa, cap, deadband, cost_bp, rho=3.0,
                   quorum=4, pairs_kappa=0.5, pairs_vote="vel",
                   trace_sym="cbBTC", trace_policy="vrate") -> dict:
    prices = load_hist_prices()
    weights = [1.0 / len(SYMS)] * len(SYMS)
    # The showcase trace defaults to cbBTC, which a charter-compliant subset
    # deliberately excludes; fall back to the widest-swinging member present.
    if trace_sym not in SYMS:
        trace_sym = max(SYMS, key=lambda x: SYNTH_SIGMA.get(x, 0.0))
    out = {"days": len(prices), "syms": SYMS,
           "prices": [[row[i] for row in prices] for i in range(len(SYMS))],
           "series": {}, "metrics": {}, "showcaseSym": trace_sym,
           "showcasePolicy": trace_policy}
    for name, mk in _policies(windows, kappa, cap, deadband, weights,
                              rho=rho, quorum=quorum,
                              pairs_kappa=pairs_kappa,
                              pairs_vote=pairs_vote).items():
        r = simulate(prices, mk(), weights, cost_bp=cost_bp,
                     trace_sym=trace_sym if name == trace_policy else None)
        out["series"][name] = r["series"]
        out["metrics"][name] = r["metrics"]
        if r["trace"] is not None:
            out["showcase"] = r["trace"]
    return out


def run_synthetic(windows, kappa, cap, deadband, cost_bp, years, seeds,
                  rho=3.0, quorum=4, pairs_kappa=0.5, pairs_vote="vel") -> dict:
    weights = [1.0 / len(SYMS)] * len(SYMS)
    per_seed: dict[str, list[dict]] = {}
    example: dict[str, dict] = {}
    for k in range(seeds):
        prices = synth_prices(seed=0xB0C + k, years=years)
        for name, mk in _policies(windows, kappa, cap, deadband, weights,
                                  rho=rho, quorum=quorum,
                                  pairs_kappa=pairs_kappa,
                                  pairs_vote=pairs_vote).items():
            r = simulate(prices, mk(), weights, cost_bp=cost_bp)
            per_seed.setdefault(name, []).append(r["metrics"])
            if k == 0:
                example[name] = {"nav": r["series"]["nav"][::7],
                                 "meanAbsDev": r["series"]["meanAbsDev"][::7]}
    metrics = {}
    for name, ms in per_seed.items():
        agg = {}
        for key in ms[0]:
            vals = [m[key] for m in ms]
            agg[key] = sum(vals) / len(vals)
            if len(vals) > 1:
                agg[key + "Std"] = statistics.stdev(vals)
        hold_cagr = [m["cagrPct"] for m in per_seed["hold"]]
        own_cagr = [m["cagrPct"] for m in ms]
        prem = [100.0 * (o - h) for o, h in zip(own_cagr, hold_cagr)]  # bp/yr
        agg["premiumVsHoldBpYr"] = sum(prem) / len(prem)
        if len(prem) > 1:
            agg["premiumVsHoldBpYrStd"] = statistics.stdev(prem)
        metrics[name] = agg
    return {"years": years, "seeds": seeds, "metrics": metrics,
            "example": example, "exampleStride": 7}


def run_sweep(windows, kappa, cap, deadband, cost_bp, years, seeds,
              rho=3.0, policy_name="factor") -> dict:
    """Coordinate sweep: premium and TE vs window per constituent.

    Validates derive_windows(): for each symbol, vary only its X_i across
    SWEEP_WINDOWS (others at default) and average the swept policy's premium
    over seeds.  The best window should track the M2-lag-derived default.
    """
    weights = [1.0 / len(SYMS)] * len(SYMS)
    paths = [synth_prices(seed=0xB0C + k, years=years) for k in range(seeds)]
    hold_cagr = [simulate(p, HoldPolicy(), weights,
                          cost_bp=cost_bp)["metrics"]["cagrPct"]
                 for p in paths]

    def mk(wins):
        if policy_name == "vrate":
            return VratePolicy(wins, weights, rho=rho, cap=cap,
                               deadband=deadband)
        return FactorPolicy(wins, kappa=kappa, cap=cap, deadband=deadband)

    prem: dict[str, list[float]] = {}
    te: dict[str, list[float]] = {}
    for sym in SYMS:
        prem[sym] = []
        te[sym] = []
        for w in SWEEP_WINDOWS:
            wins = dict(windows)
            wins[sym] = w
            ps, ts = [], []
            for k, p in enumerate(paths):
                r = simulate(p, mk(wins), weights, cost_bp=cost_bp)
                ps.append(100.0 * (r["metrics"]["cagrPct"] - hold_cagr[k]))
                ts.append(r["metrics"]["teMeanAbsPct"])
            prem[sym].append(sum(ps) / len(ps))
            te[sym].append(sum(ts) / len(ts))
    best = {s: SWEEP_WINDOWS[max(range(len(SWEEP_WINDOWS)),
                                 key=lambda j: prem[s][j])] for s in SYMS}
    return {"windows": SWEEP_WINDOWS, "premiumBpYr": prem, "teMeanAbsPct": te,
            "defaultWindow": windows, "bestWindow": best,
            "policy": policy_name}


def fit_lags() -> None:
    """Re-fit LAG_TABLE from the vendored monthly series (prints a table)."""
    from alberta_buck.sim.quotes.fetch_feedstock import _yoy, lag_scan
    from alberta_buck.sim.quotes import ingest
    from datetime import date

    def monthly(series):
        out = {}
        for d, v in series:
            out.setdefault(d.replace(day=1), v)
        return sorted(out.items())

    m2 = _yoy(ingest._load_us_csv("m2"))
    src = {
        "PAXG": ingest.load_gold(),
        "cbBTC": monthly(ingest.load_btc()),
        "NRGC": ingest.load_energy_us(),
        "CNST": ingest.load_construction_us(),
        "FOOD": ingest.load_food_us(),
        "LABR": ingest.load_labour_us(),
    }
    print(f"{'sym':6} {'overlap':>8} {'corr@0':>8} {'peakLag':>8} {'peakCorr':>9}")
    for sym, series in src.items():
        py = {d: v for d, v in _yoy(series).items() if d >= date(1995, 1, 1)}
        scan = lag_scan(py, m2, lags=range(0, 37))
        n0, c0 = scan[0]
        peak_l, (_, peak_c) = max(scan.items(), key=lambda kv: kv[1][1])
        print(f"{sym:6} {n0:8} {c0:8.3f} {peak_l:8d} {peak_c:9.3f}")


# ---------------------------------------------------------------------- main

def _print_metrics(title: str, metrics: dict[str, dict]) -> None:
    print(f"\n{title}")
    keys = ["cagrPct", "teMeanAbsPct", "turnoverPerYr", "trades",
            "timingMeanAbsDevPct", "capture90Bp"]
    hdr = f"{'policy':8}" + "".join(f"{k:>20}" for k in keys)
    print(hdr)
    for name, m in metrics.items():
        row = f"{name:8}"
        for k in keys:
            v = m.get(k, 0.0)
            row += f"{v:20.3f}" if isinstance(v, float) else f"{v:20d}"
        print(row)


def run(mode: str = "both", years: float = 20.0, seeds: int = 5,
        cost_bp: float = 30.0, cap_bp: float = 50.0, kappa: float = 0.08,
        rho: float = 3.0, quorum: int = 4, pairs_kappa: float = 0.5,
        pairs_vote: str = "vel",
        deadband: float = 0.015, sweep: bool = False,
        sweep_policy: str = "factor",
        windows_override: dict[str, int] | None = None,
        syms: Sequence[str] | None = None,
        out: Path = DEFAULT_OUT) -> dict:
    # Before anything else: the policies and the synthetic generator read
    # SYMS at construction time.
    if syms:
        set_syms(syms)
    taus = {s_: TAUS[s_] for s_ in SYMS}
    windows = derive_windows(taus)
    if windows_override:
        windows.update(windows_override)
    cap = cap_bp / 1e4

    print("M2-lag-derived MA windows (days):",
          " ".join(f"{s}={windows[s]}" for s in SYMS))

    result: dict = {
        "config": {
            "syms": SYMS, "tausMonths": taus, "windows": windows,
            "kappa": kappa, "rho": rho, "capBpPerDay": cap_bp,
            "deadband": deadband, "costBp": cost_bp, "years": years,
            "seeds": seeds,
        },
        "lagTable": [
            {"sym": s, "tauMonths": t, "peakCorr": c, "note": n}
            for s, _, t, c, n in LAG_TABLE if s in SYMS
        ],
    }
    if mode in ("both", "historical"):
        result["historical"] = run_historical(windows, kappa, cap, deadband,
                                              cost_bp, rho=rho, quorum=quorum,
                                              pairs_kappa=pairs_kappa,
                                              pairs_vote=pairs_vote)
        _print_metrics(
            f"historical (2020-09 .. 2025-09, {len(SYMS)} constituents: "
            f"{','.join(SYMS)}):", result["historical"]["metrics"])
    if mode in ("both", "synthetic"):
        result["synthetic"] = run_synthetic(windows, kappa, cap, deadband,
                                            cost_bp, years, seeds, rho=rho,
                                            quorum=quorum,
                                            pairs_kappa=pairs_kappa,
                                            pairs_vote=pairs_vote)
        _print_metrics(
            f"synthetic ({years:.0f}y x {seeds} seeds, mean):",
            result["synthetic"]["metrics"])
        prem = {n: m.get("premiumVsHoldBpYr", 0.0)
                for n, m in result["synthetic"]["metrics"].items()}
        print("premium vs hold (bp/yr): "
              + "  ".join(f"{n}={v:+.1f}" for n, v in prem.items()))
    if sweep:
        result["sweep"] = run_sweep(windows, kappa, cap, deadband, cost_bp,
                                    years, seeds, rho=rho,
                                    policy_name=sweep_policy)
        print(f"\nwindow sweep [{sweep_policy}] "
              "(best premium window vs M2-derived default):")
        for s in SYMS:
            print(f"  {s:6} best={result['sweep']['bestWindow'][s]:4d}d  "
                  f"default={windows[s]:4d}d")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result))
    print(f"\nWrote {out.relative_to(REPO) if out.is_relative_to(REPO) else out}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m alberta_buck.sim.rebalance_policy",
        description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("both", "historical", "synthetic"),
                        default="both")
    parser.add_argument("--years", type=float, default=20.0)
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--cost-bp", type=float, default=30.0)
    parser.add_argument("--cap-bp", type=float, default=50.0,
                        help="max trade per constituent, bp of NAV per day")
    parser.add_argument("--kappa", type=float, default=0.08)
    parser.add_argument("--rho", type=float, default=3.0,
                        help="vrate match ratio: trade at rho x the observed "
                             "natural closure rate")
    parser.add_argument("--quorum", type=int, default=4,
                        help="pairs policy: scales that must vote the turn")
    parser.add_argument("--pairs-kappa", type=float, default=0.5)
    parser.add_argument("--pairs-vote", choices=("curv", "vel"),
                        default="vel",
                        help="pair turn vote: divergence decelerating (curv, "
                             "early) or gap already closing (vel, confirmed)")
    parser.add_argument("--deadband", type=float, default=0.015)
    parser.add_argument("--sweep", action="store_true",
                        help="coordinate window sweep on synthetic paths")
    parser.add_argument("--sweep-policy",
                        choices=("factor", "vrate", "pairs", "modes"),
                        default="factor")
    parser.add_argument("--windows", default=None,
                        help="override, e.g. cbBTC=120,FOOD=90")
    parser.add_argument("--syms", default=None,
                        help="restrict constituents, e.g. NRGC,CNST,FOOD,LABR "
                             "or the alias 'basics' -- the charter-compliant "
                             "subset with the non-reverting assets removed")
    parser.add_argument("--fit-lags", action="store_true",
                        help="re-fit LAG_TABLE from vendored series and exit")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    if args.fit_lags:
        fit_lags()
        return 0

    overrides = None
    if args.windows:
        overrides = {}
        for part in args.windows.split(","):
            sym, _, days = part.partition("=")
            overrides[sym.strip()] = int(days)

    syms = None
    if args.syms:
        syms = (BASICS if args.syms.strip().lower() == "basics"
                else [x.strip() for x in args.syms.split(",") if x.strip()])

    run(mode=args.mode, years=args.years, seeds=args.seeds, syms=syms,
        cost_bp=args.cost_bp, cap_bp=args.cap_bp, kappa=args.kappa,
        rho=args.rho, quorum=args.quorum, pairs_kappa=args.pairs_kappa,
        pairs_vote=args.pairs_vote,
        deadband=args.deadband, sweep=args.sweep,
        sweep_policy=args.sweep_policy,
        windows_override=overrides, out=Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
