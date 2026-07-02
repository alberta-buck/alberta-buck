# Equilibrium Simulation -- Current State

A closed-loop simulation of BUCK-K value stabilization on real macro data.
Agents issue/redeem BUCK credit and save BUCK; the on-chain `BuckKControllerDirect`
PID adjusts **K** (the LTV credit cap) to defend basket parity
(1 BUCK == 1 basket of TOKEN units).

Run the standard baseline:

    python -m alberta_buck.sim --experiment alberta_buck/sim/experiments/baseline-5yr.toml

or ad hoc: `python -m alberta_buck.sim --scenario equilibrium --years N
--ticks-per-day 1 --day-step 10`.

## The control loop
- **`BuckKControllerDirect`** (`src/`): rescaled ppm PID (process/error in 1e6,
  dt in seconds), integral-dominant, deliberately **slow** (integral time ~90d,
  tiny proportional kick) so K is a structural lever that glides over months
  while private demand does the fast stabilization.
- **K is the LTV cap**: `creditLimit = collateralValue * K / 1e18`; max system
  leverage `1/(1-K)`.  Initialized at **K0 = 0.75** (~4x), rails **(0, 0.95)**
  -- the ceiling was moved off 1.0 (the infinite-leverage spiral boundary) so
  a railed K is still solvent (20x) and "pinned vs settled" is observable.
