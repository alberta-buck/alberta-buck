# BuckBasket Redesign — Pro-Rata Exit + Treasury Split

Working draft. Supersedes the fused "sell-high / recycle-to-buy-low" redemption
described in `alberta-buck-ethereum.org` §BuckBasket. Once settled this folds back
into the org master and the `BUG #N` list is retired.

> **Status (as of the shell/venue-facet refactor).** `src/basket/` now splits the
> pro-rata basket along the **AMM-venue seam** (§4):
> - `BuckBasketProRata.sol` — the **venue-agnostic shell** (economic policy:
>   allocation math, treasury split, accounting, governance) at **12,275 / 24,576 B**;
> - `BuckBasketUniswapV3.sol` — the **Uniswap V3 venue facet** (pool setup,
>   liquidity in/out, price/TWAP reads, conversions, the V3 callbacks; all V3
>   tick/L/sqrt math inline) at **14,270 B**, reached by `delegatecall`;
> - `IBuckBasketVenue.sol` — the seam: `setupPool` · `provideForToken` ·
>   `withdrawLiquidity` · `investFromBucks` · `convertIntoBucks` + two value reads;
> - `BuckBasketStorage.sol` — the storage base both inherit (the `venue` facet
>   slot, the callback guards, all errors/events) so their layouts are provably
>   identical under delegatecall;
> - `BuckBasket.sol` (legacy fused impl, retained), shared adoptable
>   `BuckBasketReceipt.sol`, and `IBasketRebalancer.sol` + `BasketRebalancer.sol`
>   (the standalone governance FX-route registry, now **decoupled** from the
>   basket — for the FX follow-up). Shared controller surface `src/IBuckKController.sol`.
>
> `BasketMath.sol` is **deleted** — splitting by venue dissolved it (the V3 math
> migrated into the facet as plain inline `UniswapV3OracleLib`; `splitProfit`
> inlined into the shell). Neither contract needs library linking.
>
> The shell calls the facet via `IBuckBasketVenue(address(this)).fn(...)` — a
> self-call routed by the shell's **`fallback`** into the facet (the degenerate
> Diamond; a real Diamond just swaps the single facet for a selector→facet map).
> The pool's V3 mint/swap callbacks land on the basket address and route in the
> same way. Facet mutators are **`onlySelf`** (only the basket's own routed
> self-call may drive them); callbacks authenticate the calling pool via the
> shared `_callbackPool` / `_swapCallbackPool` guards.
>
> Tests: `BuckBasketProRata.t.sol` 20 (deploys the facet + `setVenue`; covers
> BUCK-side deposit), legacy `BuckBasket.t.sol` 18, `BasketRebalancer.t.sol` 6.
>
> Behaviour: TOKEN deposit **and BUCK-side deposit** (`investFromBucks` into the
> underweight pool); the **sell-high `redeem`** (§5.1 closed-form, deflation
> shortfall cover under `maxConversionLossBp`, default 1%, `0 = unlimited`; the two
> revert paths); the **single-TOKEN payout**; the **spot/TWAP manipulation guard**;
> **`sweepTreasury`** (recycle-to-buy-low). **The depositor is paid in TOKEN only;
> the basket keeps the entire BUCK profit** as treasury equity (§3) — so the
> 50/50 split (and `treasuryBp`) is **removed**, and commodity LPs need no BUCK
> identity. The two generic verbs `investFromBucks` / `convertIntoBucks` (§4)
> subsume treasury re-LP, BUCK deposits, and shortfall cover, and are the hook for
> FX-routed conversion. Still on the internal TOKEN/BUCK pools (FX is a
> follow-up). Still stubbed: standalone `rebalance()`, the full migration handoff.
>
> **Sim:** drives **either** basket — `--basket {legacy,prorata}` /
> `SIM_BASKET=`, with independent `make sim-rebalancing-{prorata,traditional}`
> targets. ProRata deploys the facet + `setVenue` (no library linking) and a union
> ABI lets Python reach facet views; impl-aware event parsing handles the
> differing `Deposited`/`Redeemed` shapes. Still drop-in: identical constructor,
> `addBasketToken` signature, `Constituent` field order, `redeem(id, bp, 0)`.
>
> **Next steps** (in rough order):
> 1. **A/B the runs** — compare holder-ROI smoothness, prorata vs traditional.
> 2. **FX-routed conversion** — wire `pathFor` + `ISwapRouter` *behind*
>    `convertIntoBucks` in the venue facet (needs SwapRouter test infra).
> 3. **Migration handoff** + richer `receipt.tokenURI`; eventual EIP-2535 Diamond
>    (the shell already routes through a fallback).

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
   depositor keeps the **TOKEN side** (their commodity + its price change + AMM
   fees); the **entire BUCK side** — the seigniorage the basket minted on their
   behalf — stays with the treasury (§3). The treasury funds BUCK-system R&D and
   operations, and its take is *largest exactly under inflation* — when the peg
   most needs defending (§6). A depositor never touches BUCK, so commodity LPs
   need no identity binding to participate.
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
minted.** `totalOutstandingBuck == Σ buckPrincipal + stressBonusPrincipal` over all live receipts (the second term is the stress fee's re-LP'd partner BUCK, WAVE3 WP-5, retired pro rata by every redemption); a
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

