# Equilibrium Simulation -- Current State

A closed-loop simulation of BUCK-K value stabilization on real macro data.
Agents issue/redeem BUCK credit and save BUCK; the on-chain `BuckKControllerDirect`
PID adjusts **K** (the LTV credit cap) to defend basket parity
(1 BUCK == 1 basket of TOKEN units).  Run: `python -m alberta_buck.sim --scenario
equilibrium --years N --ticks-per-day 1 --day-step 10`.

## The control loop
- **`BuckKControllerDirect`** (`src/`): rescaled ppm PID (process/error in 1e6,
  dt in seconds), integral-dominant, deliberately **slow** (integral time ~90d,
  tiny proportional kick) so K is a structural lever that glides over months
  while private demand does the fast stabilization.
- **K is the LTV cap**: `creditLimit = collateralValue * K / 1e18`; max system
  leverage `1/(1-K)`.  Initialized at the observed equilibrium **K0 = 0.75**
  (~4x), rails `(0, 1.0)`.
- **`PidKeeperAgent`** advances `compute()` each tick (the money cadence).

## The basket (recomposed toward M2-laggards)
Real US FRED feeds (`fetch_feedstock.py`, vendored under `quotes/data/us/`):
- **CNST** (construction: steel/lumber/cement/gravel PPI),
- **LABR** (US wage, AHETPI),
- **NRGC** (retail energy: gasoline/electricity/natural gas),
- **FOOD** (retail: beef/bread/bananas).

Weights via `addBasketToken` (stick-breaking-inverted at deploy):
LABR 2400 / CNST 2200 / FOOD 2000 / NRGC 2000 (anchor ~86%) + PAXG 800 /
cbBTC 600 (decorrelated hard/crypto satellite ~14%).

**Rationale (M2-lag study, `images/m2-lag-correlation.png`):** real-economy
goods lag M2 by ~9-24 months (good, slow anchors); gold/BTC are *decorrelated*
from M2 year-over-year growth (poor monetary anchors, kept as a small satellite
for the passive rebalancing harvest from their independent dynamics).

## The agents (`equilibrium_agents.py`)
- **`FatCreditBorrowerAgent`** (lifecycle): a pool of BuckCredit "properties".
  - *Adoption ramp*: target draw = `util_target * creditLimit * cap_frac(day)`,
    a logistic S-curve (+ optional one-off uptake shock), so issuance grows in
    rather than flooding at t=0.
  - *Pre-issuance funding factor* (simulated -- zero-premium credit bypasses the
    on-chain gate): before issuing `delta`, buy+lock a reserve `R = delta * ff`,
    `ff = (1+FF_CYC*max(0,bvib-1)) * (1+FF_AMP*issue_rate^FF_POW)` --
    counter-cyclical AND super-linear in the system-wide issue rate, so a rapid
    uptake demands very strong pre-BUCK-demand.  Underfunded issuance is throttled.
  - *Discount-driven redemption*: retire harder when BUCK is cheap vs the basket
    ("money at a discount").
- **`SaverAgent`**: counter-cyclical, **basket-anchored** -- buys when
  `basketValueInBuck > 1` (BUCK cheap vs basket), sells when `< 1`, keeping a
  reserve.  Scaled so private demand is comparable to BUCK supply.
- **Support**: MarketMakerWhale (pegs TOKEN/USDC to the CSV feeds),
  AnonymousArb + TokenAccumulator (propagate into TOKEN/BUCK + BUCK/USDC),
  BootstrapDM (seed pools).
- **Regime shocks**: a few agents each redraw one knob every 182 days, staggered
  so ~10 isolated perturbations land over a 5-year run and the loop re-settles.

## Cadence & runtime
`day_step` coarse-macro mode advances N calendar days per iteration (5-year run
at 10-day steps ~= 183 iterations) to fit a bounded runtime; the loop checkpoints
the vector every 25 days for kill-resilience.

## Results so far
- **Loop closes.** With slow PID + two-sided basket-anchored savers, basketValue
  mean-reverts around parity instead of diverging.
- **Startup flood fixed** by the lifecycle borrower's pre-funding toll: day-0
  basketValue 1.0 (was ~2.5 spike), max 1.29 (was 3.47), buck_usd max 8 (was 42),
  and overall volatility 3x tighter (stdev 0.30 -> 0.09).
- **Open tuning issue:** the lifecycle borrower *over-corrects* -- basketValue
  settles ~0.92 (BUCK chronically rich) with K railed near the ceiling (0.93) --
  because the funding reserve is **cumulative and never released**, so the
  adoption ramp leaves a standing net BUCK demand.
  - *Fix in progress:* make the reserve a **rolling** requirement proportional to
    *outstanding* drawn credit, released on retirement, so pre-funding demand
    unwinds over the lifecycle (net-neutral) and only spikes during rapid uptake.
    Expected to re-center basketValue on 1.0 with K resting ~0.75.

## Key files
- `src/BuckKControllerDirect.sol` -- the PID.
- `alberta_buck/sim/equilibrium_agents.py` -- borrower / saver / keeper.
- `alberta_buck/sim/scenario.py` (`build_equilibrium`) -- scenario + weights.
- `alberta_buck/sim/gen_historical.py` -- daily CSV feeds from the quote source.
- `alberta_buck/sim/quotes/` -- price-feed module (`fetch_feedstock`, `ingest`,
  `metrics`); `data/us/` holds the vendored FRED series.
- `alberta_buck/sim/plot_equilibrium.py` -- the 6-pane diagnostic plot.
- Reference plots: `images/equilibrium-lifecycle.png` (current),
  `images/equilibrium-k075.png` (pre-lifecycle), `images/m2-lag-correlation.png`.
