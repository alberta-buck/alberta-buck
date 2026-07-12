# BuckBasket Rebalance Policy — deviation x MA-acceleration factor

Working draft. Design + measured model for the standalone `rebalance()`
increment left stubbed in `BASKET-REDESIGN.md` §"Still stubbed". Python model:
`alberta_buck/sim/rebalance_policy.py` (`make nix-sim-policy`, `--sweep` for
the window sweep); results vector `test/vectors/rebalance-policy.json`, figure
`images/rebalance-policy.png`.

## 1. Premise

Basket commodities trail the M2 money supply with per-commodity Cantillon
lags. Measured (YoY-growth Pearson vs M2SL, post-1995, lags 0..36 mo; re-fit
with `python -m alberta_buck.sim.rebalance_policy --fit-lags`):

| constituent | peak lag (mo) | peak corr | tier |
|---|---|---|---|
| cbBTC | 0 | 0.08 (weak, 178-mo sample) | monetary (fast) |
| PAXG | censored @36 | 0.10 (weak) | monetary prior (fast) |
| CNST | 9–10 | 0.48–0.52 | industrial (mid) |
| NRGC | 13 | 0.33–0.39 | industrial (mid) |
| LABR | 15–16 | 0.32–0.38 | sticky (slow) |
| FOOD | 23 | 0.21–0.26 | sticky (slow) |

Fast responders overshoot their basket share when M2 surges; the share
excursion persists until the laggards catch up, then levels off and reverts.
A rebalancer keyed to *instantaneous* deviation (what spot-based flow routing
approximates) fights the excursion all the way out and pays the full
trend-bleed; one that waits for the excursion to *level off* commits its flow
at maximum mispricing.

## 2. The factor

Per constituent i, sampled at a fixed cadence:

    delta_i = w_i / w*_i - 1                  raw share deviation (NOW)
    m_i     = X_i-day MA of delta_i           the delayed view
    v_i, a_i = strided 1st/2nd differences of m_i
    toward_i = -sign(m_i) * a_i               accel of MA back toward target
    gate_i   = clamp(toward_i / RMS(a_i), 0, 1)
    effort_i = kappa * |delta_i| * gate_i^2, capped per step

    trade against sign(delta_i), only when sign(m_i) == sign(delta_i)

Design points learned from the model (each was a measured failure first):

- **Direction and magnitude come from the RAW deviation; the MA contributes
  only timing.** A lagging MA that still shows "overweight" after the raw
  share has reverted through target must not trade (it sells an underweight).
  Requiring `sign(m) == sign(delta)` removes the wrong-way trades.
- **A leash bounds the quench.** A secular trend that never levels off (BTC
  2020–25) runs the deviation away unboundedly while the gate stays shut.
  Beyond |delta| > 30% the policy trades at cap regardless of the gate
  (hysteresis re-arm at 25%). The factor times trades *within* the leash; the
  leash enforces the mandate.
- **Proceeds must recycle.** Gated sells fire more often than gated buys; the
  BUCK proceeds must flow back into underweight constituents (this is exactly
  `sweepTreasury` / `investFromBucks(most-underweight)`), but never into a
  constituent the policy sold the same step — a stale-signal sell paired with
  a raw-underweight rebuy wash-trades the AMM fee away.
- **gate² beats gate.** Squaring suppresses weak flickers of toward-target
  acceleration; and on-chain it needs no square root (compare `toward·|toward|`
  against the running EMA of `a²` directly).

## 2b. The velocity-regime, rate-matched variant (`vrate`)

The MA's *velocity* already tells the whole regime story, more explicitly
than the acceleration gate:

    u = -sign(m) * v(MA)          positive = the delayed view is closing

    u < -eps   receding   deviation still growing: don't trade
    |u| <= eps level      turning: start, but not too fast
    u > +eps   gaining    closing: complete the remaining rebalancing

(eps scales as a fraction of |m|/X -- the speed that would close the MA gap
in one window -- so there is no absolute threshold to tune.)

Sizing is *rate-matched* rather than kappa-scaled: a 1-week EMA of
d(delta)/dt measures the speed the gap is closing on its own; trade at
`rho` times that rate (as NAV flow: `rho * c * w*`).  While level, with no
closure observed yet, the prior is "lose half the distance in one MA
window", started at half rate.  Effort therefore peaks just after the turn
-- closure accelerating while the deviation is still near maximum -- and
tapers as the gap closes.  Two structural niceties:

- **Self-calibrating**: no kappa, no acceleration normalizer; the only
  economic parameter is the match ratio rho.
- **Self-limiting**: the policy's own flow is part of the observed closure
  rate, so raising rho saturates (premium flattens ~rho 3-5) instead of
  overshooting.  Default rho = 3.

Same sign-agreement rule, deadband, and leash as the factor policy.  On-chain
it is *simpler*: the acceleration and its RMS normalizer (`a2Ema`) are
dropped; one extra int128 (the 1-week EMA of d(delta)) is added.

Caveat (measured): vrate concentrates trades where observed closure is
fastest, which in a *trending* market means selling into sharp V-shaped
pullbacks that then resume -- its historical capture90 is the worst of all
policies (-676bp) even while its NAV loss stays comparable to prop's on
~1/3 the turnover.  In the mean-reverting regime the same concentration is
maximally right (+514bp capture90, the best).  The factor gate is the more
trend-robust of the two; vrate is the more efficient harvester.

## 2c. The differential-mode multi-scale variant (`pairs`)

Perry's reconception (2026-07-11): the single-window share-vs-target signal
harvests only the macro (M2) scale and concedes every mid-size swing to
continuous rebalancing; and shares measured from pool BUCK reserves carry
numeraire/flow noise. Work instead in *differential mode* — the 3-phase-power
analogy: profit lives in the differentials between commodity legs; the common
mode (BUCK/USDC valuation) is the K-controller's job and cancels exactly in
cross-commodity log price ratios.

- **Per-leg EMA ladder**: K=7 windows (5..320d geometric) of EMAs on log
  spot. EMA linearity ⇒ every PAIR's MA/velocity/curvature at every scale is
  the difference of two legs' ladders — the full N(N-1)/2 differential graph
  from O(N*K) state.
- **Quorum turn detector**: per pair, each scale votes iff its MA-gap agrees
  in sign with the raw pairwise imbalance AND its curvature points back
  toward equilibrium (the factor gate, per scale). votes >= quorum (default
  4/7) opens the pair. Short windows catch mid-size swings; long windows the
  M2 excursions; no single scale can fire alone.
- **Effort** = kappa(0.5) x |pairwise imbalance d_ij| x votes/K, capped,
  pairwise leash 30%/25%. `d_ij = ln((1+delta_i)/(1+delta_j))` — also
  numeraire-free.
- **Matched pair trades** (sell rich leg, buy poor leg, equal value):
  self-financing, no cash residue, structurally wash-proof. Per-leg nets
  capped with proportional pair rescale.
- Progressive warmup: short windows vote from ~day 20 (no 116-day silence).

**Results (synthetic 20y x 5 seeds, quorum=4):**

| cost/leg | prop | band | factor | vrate | pairs |
|---|---|---|---|---|---|
| 30bp | +430 | +417 | +393 | +340 | **+496** |
| 100bp | +365 | +308 | +357 | +313 | **+422** |
| 250bp | +228 | +89 | **+283** | +271 | +262 |

pairs **beats prop's gross harvest** (multi-scale coverage recovered the
mid-size swings AND the pair-matched execution harvests more per unit
imbalance) and dominates every policy through ~200bp/leg costs; factor
retakes the lead only at extreme costs. Turnover 0.96/yr, TE 5.4%,
capture90 +201. Historical (2020-25 trend): CAGR 17.9% — better than vrate
(18.1%→ comparable) but still below factor (19.3%)/prop (18.4%): the quorum
fires on BTC's consolidations during a relentless trend; the leash bounds
it. Quorum scan: q3 max premium (+508, turnover 1.39); q5 min turnover
(0.65, +460); **q4 the knee**. factor remains the trend-regime pick; pairs
the reverting-regime pick.

