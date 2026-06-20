# BuckBasket Redesign — Pro-Rata Exit + Treasury Split

Working draft. Supersedes the fused "sell-high / recycle-to-buy-low" redemption
described in `alberta-buck-ethereum.org` §BuckBasket. Once settled this folds back
into the org master and the `BUG #N` list is retired.

> **Scaffold status.** The module lives in `src/basket/`:
> `BuckBasketProRata.sol` (the new pro-rata core), `BuckBasket.sol` (the legacy
> fused implementation, moved here and retained), shared adoptable
> `BuckBasketReceipt.sol` (used by *both*, now with on-chain `tokenURI`),
> `BasketMath.sol`, `IBasketRebalancer.sol` + stub `BasketRebalancer.sol`. The
> shared controller surface is `src/IBuckKController.sol`. Tests:
> `test/basket/BuckBasketProRata.t.sol` (12/12) and the retained
> `test/basket/BuckBasket.t.sol` (18/18) — 30/30.
>
> `BuckBasketProRata` has TOKEN deposit, the **sell-high `redeem`** (§5.1
> closed-form overweight-first allocation degenerating to pro-rata; treasury
> split; deflation shortfall cover under a `maxConversionLossBp` budget, default
> 1%; the two revert paths), and **`sweepTreasury`** (recycle-to-buy-low). Both
> the shortfall cover and the treasury re-LP swap on the **internal** TOKEN/BUCK
> pools for now — FX multi-hop routing via the rebalancer + `ISwapRouter` is a
> separate pass, as is TWAP-hardening the allocation's value read (it currently
> uses spot BUCK reserves). Still stubbed: the **single-TOKEN payout mode**,
> BUCK-side deposits, standalone `rebalance()`, and the full migration handoff.
> The underwater check carries a `MAX_DUST_WEI = 1e9` tolerance for V3
> burn-rounding on a fully-drained pool.

## 1. Goals

1. **Thin-funding-proof redemption.** Burning a receipt's share always succeeds
   regardless of how thin any individual pool is. Redemption allocates a position's
   value claim (in BUCK, §2.1) across pools; because the allocation always sums to
   the claim and **conserves value**, the coverage ratio is preserved and the tail
   stays solvent (§5.2). The *only* revert is the transient **deep-deflation
   underwater** case (whole-basket NAV < principal), quenched by the
   BuckCredit/`BuckKController` backstop (§6). *Thin ≠ underwater.* Pure pro-rata
   is always available as the maximally-live floor.
2. **Redemption rebalances (sell-high); the treasury recycles (buy-low).** The
   default redemption draws from the *most overweight* pools first, nudging the
   basket toward target weights, and degenerates to pure pro-rata at equilibrium.
   `sweepTreasury` re-LPs profit into the *most underweight* pools. Together with
   external arbs (and a future standalone `rebalance()`), these keep weights from
   drifting — value-conservation (§5.2) makes the sell-high reliable.
3. **Treasury is a primary product.** Direct-mint pairs the depositor's TOKEN
   with freshly-minted BUCK, so the deposit puts ~2× the liquidity to work. The
   yield is split (default 50/50, governance-adjustable): the depositor keeps the
   TOKEN side, the treasury keeps the BUCK side. The treasury funds BUCK-system
   R&D and operations, and its take is *largest exactly under inflation* — when
   the peg most needs defending (§6).
4. **Clean establishment / bootstrap / unwind.** Governance can seed, add,
   re-weight, remove constituents, and **migrate to a successor basket** with all
   LP, treasury equity, and outstanding-BUCK accounting carried over. Receipts
   survive migration.
5. **Simple and modular.** Lean on Uniswap's deployed `ISwapRouter` for swaps and
   multi-hop FX routing instead of hand-rolled `pool.swap()` + callbacks. Core
   holds funds + state + the solvent primitives; the rebalancer/router
   intelligence lives in a replaceable sub-contract (pre-Diamond shape).
6. **Optimizer-friendly.** A naive on-chain default that always works (balanced
   sell-high), plus a symmetric single-TOKEN API — deposit one TOKEN, redeem into
   one TOKEN — so a caller who optimizes off-chain picks the tokens, and a
   `maxConversionLossBp` knob bounds the loss they'll accept.

## 2. The invariant and the unit of account

