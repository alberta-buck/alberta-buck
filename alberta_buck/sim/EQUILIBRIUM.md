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

## Results (banked milestone)
Over this work the system went from **diverging + bang-banging + flooding** to
**closed, tight, and flood-free**:
- **Loop closes:** slow integral-dominant PID + two-sided basket-anchored savers
  mean-revert basketValue instead of diverging.
- **Startup flood fixed** by the lifecycle borrower's pre-funding toll: day-0
  basketValue 1.0 (was a ~2.5 spike), max 1.29 (was 3.47), buck_usd max 8
  (was 42).
- **Volatility 3x tighter** (basketValue stdev 0.30 -> 0.09).

**Accepted residual (banked):** basketValue settles ~0.92 (BUCK mildly *rich*
vs the basket) with K riding high (~0.93).  BUCK mildly appreciating vs the
basket is deflationary money -- arguably on-design for a high-inertia anchor
that "deflates forever."  The one caveat is that K rides near its ceiling: the
controller is pinned high rather than holding an interior equilibrium.  We bank
this state; refinements are catalogued below.

Reference: `images/equilibrium-lifecycle.png` (+ its vector) is this milestone.

## Approaches forward
The recurring reason parity isn't hit *exactly* is structural: basketValueInBuck
is read from the **TOKEN/BUCK** pools, but the stabilizing agents (savers, the
borrower's reserve) trade on **BUCK/USDC** -- the two are linked only by arbs,
which lag at the coarse macro cadence.  Demand-side control of the exact
observable is therefore always indirect.  Refinements, in increasing effort:

- **(B) Reserve-accounting rework.**  Make the borrower's pre-issuance funding
  reserve a *rolling* requirement tied to OUTSTANDING drawn credit, released
  (sold back into the **TOKEN/BUCK** pools) as credit is retired -- so the
  adoption-ramp pre-demand unwinds instead of leaving a standing net demand.
  Target: basketValue on 1.0 with K settling in the interior ~0.75.  (A first
  attempt keyed the release off the tangled `drawn = reserve_held - signed`
  accounting and sold into BUCK/USDC; it regressed and was reverted.)
- **(C) Direct TOKEN/BUCK stabilization channel.**  Give savers/borrowers a
  direct channel on the controlled pool so demand acts on the observable without
  waiting for arbs.  Most principled, biggest change.

### Economic direction (beyond the current sim)
The current sim fights *deflation* (BUCK richer than the basket).  In reality,
demand for BUCK credit will out-strip deflationary tendencies, so the harder
regime is the opposite one:

- **Issuance limiting needs BOTH a primary and a backstop function.**
  *Primary:* the counter-cyclical insurance **funding factor** as a
  market-driven, valuation-sensitive pre-issuance demand toll (currently
  *simulated* in the agent, because zero-premium BuckCredit bypasses the
  on-chain gate).  *Backstop:* a hard supply-relative cap (new credit <= X% of
  BUCK supply per period) against pathological floods.
- **At civilization scale**, when BuckCredit issuance and FX-mediated debt
  retirement (BUCK credit -> USDC to retire fiat mortgages) really hit, the
  primary problems flip to **inflation and BUCK/fiat pool depth** -- the system
  must absorb enormous issuance without draining the fiat gateways.
- A **richer agent simulator** (heterogeneous per-actor credit lifecycles,
  non-zero-premium insurance so the funding factor is enforced on-chain, and
  explicit fiat-gateway liquidity) would model these regimes; future modelling
  will refine this.

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