**Vote mode** (`--pairs-vote`, one enum knob — Perry's spec vs the
implementation's default, measured at quorum 4):

| vote | synth premium | turnover | capture90 | hist CAGR |
|---|---|---|---|---|
| `curv` (divergence *decelerating* — early, at the top) | +496 | 0.96 | +201 | 17.9% |
| `vel` (gap *already closing* — confirmed turn; the spec) | +421 | 0.60 | +270 | **18.4%** |

`curv` buys ~75bp/yr more gross harvest with 60% more turnover and worse
trend bleed; `vel` trades later and less, with better per-trade capture and
prop-level trend robustness — net premiums cross at ~235bp/leg costs.
`vel` is the better-balanced deployment default; `curv` the aggressive
setting for cheap-trading, reverting conditions.

## 3. Window selection

`derive_windows()`: a constituent's share-excursion timescale is its M2 lag
*relative to the basket median* (fast movers lead the pack by the median lag;
laggards trail it), floored by the idiosyncratic mean-reversion time
(~4 months); the MA window is one third of that, clipped to [30, 180] days.
Defaults: PAXG 86d, cbBTC 116d, NRGC/CNST/LABR 40d, FOOD 116d.

The coordinate sweep (`--sweep`, synthetic ground truth) shows the premium is
**flat to window choice within 30–180d** (±15bp on ~380bp/yr). The one peaked
curve is cbBTC (best 45d): its idiosyncratic vol sets a shorter excursion
timescale than its differential M2 lag. So the ideal X is
`min(M2-relative-lag scale, idio-reversion scale)`; the M2 tiers mainly
determine *which side* of a monetary surge each constituent lands on. Window
choice is forgiving — safe to fix per-constituent at deploy time.

For `vrate` the sweep tilts mildly but consistently toward the short end
(30–45d best for most constituents, ~20bp spread): its MA is only a regime
detector — direction sign + receding/level/gaining — while sizing comes from
the 1-week closure EMA, so a faster regime view just flips earlier.  A single
common 30–60d window is adequate for vrate; the per-commodity M2 windows
matter more for the acceleration-gated factor.

## 4. Measured results (cost 30bp/leg, cap 50bp NAV/day, kappa 0.08)

Synthetic (20y x 4–5 seeds, transient M2-lag excursions — the premise):

| policy | premium vs hold | TE mean abs dev | turnover | trades | capture90 |
|---|---|---|---|---|---|
| prop (continuous ~ spot routing) | +423 bp/yr | 2.3% | 0.82 NAV/yr | 23k | +219 bp |
| band (5% threshold, on-chain agent) | +411 bp/yr | 2.3% | 1.42 NAV/yr | 6k | +138 bp |
| **factor** | **+381 bp/yr** | 5.0% | 0.44 NAV/yr | 6k | +341 bp |
| **vrate** (rho=3) | +322 bp/yr | 7.3% | **0.25 NAV/yr** | 7.6k | **+514 bp** |

Historical replay (hist-*.csv 2020-09..2025-09; BTC x9 secular trend — the
stress case):

| policy | CAGR | TE | turnover | capture90 |
|---|---|---|---|---|
| hold (abandons mandate) | 22.3% | 43.8% | 0 | — |
| prop | 18.4% | 2.1% | 0.70 | −191 bp |
| band | 18.0% | 2.2% | 1.16 | −118 bp |
| **factor** | **19.3%** | 4.8% | 0.45 | −183 bp |
| **vrate** (rho=3) | 18.1% | 6.8% | **0.27** | −676 bp |

Read: the gated policies form an efficiency frontier. prop maximizes raw
premium and tracking tightness but pays full turnover and trend-bleed.
factor captures ~90% of the premium at ~half the turnover and is the most
trend-robust (best active CAGR historically). vrate is the most *efficient*
harvester — ~75% of the premium at 30% of the turnover, with by far the best
per-trade timing in the reverting regime (trades at 12.6% mean deviation;
premium-per-unit-turnover ~1300bp vs factor ~870 vs prop ~515) — but its
closure-rate concentration is the most exposed to trend whipsaws.  All trade
tracking tightness (5–7% vs 2%) for cost and timing — the right trade for a
basket whose deviations are the harvest, not the harm.

## 5. Solidity state machine

> **Status: IMPLEMENTED** as `src/basket/BasketRebalanceDirector.sol` — a
> standalone advisor with exact closed-form EMA catch-up (m, vEma, ddotEma),
> the round-robin `poke(maxWork)` work wheel, cached per-pool observations
> with running sums, and O(1)-cached `depositHint()`/`redeemHint()`/
> `effortOf(i)` advisory reads. Tests: `make nix-test-director` (10 tests
> incl. a 256-run fuzz of the lazy==diligent invariant),
> `make nix-test-director-regimes` (parallel window x rho matrix — it caught
> a real velocity-catch-up approximation bug), `make nix-sim-director`
> (30-day Anvil sim; `DirectorKeeperAgent` pokes + executes advice).
> Measured gas: ~36.5k/poke (1-epoch gap), ~19.9k (50-epoch gap — flat in
> gap size), ~3.1k fresh-epoch guard. Article:
> `alberta-buck-rebalance.org`. The section below is the original design
> sketch; the vrate variant is what shipped.

Everything above is O(1) state and O(1) work per constituent per step; no
history arrays needed on-chain:

- SMA -> **EMA**: `m += (delta - m) * beta`, beta = 2/(X+1) in 1e18 fixed.
- Strided differences -> the sampling cadence IS the stride: each `poke`
  shifts a 3-deep ring `m2 <- m1 <- m0 <- m`, then
  `v = m0 - m1`, `a = m0 - 2*m1 + m2`.
- Gate normalizer: running EMA of `a^2`; `gate^2 = clamp(a*|a| / a2EMA, 0, 1)`
  — **no sqrt**.

Per-constituent signal storage (~3 slots): `int128 m; int128 m1; int128 m2;
uint128 a2Ema; uint40 lastPoke; bool leashed;` plus global params
(`kappa, capBp, deadbandBp, leashBp, leashInnerBp, sampleInterval[i]`).
The `vrate` variant is simpler still: drop `a2Ema` (no acceleration, no
normalizer, no gate²), add `int128 ddotEma` (the 1-week EMA of d(delta)) and
`int128 prevDelta`; the effort formula is one comparison (`u` vs `eps`) and
one multiply (`rho * c * w*`), all integer-friendly.

Permissionless keeper steps, each gas-bounded and independently invocable
(the `sweepTreasury` pattern; all venue mechanics already exist in
`IBuckBasketVenue`):

1. **`pokeSignal(i)`** — rate-limited by `lastPoke + sampleInterval`. Reads
   the pool's spot + depositor reserve (`poolBuckValues` already computes
   both, with the TWAP manipulation guard), computes `delta_i`, updates
   `m/m1/m2/a2Ema`, stores the signed effort for step 2. No trading.