The hard constraint: **the basket must eventually burn exactly the BUCK it
minted.** `totalOutstandingBuck == Σ buckPrincipal` over all live receipts; a
redemption burns its share of that total and never more.

NAV above outstanding is **treasury equity** — accumulated AMM fees + retained
BUCK profit + unclaimed external-arb BUCK. It is tracked, not commingled into
depositor claims, via a per-pool `treasuryLiquidity` counter (§3). Depositor
pro-rata claims span only `depositorLiquidity = totalLiquidity − treasuryLiquidity`.

### 2.1 Everything is measured in BUCK value, never TOKEN amounts

The basket holds N heterogeneous constituents — different ERC-20 decimals
(cbBTC 8, PAXG 18, …) and unit prices spanning orders of magnitude (1 cbBTC ≈
65 000 BUCK vs 1 PAXG ≈ 2 600 BUCK). **Raw TOKEN amounts are incommensurable** —
you cannot add, compare, or weight them against one another. So *every decision*
in the basket is computed in **BUCK value**, the system's native unit of account
(and exactly the controller's process variable, `basketValueInBuck`). The basket
needs no external USD oracle: BUCK is the numéraire.

Computed in BUCK value:

| Quantity | Definition (BUCK, 18-dec) |
|---|---|
| Pool value | `tokenReserve · priceInBuck + buckReserve` |
| Actual weight | pool value / total basket value |
| Target value | `basketAmount · priceInBuck` (the pool's share of 1.0 BUCK) |
| Over/under-weight | actual value vs. target value |
| Position claim fraction | `θ = buckPrincipal / totalOutstandingBuck` (= share of depositor NAV *by value*) |
| Redemption value claim | `θ · NAV` |
| Per-pool redemption allocation | distributed by each pool's BUCK-value excess over target (§5) |

TOKEN amounts appear **only as physical quantities, never as a basis for
comparison**: the raw reserves custodied (native decimals), the V3 *liquidity*
`L` used to execute a withdrawal (itself neither BUCK nor TOKEN), and the raw
TOKEN the redeemer receives.

**The value ↔ liquidity bridge.** A decision yields a BUCK-value target per pool;
execution converts it to a liquidity fraction `fᵢ = valueToWithdrawᵢ /
poolDepositorValueᵢ`, burns `fᵢ · Lᵢ`, and pays out the resulting raw TOKEN +
BUCK. Pure pro-rata is the special case `fᵢ = θ` for all pools; a skewed
(sell-high) allocation uses different `fᵢ` but conserves total value (§5).

**Price source.** `priceInBuck` is the V3 pool's own quote — the TOKEN→BUCK
conversion factor — read as **TWAP for decisions** (weights, claim sizing;
manipulation-resistant) and spot for the final amount arithmetic.

## 3. Economic model

A deposit of TOKEN worth `P` BUCK at spot `p0`:

- Mints `P` BUCK (`buckPrincipal = P`, the receipt's share unit).
- LPs `(tok0, P)` — `tok0 = P/p0` of TOKEN plus `P` of BUCK — so **2·P of
  liquidity works** while the depositor's TOKEN is on deposit. The depositor
  supplied `P`; the basket-minted `P` doubles their working liquidity. Splitting
  the yield is just.

Who keeps what, realized at redemption:

| | Depositor | Treasury |
|---|---|---|
| TOKEN side (principal + AMM fees) | **100 %** | 0 % |
| BUCK profit (BUCK above principal) | `1 − treasuryBp` (default 50 %) | `treasuryBp` (default 50 %) |
| BUCK principal | — | **burned** |

The burn obligation is **senior** to the split: principal is retired first from
the withdrawn BUCK, then (deflation only) from the depositor's TOKEN. The treasury
takes no TOKEN ever; under deflation it simply earns no BUCK profit that round.
Treasury BUCK profit is **re-LP'd** (via the rebalancer, into the underweight
pool) so it compounds as `treasuryLiquidity`; governance may `treasuryWithdraw`
to fund operations.

State:

```solidity
uint256 public totalOutstandingBuck;          // Σ buckPrincipal
uint256 public treasuryBp = 5000;             // governance-adjustable split
mapping(uint256 pool => uint128) treasuryLiquidity;   // treasury-owned L per pool
struct Deposit { uint256 buckPrincipal; uint256 tokenPrincipal; uint64 depositTime; }
```

## 4. Module layout

`src/basket/`

```
BuckBasket.sol          core: state, deposit, redeem, governance, LP custody, swap execution
BasketMath.sol          library (pure): shares, weight errors, sqrtPrice/L helpers
IBasketRebalancer.sol   strategy interface (plan structs)
BasketRebalancer.sol    SEPARATE, REPLACEABLE: FX-route registry + routing/rebalance intelligence
BuckBasketReceipt.sol   ~unchanged; `basket` made adoptable for migration
```

Design rules:
- **Swaps** (TOKEN↔BUCK, multi-hop FX) execute through Uniswap **`ISwapRouter`**
  (v3-periphery, already vendored). The hand-rolled `uniswapV3SwapCallback`,
  `_swapTokenForBuckExactIn`, `_tokenInForBuckOut`, recursive
  `_coverShortfallAggregate` are **deleted**.
- **LP custody** stays raw-pool `mint`/`burn`/`collect` with the existing
  `uniswapV3MintCallback` (well-tested; avoids NonfungiblePositionManager gas).
  Only retained callback.
- **The rebalancer is a separate contract**, replaceable by governance
  (`setRebalancer`). It *plans* (the FX-routing intelligence); the **basket
  executes**, so funds never leave the basket's control. This is the safe
  pre-Diamond shape — it migrates cleanly to a Diamond facet later (open
  decision §12: advisor vs. privileged executor).

## 5. Redemption

```
redeem(receiptId, redeemBp, maxConversionLossBp)                 // balanced
redeem(receiptId, redeemBp, payoutToken, maxConversionLossBp)    // single TOKEN
```

The balanced form is the only allocation; the single-TOKEN form consolidates the
whole claim into one TOKEN (reverting if infeasible). This mirrors deposit (one
TOKEN in) ⇄ redeem (optional one TOKEN out) for callers who optimize off-chain;
everyone else gets the computed sell-high mix.

### 5.1 Balanced allocation — a single closed-form over the BUCK balances

Because every position is full-range, **pool value (BUCK) = 2 · buckReserve** — the
TOKEN reserve and price drop out. So the entire sell-high allocation is a
closed-form function of the N BUCK reserves `{buckᵢ}`, the declared weights
`{wᵢ}`, and θ (no iteration, no TOKEN amounts):

```
R   = buckPrincipal · redeemBp / 10000        (burn obligation, BUCK)
θ   = R / totalOutstandingBuck                 (claim fraction)
B   = Σ buckᵢ                                   NAV = 2B,   V = θ·NAV   (value claim)
aᵢ  = 2·buckᵢ − 2·wᵢ·B·(1−θ)                   (ideal sell-high draw; >0 ⇒ overweight)
aᵢ⁺ = max(0, aᵢ)                               (underweight pools draw 0)
fᵢ  = (aᵢ⁺ / Σaⱼ⁺) · V / (2·buckᵢ)             (liquidity fraction to burn in pool i)
```

`Σaᵢ = V` exactly, so the positive parts always cover `V`; at equilibrium every
`aᵢ⁺` is proportional and `fᵢ → θ` (pure pro-rata) falls out automatically. The
most overweight pool is drawn first and, if its excess covers `V`, supplies the
whole claim ("return 100 from the most overweight pool") — driving it to target.

> **Manipulation note.** `buckᵢ` is read at the **TWAP** price (spot BUCK reserve
> moves with price), keeping the single-calculation form sandwich-resistant.

Then burn `fᵢ · Lᵢ` in each pool, collect `(Tᵢ, Bkᵢ)`, `Bw = Σ Bkᵢ`, and settle:

- **`Bw ≥ R`** (the common case — overweight pools are BUCK-rich, so skewing
  toward them *over-collects* BUCK): burn `R`; split profit `Bw − R` (depositor
  `1−treasuryBp`, treasury `treasuryBp` → `treasuryBuckPending`, recycled by
  `sweepTreasury`). **Never reverts.**
- **`Bw < R`** (deflation): cover the shortfall `S = R − Bw` by converting the
  **minimum** withdrawn `Tᵢ` → BUCK via the rebalancer (internal pool now, FX
  later). If the conversion loss (slippage + fee, at TWAP) would exceed
  `maxConversionLossBp · V` — or `S` is unreachable even converting all `ΣTᵢ`
  (the underwater extreme) — **revert** (revert path 1, §5.3). Otherwise burn `R`.

Finally pay the depositor the surviving `Tᵢ`; decrement `buckPrincipal` and
`totalOutstandingBuck` by `R`; burn the receipt on full redemption.

### 5.2 Why a skewed (non-pro-rata) withdrawal stays reliable

The skew only changes *which* pools supply the claim, never its total: the
closed-form allocation (§5.1) sums to exactly `V = θ · NAV`. That
**value-conservation preserves the coverage ratio** for everyone left behind:

```
D' = D − θ·D,  O' = O − θ·O   ⇒   D'/O' = D/O   (D = depositor NAV, O = outstanding)
```

So a sell-high redemption cannot push the remaining (including the *last*) holders
underwater — if the basket was solvent before, it is solvent after, regardless of
which pools were drawn down. Two reliability axes, and their guards:

| Axis | Risk under skew | Guard |
|---|---|---|
| Value / solvency | over-claim value ⇒ dilute others | `Σ allocᵢ = θ·NAV`, valued at **TWAP** |
| Physical liveness | pool too thin to supply its allocation / conversion | per-pool cap `fᵢ ≤ 1`; spill + proportional remainder always reaches `V`; burn covered or `underwater` revert |

Sell-high is in fact **burn-positive**: overweight pools are the BUCK-rich ones,
so skewing toward them collects *more* BUCK than pro-rata and needs *less*
TOKEN→BUCK conversion — strictly easier on the solvency-critical resource. The one
cost is **composition drift**: serial sell-high redemptions leave later holders
holding relatively more of the underweight pools. They stay solvent (coverage
preserved); the thinned pools are replenished by `sweepTreasury` (buy-low),
external arbs, and a future `rebalance()`. Sell-high redemption and buy-low
treasury are the two halves of one balancing loop.

### 5.3 Single-TOKEN mode, the loss budget, and the two revert paths

There is **no arbitrary per-pool override** — only the balanced allocation (§5.1)
and an optional single-TOKEN payout. `payoutToken != 0` consolidates the entire
claim into that one TOKEN (converting the other pools' shares to it), the
deposit-side twin of pledging a specific TOKEN; it reverts if infeasible within
the loss budget.

**`maxConversionLossBp`** (default `100` = 1 %) caps the value lost to forced
TOKEN→BUCK conversion — slippage + fee, measured at TWAP — as a fraction of the
redemption value `V`. It protects the average caller from being silently dumped
through a thin pool at a large haircut: rather than realize a big loss, the call
reverts and they wait, switch tokens, or *explicitly* raise the budget.

**The two (and only) economic revert paths** — both the same feasibility test
("source the needed BUCK within the loss budget?") applied to each mode:

1. **Balanced burn unsatisfiable within budget** — `Bw < R` and covering the
   shortfall costs more than `maxConversionLossBp · V`, or is impossible even
   converting all `ΣTᵢ`. *Underwater* (NAV < principal) is the budget-maxed
   extreme of this, not a separate path.
2. **Single-TOKEN target unsatisfiable within budget** — consolidating into
   `payoutToken` (plus covering the burn) exceeds the budget or is impossible.

When no conversion is needed (`Bw ≥ R`, the normal/inflation case) redemption
never reverts. Input-validation reverts (not owner, bad bp) are separate and
trivial. Passing the balanced form with a generous `maxConversionLossBp` and a
fully-overweight basket reproduces the maximally-live pro-rata exit.

## 6. Outflow effects across regimes

Single-pool, full-range ≈ CPMM (invariant `k`), deposit `P` at `p0`, price moves
to `p1`, redeem in full. LP value at `p1` is `2P·√(p1/p0)`; BUCK withdrawn is
`Bw = P·√(p1/p0)`.

| Regime | `p1/p0` | `Bw` | Burn | BUCK profit | Depositor gets | Treasury | System effect |
|---|---|---|---|---|---|---|---|
| Stable | 1.0 | `P` | `P` | ~0 (+fees) | `tok0` + fee/2 BUCK | fee/2 (re-LP) | supply-neutral over cycle |
| Mild inflation | 1.21 | `1.1P` | `P` | `0.1P` | `0.91·tok0` + `0.05P` BUCK | `0.05P` (re-LP) | burns `P`; exit incentive ⇒ **contracts supply** |
| Strong inflation | 4.0 | `2P` | `P` | `P` | `0.5·tok0` + `0.5P` BUCK | `0.5P` (re-LP) | max treasury revenue *when peg most stressed* |
| Mild deflation | 0.81 | `0.9P` | `P` | none (cover) | ~`0.99·tok0` (sold 0.1P worth) | 0 | exit costly (value down) ⇒ **holds, supply stays high** |
| Strong deflation | 0.25 | `0.5P` | `P` | none | ~0 TOKEN left (boundary) | 0 | exit maximally costly ⇒ holds |
| Extreme (`p1 < p0/4`) | <0.25 | <`0.5P` | `P` | — | **revert** | — | forced hold; BuckCredit floods in |

Reading the dynamics:

- **Inflation** (BUCK cheap vs commodities; pools BUCK-heavy): the depositor's
  TOKEN balance has shrunk (impermanent loss) but the BUCK side ballooned, so the
  fixed principal burn is a *smaller* fraction of their position — **incentive to
  withdraw**. Each exit burns `P` ⇒ BUCK supply contracts, reinforcing the
  `BuckKController` lowering K (fewer BuckCredit mints). Treasury revenue peaks
  here, funding the defense of the peg precisely when needed. Stabilizing.
- **Deflation** (BUCK dear; pools BUCK-drained): the BUCK side is below principal,
  so exiting means selling appreciated-claim TOKEN to cover the burn and getting
  *less* TOKEN back — **disincentive to withdraw**. Holders keep `P` outstanding,
  so BUCK liquidity stays high (anti-deflationary, which is what we want). The
  extreme `>4×` BUCK appreciation reverts redemption entirely — but that regime
  is self-quenching: minting asset-backed BUCK via BuckCredit is trivial and most
  profitable exactly when BUCK is this dear, so the gap closes fast.

Net: the split + pro-rata-burn design makes withdrawal pressure *pro-cyclical
with the system's own corrective need* — people pull BUCK out (burn) under
inflation and leave it in under deflation.

## 7. Rebalancer sub-contract

The N **TOKEN/BUCK** V3 pools are the owned foundation — *sufficient* for
solvency but possibly thin. The replaceable `BasketRebalancer` additionally knows
about **FX pools** it never custodies:

```
BUCK/USDC, BUCK/USDT            (BUCK <-> stable)
USDC/<TOKEN>, USDT/<TOKEN>      (stable <-> commodity, deep external pools)
```

It owns the route registry and the planning logic:

```solidity
interface IBasketRebalancer {
    function planSellTokenForBuck(address token, uint256 buckOut, uint256 maxTokenIn)
        external view returns (SwapStep[] memory steps);          // redeem shortfall
    function planTreasuryReinvest(uint256 buckAmount)
        external view returns (SwapStep[] memory steps, uint256 poolIdx);
    function planRebalance(PoolState[] calldata pools)
        external view returns (SwapStep[] memory steps);          // constant-mix
}
```

`SwapStep` is an `ISwapRouter` exact-in/out call (single or encoded multi-hop
path). The basket executes each step (`approve` + `exactOutput`/`exactInput`),
keeping intermediates (USDC/USDT) from ever resting in the basket. FX routes are
pure optimization: absent/disabled ⇒ fall back to the internal pool ⇒ ultimately
the §5.1 underwater check.

```
rebalance(uint256 maxNotionalBuck)   // permissionless keeper entry on the basket
```
asks `planRebalance`, executes bounded moves from overweight → underweight pools.
Not on the redeem hot path. Governance `setRebalancer(addr)` swaps the whole
strategy — the pre-Diamond replaceability the user wants.

## 8. Deposits

```
deposit(token, amount, DepositPlan plan)
```
- `token == BUCK`: rebalancer routes it into the underweight pool's TOKEN; mint
  partner BUCK; LP. (Constant-mix injection.)
- `token == constituent`: mint BUCK at spot, LP into its own pool by default;
  `plan` may request underweight-routing (old `BUG #9` asymmetry → opt-in).
- `plan.token` lets a client pledge a *specific* commodity — deposit-side twin of
  `payoutToken`.

## 9. Lifecycle, migration, unwind

Current blockers: `Buck.setBasket` is one-shot immutable, and there is no LP exit
or treasury withdrawal, so the basket can never be replaced. Fixes:

1. **`Buck`**: replace one-shot `setBasket` with governance-gated
   `migrateBasket(newBasket)` (callable only by `insurancePool`), moving the
   `mintFromBasket`/`burnFromBasket` authority.
2. **`BuckBasket.migrateTo(newBasket)`** (governance): withdraw **all** liquidity
   (depositor + treasury) from every pool; transfer all TOKEN + BUCK to
   `newBasket`; `newBasket.adoptMigration(constituents, totalOutstandingBuck,
   treasuryLiquidity, receipt)`; re-point Buck. Receipts redeem against the new
   basket unchanged.
3. **`BuckBasketReceipt`**: make `basket` adoptable (one-shot handoff) instead of
   constructor-immutable.
4. **`removeConstituent` / `rebalanceWeights`** (the commented-out governance
   fns): recompute `basketAmount` at current spot; weight-0 removes a token after
   pro-rata-withdrawing its pool into the others.
5. **`treasuryWithdraw(amount, to)`** (governance): the R&D/ops funding tap on
   accumulated treasury equity.

Bootstrap is the inverse: `addConstituent` then seed deposits fill each pool;
`minSeedLiquidity` floor unchanged; a `seeded(i)` view exposes per-pool readiness
for the UI.

## 10. Keep vs. replace

| Keep | Replace / delete |
|---|---|
| `Constituent` registry, `addBasketToken` → `addConstituent` | fused `redeem` 9-phase pipeline |
| `totalOutstandingBuck`, receipts, deposits | `_allocateRedemption` 2-pass allocator → closed-form BUCK-balance allocation (§5.1) |
| treasury + adjustable split + treasury re-LP | hand-rolled swap path: `uniswapV3SwapCallback`, `_swapTokenForBuckExactIn`, `_tokenInForBuckOut`, `_coverShortfallAggregate` |
| `uniswapV3MintCallback`, raw-pool LP custody | `MAX_ORPHAN_DUST_WEI` dust orphaning (replaced by clean underwater revert) |
| `basketValueInBuck()` PV for the controller | in-redeem "buy low" recycling (split is now realize-on-redeem) |
| `_readPoolPrice` TWAP/spot, slippage guard, tick/L math → `BasketMath` | one-shot `Buck.setBasket` immutability |

## 11. Test & sim implications

- **Solidity**: keep the 18 passing tests as regression; add (a) deep-deflation
  redeem on a BUCK-drained pool — no revert, correct burn + TOKEN sold;
  (b) underwater revert at `p1 < p0/4`; (c) thin-pool pro-rata across a
  near-empty constituent; (d) FX-route shortfall cover (mock external pool);
  (e) profit-split accounting (depositor TOKEN + half BUCK; treasury half re-LP'd);
  (f) migration round-trip (receipts + treasury + outstanding preserved);
  (g) single-TOKEN `payoutToken` exit + `maxConversionLossBp` revert paths;
  (h) `rebalance` toward target weights; (i) `setRebalancer` swap.
- **Sim** (`basket_model.py`, `basket_flow.py`): pro-rata withdrawal + shortfall
  conversion + treasury split/re-LP; add the FX-pool layer and a `rebalance`
  keeper; emit the per-regime outflow series the §6 table predicts. This is the
  model the ncurses UI visualizes (pools, FX routes, per-receipt share,
  treasury equity, outstanding vs NAV).

## 12. Open decisions

1. Rebalancer access: **advisor** (plans; basket executes — recommended, funds
   stay in basket) vs. privileged **executor** (more Diamond-like, more surface).
2. Router: `ISwapRouter` (recommended, vendored) vs. Universal Router + Permit2.
3. `treasuryBp` default 5000 and bounds (cap to prevent governance over-extraction?).
4. FX route representation: encoded `bytes` path (router-native, recommended) vs.
   structured hops.
5. Migration LP handoff: withdraw-all-and-re-LP (clean cut, recommended) vs.
   incremental.
6. Underwater threshold telemetry: emit a `BasketUnderwater` signal for the K
   controller / keepers even though redeem reverts?