## 3. Economic model — the depositor keeps the TOKEN, the basket keeps the BUCK

A deposit of TOKEN worth `P` BUCK at spot `p0`:

- Mints `P` BUCK (`buckPrincipal = P`, the receipt's share unit) **on the
  depositor's behalf**.
- LPs `(tok0, P)` — `tok0 = P/p0` of TOKEN plus `P` of BUCK — so **2·P of
  liquidity works** while the depositor's TOKEN is on deposit. The depositor
  supplied `P` of TOKEN; the basket-minted `P` of BUCK doubles their working
  liquidity.

Who keeps what, realized at redemption:

| | Depositor | Basket / treasury |
|---|---|---|
| TOKEN side (principal + price change + AMM fees) | **100 %** | 0 % |
| BUCK profit (BUCK above principal) | 0 % | **100 %** (treasury equity) |
| BUCK principal | — | **burned** |

**The depositor is paid in TOKEN only; the BUCK half stays with the basket.** The
BUCK was minted — and is burned again at redemption — *on the depositor's behalf*:
it provided the other half of every position's liquidity, amplifying the AMM fees
and rebalancing flow the depositor's TOKEN earned. That 2×-liquidity leverage **is**
the depositor's reward: they get their commodity back with its price change plus
the fees the doubled depth captured. The BUCK half of the equation is the system's
own seigniorage and rightly stays with the basket as treasury equity. The burn
obligation is **senior**: principal is retired first from the withdrawn BUCK, then
(deflation only) from the depositor's TOKEN — the treasury takes no TOKEN, ever.

Two consequences follow:

1. **No depositor ever touches BUCK**, so commodity LPs need **no BUCK identity**
   to participate. (BUCK transfers are identity-gated — sender and recipient must
   be public or have CP-proof-approved one another; paying depositors BUCK would
   force every participant to be identity-bound. Only BUCK-*side* depositors, who
   are inherently BUCK-capable, ever handle BUCK.)
2. The treasury — the BUCK-system's R&D/ops funding, a *primary product* of the
   basket — captures the **entire** seigniorage profit, not half of it. It is
   re-LP'd (`sweepTreasury`, into the underweight pool) so it compounds as
   `treasuryLiquidity`; governance may `treasuryWithdraw` to fund operations.

State:

```solidity
uint256 public totalOutstandingBuck;          // Σ buckPrincipal (the burn obligation)
uint256 public treasuryBuckPending;           // BUCK profit retained, awaiting re-LP
mapping(uint256 pool => uint128) treasuryLiquidity;   // treasury-owned L per pool
struct Deposit { uint256 buckPrincipal; uint256 tokenPrincipal; address token; uint64 depositTime; }
```

## 4. Module layout — the shell / venue-facet split

`src/basket/`

```
BuckBasketStorage.sol     storage layout base (state, constants, events, errors); both inherit only this
BuckBasketProRata.sol     SHELL (venue-agnostic): deposit, redeem, treasury retention, accounting, governance,
                          allocation policy, the fallback router  (12.3 KB)
IBuckBasketVenue.sol      the AMM seam (setupPool/provide/withdraw/invest/convert + value reads)
BuckBasketUniswapV3.sol   VENUE FACET (Uniswap V3): pool setup, LP custody, price/TWAP, swaps,
                          V3 callbacks, all tick/L/sqrt math  (14.3 KB), reached by delegatecall
IBasketRebalancer.sol     FX-route registry interface (`pathFor`); standalone, for the FX follow-up
BasketRebalancer.sol      SEPARATE, REPLACEABLE: governance FX-route registry
BuckBasketReceipt.sol     shared adoptable ERC-721 receipt (on-chain tokenURI)
BuckBasket.sol            legacy fused impl, retained for the sim A/B
```

The shell and the facet inherit **only** `BuckBasketStorage` and add no state, so
their storage layouts are provably identical — the requirement for sharing slots
under `delegatecall`. Wiring addresses (`buck`, `v3Factory`, `venue`, …) are plain
storage (not `immutable`) precisely so the facet reads them through the shared
storage rather than its own code.

**The venue seam** (`IBuckBasketVenue`) is the abstraction that lets the same
foundational basket back onto different AMMs (Uniswap V3 today; a v4 / Balancer
facet later) — four mechanical verbs plus two value reads, all venue-neutral in
name:

| Verb | What it does | Used by |
|---|---|---|
| `setupPool` | create/init the (BUCK,token) pool, return its venue fields | `addBasketToken` |
| `provideForToken` | LP an exact TOKEN, **minting** the partner BUCK | `depositToken` |
| `withdrawLiquidity` | burn `L`, collect both sides to the basket | `redeem` |
| `investFromBucks` | from BUCK: swap-balance + LP into a pool | `sweepTreasury` (+ future BUCK deposit) |
| `convertIntoBucks` | TOKEN → BUCK, best route, with loss accounting | `redeem` shortfall (+ future FX route) |

The facet returns *what it did*; the **shell books ownership** (treasury slice vs
depositor receipt) and the BUCK ledger. So `investFromBucks` serves both treasury
re-LP and BUCK-side deposits, and `convertIntoBucks` serves both the
deflation shortfall cover and (deferred) FX-routed conversion — one primitive each.

Design rules:
- **Custody** stays at the shell. The facet is code-only (holds nothing); every
  method runs in the shell's storage context via `delegatecall`, so tokens and LP
  live at the basket address.
- **Dispatch** is a degenerate Diamond: the shell's `fallback` `delegatecall`s the
  configured `venue` facet for any selector it doesn't implement — the facet's
  methods (invoked by the shell as `IBuckBasketVenue(address(this)).fn(...)`) *and*
  the V3 mint/swap callbacks the pool fires at the basket. A real EIP-2535 Diamond
  swaps the single facet for a selector→facet map; nothing else changes.
- **Authorization**: facet *mutators* are `onlySelf` (`msg.sender == address(this)`
  — only the basket's own routed self-call), so the fallback can't expose them to
  the world; *callbacks* authenticate the calling pool via the shared
  `_callbackPool` / `_swapCallbackPool` guards.
- **The FX rebalancer** (`BasketRebalancer`) remains a separate, governance-swappable
  route *registry* (§7); it plans, the venue facet executes behind
  `convertIntoBucks`. Funds never leave the basket.

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
closed-form function of the N BUCK reserves `{buckᵢ}`, the per-pool target weights
`{wᵢ}`, and θ (no iteration, no TOKEN amounts).

**The target weights are price-scaled.** The basket is a *fixed-quantity*
commodity index — so many units of each TOKEN — and the intent is to hold *less*
BUCK value of a constituent as it appreciates (and more as it cheapens). Since
external arbs drive each pool's spot to the market price, a pool's target *value*
share is its declared weight scaled by `initialPrice/spot` and renormalised:

```
sᵢ  = weightᵢ · (initialPriceᵢ / spotᵢ)         (relative target; ∝ basketAmountᵢ·initialPriceᵢ²/spotᵢ)
wᵢ  = sᵢ / Σ sⱼ                                  (normalised target value share)
```

E.g. PAXG at 1/3 declared weight, initial 3000, spot 4000 ⇒ sᴾᴬˣᴳ ∝ ⅓·(3000/4000),
so its target share falls below ⅓ — the basket leans out of the winner and into
the laggard. At spot = initialPrice this reduces to the flat declared weights.
Then:

```
R   = buckPrincipal · redeemBp / 10000        (burn obligation, BUCK)
θ   = R / totalOutstandingBuck                 (claim fraction)
B   = Σ buckᵢ                                   NAV = 2B,   V = θ·NAV   (value claim)
aᵢ  = 2·buckᵢ − 2·wᵢ·B·(1−θ)                   (ideal sell-high draw; >0 ⇒ overweight)
aᵢ⁺ = max(0, aᵢ)                               (underweight pools draw 0)
fᵢ  = (aᵢ⁺ / Σaⱼ⁺) · V / (2·buckᵢ)             (liquidity fraction to burn in pool i)
```

`Σaᵢ = V` exactly (Σwᵢ = 1), so the positive parts always cover `V`; at equilibrium
every `aᵢ⁺` is proportional and `fᵢ → θ` falls out automatically. The most
overweight pool — measured against the *price-scaled* target — is drawn first and,
if its excess covers `V`, supplies the whole claim ("return 100 from the most
overweight pool"), driving the basket toward its fixed-quantity target mix. The
deposit/treasury "buy-low" leg (`investFromBucks` → most-underweight pool) uses the
*same* price-scaled target, so both legs balance toward one definition.

> **Manipulation guard.** `buckᵢ` is read at **spot** (keeping the elegant
> single-calculation form), but every pool entering the computation must sit
> within `defaultMaxDeviationBp` of its **TWAP** — a flash-loan sandwich that
> moves a pool's spot to inflate the claim or steer the skew reverts the redeem
> (`_poolBuckValues` → `_enforceSlippageGuard`). Rationale: value-conservation +
> fractional-liquidity withdrawal already blunt the attack to ~break-even (the
> manipulator funds their own inflation and loses the shared `(1−θ)`); the guard
> caps the residual surface to the divergence tolerance without paying to
> reconstruct value-at-TWAP from `L`. Cold pools (no TWAP history) skip the guard
> — the bootstrap window. This is cheaper than, and a close approximation of,
> valuing via the price-invariant `L` at TWAP.

Then burn `fᵢ · Lᵢ` in each pool, collect `(Tᵢ, Bkᵢ)`, `Bw = Σ Bkᵢ`, and settle:

- **`Bw ≥ R`** (the common case — overweight pools are BUCK-rich, so skewing
  toward them *over-collects* BUCK): burn `R`; the entire profit `Bw − R` accrues
  to the treasury (`treasuryBuckPending`, recycled by `sweepTreasury`) — the
  depositor is paid in TOKEN only (§3). **Never reverts.**
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
and an optional single-TOKEN payout. `payoutToken != 0` draws the entire claim
from **that token's pool only** (`f = V / depositorValue_X`; revert `token too
thin` if `f > 1`) — *no cross-pool routing*, the deposit-side twin of pledging a
specific TOKEN. The burn is covered from that one pool's BUCK side, and under
deflation by converting some of the withdrawn token X → BUCK **on the same pool**
(within-pool, bounded by `maxConversionLossBp`).

A useful identity: a full-range pool is 50/50 by value, so the BUCK side of the
withdrawal is exactly `V/2`. And direct-mint pairs TOKEN worth `P` with a fresh
`P` BUCK, so a fresh basket has `D = 2·O` — **baseline coverage is 2**.
Single-TOKEN therefore covers the burn outright (`V/2 ≥ R ⟺ coverage ≥ 2`) at par
and under inflation; only under deflation (coverage < 2) does it need the
within-pool conversion, and it falls back to balanced if that exceeds the budget.

**`maxConversionLossBp`** (default `100` = 1 %) caps the value lost to forced
TOKEN→BUCK conversion — slippage + fee, the spent TOKEN valued at its pre-swap
spot price minus the BUCK received — as a fraction of the redemption value `V`.
It protects the average caller from being silently dumped through a thin pool at
a large haircut: rather than realize a big loss, the call reverts and they wait,
switch tokens, or *explicitly* raise the budget. **`0` means unlimited** (skip
the cap), so the legacy-style `redeem(id, bp, 0)` works on both baskets.

**Sim drop-in compatibility.** `BuckBasketProRata` keeps three things aligned
with the legacy `BuckBasket` so one (web3) sim can drive either: the
`addBasketToken(token,dec,price,wbp,fee)` name/signature; the `Constituent`
struct field order (so `constituents(i)[2] == basketAmount`); and the
`redeem(id, bp, 0)` semantics above. Everything pro-rata adds (treasury,
`sweepTreasury`, single-TOKEN payout, the guard) is additive — the sim ignores it.

**The two (and only) economic revert paths** — both the same feasibility test
("source the needed BUCK within the loss budget?") applied to each mode:

1. **Balanced burn unsatisfiable within budget** — `Bw < R` and covering the
   shortfall costs more than `maxConversionLossBp · V`, or is impossible even
   converting all `ΣTᵢ`. *Underwater* (NAV < principal) is the budget-maxed
   extreme of this, not a separate path.
2. **Single-TOKEN pool can't source the claim** — `payoutToken`'s pool is
   smaller than the claim (`f > 1` ⇒ `token too thin`), or under deflation its
   within-pool conversion to cover the burn exceeds the budget.

When no conversion is needed (`Bw ≥ R`, the normal/inflation case) redemption
never reverts. Input-validation reverts (not owner, bad bp) are separate and
trivial. Passing the balanced form with a generous `maxConversionLossBp` and a
fully-overweight basket reproduces the maximally-live pro-rata exit.

## 6. Outflow effects across regimes

Single-pool, full-range ≈ CPMM (invariant `k`), deposit `P` at `p0`, price moves
to `p1`, redeem in full. LP value at `p1` is `2P·√(p1/p0)`; BUCK withdrawn is
`Bw = P·√(p1/p0)`.

Depositor is paid **TOKEN only**; the basket keeps the full BUCK profit (§3).

| Regime | `p1/p0` | `Bw` | Burn | BUCK profit | Depositor gets (TOKEN) | Treasury (BUCK) | System effect |
|---|---|---|---|---|---|---|---|
| Stable | 1.0 | `P` | `P` | ~0 (+fees) | `tok0` (+ TOKEN fees) | ~0 + fee BUCK (re-LP) | supply-neutral over cycle |
| Mild inflation | 1.21 | `1.1P` | `P` | `0.1P` | `0.91·tok0` | `0.1P` (re-LP) | burns `P`; exit incentive ⇒ **contracts supply** |
| Strong inflation | 4.0 | `2P` | `P` | `P` | `0.5·tok0` | `P` (re-LP) | max treasury revenue *when peg most stressed* |
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

## 7. Rebalancer sub-contract — a route provider

The N **TOKEN/BUCK** V3 pools are the owned foundation — *sufficient* for
solvency but possibly thin. The replaceable `BasketRebalancer` is **not** the
market weight-balancer (that is external arbs + the basket's own sell-high redeem
and buy-low `sweepTreasury`); it is the **BUCK↔TOKEN route provider** for the
basket's own conversions, so they can use deep external FX pools instead of the
thin internal one:

```
BUCK/USDC, BUCK/USDT            (BUCK <-> stable)
USDC/<TOKEN>, USDT/<TOKEN>      (stable <-> commodity, deep external pools)
```

The whole interface is one function — a **governance-curated route registry**:

```solidity
interface IBasketRebalancer {
    // V3-encoded path tokenIn -> ... -> tokenOut, or empty bytes => internal pool.
    function pathFor(address tokenIn, address tokenOut) external view returns (bytes memory);
}
```

Routing is curated off-chain (which path is cheapest) and stored per ordered
`(tokenIn, tokenOut)` pair — the registry serves it, it does **not** search
(`ISwapRouter` only executes a given path; we don't reinvent a router). Division
of labour: the **rebalancer** returns the path (no funds, no amounts, no
execution); the **basket** holds the amount + exact-in/out and executes via
`ISwapRouter.exactInput`/`exactOutput`, falling back to its internal `pool.swap`
when the path is empty or no rebalancer/router is set — so it never bricks.

`setRebalancer(addr)` (governance) swaps the whole strategy — the pre-Diamond
replaceability path. The basket-side execution (consult `pathFor`, run it through
`ISwapRouter` for `_coverShortfall` / `_reinvestTreasury` / a future
`rebalance()`, else internal) is the next increment; it needs `ISwapRouter` test
infrastructure (no `SwapRouter` artifact is built today). The single-TOKEN
*within-pool* conversion deliberately stays internal.

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

Bootstrap is the inverse: `addBasketToken` then seed deposits fill each pool;
`minSeedLiquidity` floor unchanged; a `seeded(i)` view exposes per-pool readiness
for the UI.

## 10. Keep vs. replace

| Keep | Replace / delete |
|---|---|
| `Constituent` registry + `addBasketToken` (name/sig kept for sim compat) | fused `redeem` 9-phase pipeline |
| `totalOutstandingBuck`, receipts, deposits | `_allocateRedemption` 2-pass allocator → closed-form BUCK-balance allocation (§5.1) |
| treasury (keeps 100% of BUCK profit) + treasury re-LP | hand-rolled swap path: `uniswapV3SwapCallback`, `_swapTokenForBuckExactIn`, `_tokenInForBuckOut`, `_coverShortfallAggregate` |
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
3. ~~`treasuryBp` split~~ — decided: **removed**.  The depositor is paid in TOKEN
   only; the basket retains 100% of the BUCK profit (§3).  No depositor BUCK
   payout ⇒ commodity LPs need no BUCK identity.
4. ~~FX route representation~~ — decided: encoded `bytes` path (router-native),
   served by the `BasketRebalancer` registry keyed on `(tokenIn, tokenOut)`.
5. Migration LP handoff: withdraw-all-and-re-LP (clean cut, recommended) vs.
   incremental.
6. Underwater threshold telemetry: emit a `BasketUnderwater` signal for the K
   controller / keepers even though redeem reverts?