- **Governance setters** (all forge-tested): `setRails` (clamps the live K),
  **bumpless** `retune` (re-derives I so a gain change never steps the
  output; `_rederiveI()` is overridden for the ppm algebra), and bumpless
  `setBuckK0`.  These are the mid-run intervention surface.
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
  - *ROLLING pre-issuance funding reserve* (simulated -- zero-premium credit
    bypasses the on-chain gate): the reserve lives in a separate **escrow
    proxy** (drawn credit is chain-truth `-signed(proxy)`; the escrow's
    holdings never contaminate it).  Each issuance pushes an
    `(outstanding, ff)` tranche; the requirement is `sum(outstanding*ff)`
    -- it FALLS as credit retires.  Retirement pops tranches FIFO into a
    pending-release buffer that is **sold back into the TOKEN/BUCK pools**
    (the controlled observable) counter-cyclically -- only while
    `basketValueInBuck <= ~1`, i.e. exactly when the push is toward parity --
    with TOKEN proceeds recycled to USDC through the deep truth pools.
  - *Two-sided funding factor* (mirrors the on-chain `fundingFactor()`):
    `ff = max(0, 1 + FF_CYC*(bvib-1)/bvib) * (1 + FF_AMP*issue_rate^FF_POW)`.
    Above parity a hot uptake demands a heavy pre-buy toll (the throttle bites
    when it can't be funded); below parity the toll **saturates toward zero**,
    so issuance is cheap exactly when new supply pushes toward parity.
  - *Discount-driven redemption*: retire harder when BUCK is cheap vs the
    basket ("money at a discount").
- **`SaverAgent`**: counter-cyclical, **basket-anchored** -- buys when
  `basketValueInBuck > 1` (BUCK cheap vs basket), sells when `< 1`, keeping a
  reserve.  Scaled so private demand is comparable to BUCK supply.  (A
  regime-redraw bug that permanently cut a saver's demand 10x mid-run is
  fixed: redraws come from the same spec as setup.)
- **Support**: MarketMakerWhale (pegs TOKEN/USDC to the CSV feeds),
  AnonymousArb + TokenAccumulator (propagate into TOKEN/BUCK + BUCK/USDC),
  BootstrapDM (seed pools).
- **Regime shocks**: a few agents each redraw one knob every 182 days, staggered
  so ~10 isolated perturbations land over a 5-year run and the loop re-settles.

## The experiment harness (`experiment.py`)
Declarative initial conditions + scripted mid-run interventions, from TOML
(see `experiments/template.toml` for every knob):

    python -m alberta_buck.sim --experiment experiments/foo.toml \
        --set deploy.k0=0.8 --set scenario.seed=7

- `[scenario]` window/cadence/population; `[deploy]` controller + pool-depth
  initial conditions; `[agents.<Cls>]` per-class knob-range overrides
  (`[lo,hi]` seeded draw or scalar).
- `[[interventions]]` -- day-indexed actions over four surfaces:
  *governance* (`retune`, `set_gains`, `set_rails`, `set_k0`, `set_dt`,
  `set_dtmax`), *agent knobs* (`set_knob`, `fund`), *exogenous shocks*
  (`price_shock` overlays the CSV reference so the whale re-pins pools;
  `uptake_shock` adds adoption), *population* (`add_agents`,
  `remove_agents` wind_down/hard).
- The RESOLVED config + applied-intervention log are embedded in the output
  vector (`meta`), so every vector is self-describing and reproducible.  Pin
  `start`/`end` for runs that must not drift as vendored data updates.
- **Metrics**: `python -m alberta_buck.sim.eqmetrics VEC...` -- tail stats +
  the acceptance gate (parity +/-2%, K interior/never railed, channels alive).
- **Sweeps**: `python -m alberta_buck.sim.sweep EXP... --seeds a,b --jobs N`
  runs experiments x seeds in parallel (own anvil each), then prints/writes
  the eqmetrics table.  Make targets: `nix-sim-experiment`, `nix-sim-sweep`,
  `nix-sim-metrics`.
- Frames now capture the borrower channel (`fat_limit/drawn/reserve_*,
  fat_issued/retired/released/throttled`), rendered as pane 5 of the 7-pane
  `plot_equilibrium.py` (interventions are marked on pane 1); the plot also
  prints the acceptance verdict.

## Cadence & runtime
`day_step` coarse-macro mode advances N calendar days per iteration (~12s per
10-day iteration; a 5-year run is ~183 iterations ~= 35 min).  The loop
checkpoints the vector every 25 days for kill-resilience.  `gen_historical`
caches CSVs behind a window manifest so parallel sweep children don't race.

## Results

### History: the banked lifecycle milestone (eee765b) -- corrected
The milestone banked as "basketValue settles ~0.92 with K riding ~0.93"
actually describes the mid-run plateau (~days 700-1250).  The banked vector's
END state (`test/vectors/equilibrium-lifecycle.json`) is: **basketValue 0.79,
K hard-pinned at the 1.0 rail from ~day 1400, BUCK supply frozen at 13.35M
from ~day 50** -- the late sim was open-loop.  Root cause (now understood and
fixed): the borrower's funding reserve was a cumulative ratchet
(`reserve_target` never released, each issuance a net USDC drain), so once
budgets exhausted, the throttle clamped issuance to zero *permanently* --
the controller raised K into a dead plant, and the never-released reserve
stood as permanent BUCK demand holding basketValue rich.  The startup-flood
fix and 3x volatility tightening from that milestone were real and are
retained.

### Rolling reserve + two-sided toll (this iteration), 1-year check
Same window/seed, three designs:

| design                     | bv tail (sd)      | K tail        | channel (iss/ret) |
|----------------------------|-------------------|---------------|-------------------|
| cumulative reserve (banked)| 0.917 (0.032)     | 0.91 -> rail  | frozen            |
| rolling, ff floored at 1   | 0.736 (0.012)     | pinned 0.95   | 1.6M / 1.0M       |
| rolling + two-sided ff     | **0.979 (0.009)** | **0.43 interior** | 4.3M / 1.2M   |

The remaining ~2% richness bias is the toll friction just below parity
(ff ~0.8 at bv 0.98, per the on-chain shape) against finite USDC budgets --
the issuance plant saturates slightly below setpoint.  Knobs to probe via the
harness (no code): saver `prem_gain` (sell-side parity snap), `ff_cyc`,
budgets.

### 5-year campaign (vectors in `test/vectors/sweep-*/`)
Acceptance gate: tail (last 25%) basketValue within 1.00 +/- 0.02, K never
railed in the tail, issuance/redemption channels alive.

Campaign vectors were produced with the **legacy BuckBasket**; the sim
default has since moved to **BuckBasketProRata**.  To reproduce these
tables exactly, add `--basket legacy` (or `--set scenario.basket=legacy`
through the sweep).  The named Makefile incantations are
`make nix-sim-sweep-{baseline,shocks,savers2x,capacity}` and
`make nix-sim-plot-eq-<name>` for the plots.

**Baseline x 5 seeds** (`images/equilibrium-baseline-5yr.png` = s41404):

| seed  | bv tail (sd)    | K tail | rail% | thr% | verdict |
|-------|-----------------|--------|-------|------|---------|
| 41404 | 1.0016 (0.032)  | 0.748  | 0%    | 13%  | PASS    |
| 7     | 0.9959 (0.025)  | 0.850  | 0%    | 18%  | PASS    |
| 1337  | 0.9793 (0.021)  | 0.849  | 26%   | 31%  | FAIL (dev 0.021) |
| 2025  | 0.9782 (0.086)  | 0.761  | 33%   | 49%  | FAIL (dev 0.022) |
| 99    | 0.9487 (0.028)  | 0.950  | 93%   | 4%   | FAIL    |

The canonical seed lands the design target exactly: **parity with K settled
at ~K0 (0.75), never railed, channels breathing** (1.3M issued / 3.7M
retired across the tail).  Failures share one signature -- mild richness
(0.95-0.98), never divergence -- in two mechanisms: *funding-throttled*
(1337/2025: throttle 31-49%) vs *capacity-limited* (99: throttle 4%, the
fixed 5-borrower face fully drawn with K pinned high).

**Shock suite** (canonical seed; `images/equilibrium-population-churn.png`
is the worst case):

| experiment       | bv tail (sd)    | K tail | rail% | verdict |
|------------------|-----------------|--------|-------|---------|
| retune-mid       | 1.0028 (0.029)  | 0.743  | 0%    | PASS -- bumpless tau_I 90->45d absorbed |
| shock-price      | 0.9910 (0.074)  | 0.646  | 0%    | PASS -- NRGC +30% & cbBTC +50% absorbed |
| shock-uptake     | 0.9904 (0.019)  | 0.707  | 0%    | PASS -- +0.25 wave & +2 borrowers ramped in |
| shock-demand     | 0.9953 (0.027)  | 0.859  | 2%    | FAIL (one railed frame; bv fine) |
| population-churn | 0.9746 (0.046)  | 0.901  | 41%   | FAIL -- lost borrower+saver => richness bias |

No run diverged, bang-banged, or crashed; every failure is the same bounded
sub-parity offset from a saturated issuance plant.

**Knob probes on the failing seeds** (`baseline-savers2x.toml`: saver
prem_gain [8,15] + budget_m [40,80] -- pure TOML, no code):

| seed | plain bv / K / rail    | savers2x bv / K / rail   |
|------|------------------------|--------------------------|
| 1337 | 0.9793 / 0.849 / 26%   | 0.9935 / 0.944 / 46%     |
| 2025 | 0.9782 / 0.761 / 33%   | **1.0039** / 0.734 / 15% |
| 99   | 0.9487 / 0.950 / 93%   | 0.9612 / 0.939 / 74%     |

Stronger sell-side demand pulls parity in on ALL seeds (2025 fully to 1.004
/ interior K); the residual failure mode is K's tail time at the 0.95 rail
while bv sits 1-2% rich -- the controller asking for more credit capacity
than the FIXED 5-borrower face can supply.  `baseline-capacity.toml` (adds
face_m [3,7] + fund_budget_m [5,9] on top of savers2x) confirms it on the
worst seed:

| seed 99          | bv tail (sd)   | K tail | rail% | verdict |
|------------------|----------------|--------|-------|---------|
| plain baseline   | 0.9487 (0.028) | 0.950  | 93%   | FAIL    |
| + savers2x       | 0.9612 (0.027) | 0.939  | 74%   | FAIL    |
| + capacity       | **1.0062 (0.047)** | **0.650** | **0%** | **PASS** |

So the equilibrium is reachable on every probed seed once the issuance
plant has demand+capacity headroom; what the sim lacks is the *extensive
margin* that would provide that headroom endogenously -- in reality a high
K (cheap credit) attracts NEW borrowers, while this population is closed.

## Approaches forward
- **Extensive-margin adoption** (the residual's real fix): let the borrower
  POPULATION respond to credit conditions -- new borrowers enter (or
  existing faces grow) when K is high and bvib < 1, retire when credit is
  tight.  Turns the fixed-face capacity ceiling into a supply curve; expected
  to clear the K-rail-time failures that knob probes only soften.  Fits the
  planned "richer agent simulator" (heterogeneous per-actor lifecycles).
- **(C) Direct TOKEN/BUCK stabilization channel** for savers (the borrower's
  release path already trades the observable directly): give savers a direct
  channel on the controlled pools so demand acts on basketValueInBuck without
  waiting for arbs.
- **Backstop supply-relative cap** (new credit <= X% of supply per period)
  against pathological floods -- the *inflation-regime* half of issuance
  limiting; the two-sided ff is the market-driven primary.
- **Richer inflation-regime modelling** (the harder real-world direction):
  heterogeneous per-actor credit lifecycles, non-zero-premium insurance so
  the funding factor is enforced on-chain, explicit fiat-gateway liquidity
  and BUCK/fiat pool depth at scale.

## Key files
- `src/BuckKControllerDirect.sol` / `src/BuckKControllerBase.sol` -- the PID
  (+ governance: setRails / bumpless retune / setBuckK0).
- `alberta_buck/sim/equilibrium_agents.py` -- borrower (rolling reserve,
  two-sided ff, escrow) / saver / keeper.
- `alberta_buck/sim/experiment.py` -- TOML experiments, knob draws,
  PriceOverlay, the Interventions engine.
- `alberta_buck/sim/experiments/` -- template + baseline + shock suite.
- `alberta_buck/sim/eqmetrics.py` -- tail metrics + acceptance gate.
- `alberta_buck/sim/sweep.py` -- parallel sweep runner.
- `alberta_buck/sim/scenario.py` (`build_equilibrium`) -- scenario + weights.
- `alberta_buck/sim/gen_historical.py` -- daily CSV feeds (manifest-cached).
- `alberta_buck/sim/plot_equilibrium.py` -- 7-pane diagnostic + verdict.
- Reference plots: `images/equilibrium-lifecycle.png` (pre-rework milestone),
  `images/m2-lag-correlation.png`.