2. **`rebalanceSource(i)`** — requires stored effort < 0 (or leash on an
   overweight): `withdrawLiquidity(i, L)` sized to
   `min(effort, capBp) * NAV`, then `convertIntoBucks` the TOKEN leg;
   proceeds accrue to `treasuryBuckPending`-style `rebalancePending`.
3. **`rebalanceSink()`** — `investFromBucks(rebalancePending, hint)` where
   `hint` = the underweight constituent with the highest stored buy-effort
   (fallback: most underweight, i.e. today's behavior), excluding any
   constituent sourced in the same round (the wash guard).

A full rebalance action is thus 2–3 cheap independent transactions; anyone
may drive any step at any time; caps + rate limits + the existing
spot-vs-TWAP deviation guard bound what a hostile keeper or MEV sandwich can
extract per step. Increment 0 (zero new trades): use the stored factor only
to pick `investFromBucks`' hint and to weight `_allocateSellHigh`'s draw —
the deposit/redeem/treasury flows the basket already routes then do the
well-timed rebalancing for free.

## 6. Open items

- Elasticity: LABR/FOOD long-run M2 pass-through < 1 in the data; the model
  assumes 1. A pass-through-weighted target (or periodic weight re-declare)
  is the governance-level answer to permanent-drift constituents (BTC).
- The synthetic driver uses a single global M2 surge process; no cross-sector
  idiosyncratic correlation. Enrich if the sweep is ever used to *set* (not
  bound) windows.
- Keeper bounty economics (pay a few bp of each productive step from
  treasury) — needed on mainnet, not modeled.
- Wire `--fit-lags` into `fetch_feedstock.py`'s report so the lag table
  refreshes with the FRED data pulls.
