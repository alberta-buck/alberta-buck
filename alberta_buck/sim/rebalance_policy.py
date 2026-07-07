"""BuckBasket slow-rebalance policy model: deviation x MA-acceleration factor.

Premise (BASKET-REDESIGN.md; alberta-buck-ethereum-basket.org): the BUCK basket
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

Policies compared: hold (never rebalance), prop (continuous proportional to
instantaneous deviation -- what spot-based flow routing approximates), band
(threshold 5%, the BuckBasketRebalancerAgent approach), factor and vrate
(this model).

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

    def efforts(self, deltas: Sequence[float]) -> list[float]:
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

    def efforts(self, deltas: Sequence[float]) -> list[float]:
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

    def efforts(self, deltas: Sequence[float]) -> list[float]:
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

    def efforts(self, deltas: Sequence[float]) -> list[float]:
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

    def efforts(self, deltas: Sequence[float]) -> list[float]:
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
        efforts = policy.efforts(deltas)

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
              rho: float = 3.0) -> dict[str, Callable[[], object]]:
    return {
        "hold": lambda: HoldPolicy(),
        "prop": lambda: PropPolicy(cap=cap, deadband=deadband),
        "band": lambda: BandPolicy(cap=cap),
        "factor": lambda: FactorPolicy(windows, kappa=kappa, cap=cap,
                                       deadband=deadband),
        "vrate": lambda: VratePolicy(windows, weights, rho=rho, cap=cap,
                                     deadband=deadband),
    }


# --------------------------------------------------------------------- modes

def run_historical(windows, kappa, cap, deadband, cost_bp, rho=3.0,
                   trace_sym="cbBTC", trace_policy="vrate") -> dict:
    prices = load_hist_prices()
    weights = [1.0 / len(SYMS)] * len(SYMS)
    out = {"days": len(prices), "syms": SYMS,
           "prices": [[row[i] for row in prices] for i in range(len(SYMS))],
           "series": {}, "metrics": {}, "showcaseSym": trace_sym,
           "showcasePolicy": trace_policy}
    for name, mk in _policies(windows, kappa, cap, deadband, weights,
                              rho=rho).items():
        r = simulate(prices, mk(), weights, cost_bp=cost_bp,
                     trace_sym=trace_sym if name == trace_policy else None)
        out["series"][name] = r["series"]
        out["metrics"][name] = r["metrics"]
        if r["trace"] is not None:
            out["showcase"] = r["trace"]
    return out


def run_synthetic(windows, kappa, cap, deadband, cost_bp, years, seeds,
                  rho=3.0) -> dict:
    weights = [1.0 / len(SYMS)] * len(SYMS)
    per_seed: dict[str, list[dict]] = {}
    example: dict[str, dict] = {}
    for k in range(seeds):
        prices = synth_prices(seed=0xB0C + k, years=years)
        for name, mk in _policies(windows, kappa, cap, deadband, weights,
                                  rho=rho).items():
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
        rho: float = 3.0, deadband: float = 0.015, sweep: bool = False,
        sweep_policy: str = "factor",
        windows_override: dict[str, int] | None = None,
        out: Path = DEFAULT_OUT) -> dict:
    windows = derive_windows(TAUS)
    if windows_override:
        windows.update(windows_override)
    cap = cap_bp / 1e4

    print("M2-lag-derived MA windows (days):",
          " ".join(f"{s}={windows[s]}" for s in SYMS))

    result: dict = {
        "config": {
            "syms": SYMS, "tausMonths": TAUS, "windows": windows,
            "kappa": kappa, "rho": rho, "capBpPerDay": cap_bp,
            "deadband": deadband, "costBp": cost_bp, "years": years,
            "seeds": seeds,
        },
        "lagTable": [
            {"sym": s, "tauMonths": t, "peakCorr": c, "note": n}
            for s, _, t, c, n in LAG_TABLE
        ],
    }
    if mode in ("both", "historical"):
        result["historical"] = run_historical(windows, kappa, cap, deadband,
                                              cost_bp, rho=rho)
        _print_metrics("historical (2020-09 .. 2025-09, 6 constituents):",
                       result["historical"]["metrics"])
    if mode in ("both", "synthetic"):
        result["synthetic"] = run_synthetic(windows, kappa, cap, deadband,
                                            cost_bp, years, seeds, rho=rho)
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
    parser.add_argument("--deadband", type=float, default=0.015)
    parser.add_argument("--sweep", action="store_true",
                        help="coordinate window sweep on synthetic paths")
    parser.add_argument("--sweep-policy", choices=("factor", "vrate"),
                        default="factor")
    parser.add_argument("--windows", default=None,
                        help="override, e.g. cbBTC=120,FOOD=90")
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

    run(mode=args.mode, years=args.years, seeds=args.seeds,
        cost_bp=args.cost_bp, cap_bp=args.cap_bp, kappa=args.kappa,
        rho=args.rho, deadband=args.deadband, sweep=args.sweep,
        sweep_policy=args.sweep_policy,
        windows_override=overrides, out=Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
