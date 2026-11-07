# alberta-buck-core

The JavaScript side of the Alberta Buck platform: one testing API over every
EVM backend, an agent simulation platform that runs in a browser, and
BigInt-native wrappers around the WebAssembly kernels.

- **`Session`** -- the ChainSession API. Send, call and deploy against Tevm
  in-process, anvil, or a public testnet, with the same code. Every
  state-changing operation declares whether it is expected to succeed or
  revert, so expectation mismatches are first-class results rather than
  surprises.
- **Journal** -- an append-only JSONL record of every operation: tag, op,
  sender, expectation, outcome, gas, tx hash, block, error. Journals make
  runs auditable, diffable across backends *and across languages* (the
  Python platform writes the same schema), and replayable for demos.
- **Agents and worlds** -- `runDays`, whales, round-trip traders, and the
  scenario builders that stand up a pool world.
- **A BUCK world** -- `buckworld.js` deploys the identity and monetary
  stack and runs its ceremonies, whole (`onboard`, `createCredit`) or in
  the halves each party performs: `issueCredential` (the issuer, off-chain)
  and `registerWallet` (the holder); `insureAsset` (the insurer) and
  `activateCredit` (the holder). `observer.js` decodes the chain as the
  public sees it, identity material marked opaque. `snapshotTevm` /
  `restoreTevm` and `codec.js` save a Tevm world as JSON and bring it back,
  clock included.
- **Kernel wrappers** -- `identity.js` and `wallet.js` wrap
  [`alberta-buck-kernel`](https://www.npmjs.com/package/alberta-buck-kernel)
  in a BigInt-native API; `identity-web.js` and `wallet-web.js` do the same
  for the browser, taking an explicit wasm source because bundlers break
  wasm-bindgen's default relative fetch.

## Install

```sh
npm install alberta-buck-core
```

Needs npm 11 or later (Node 24 bundles it): npm 10's installer crashes on
this package's dependency graph.

## Use

```js
import { tevmSession, runDays } from "alberta-buck-core";

const session = await tevmSession();          // or anvilSession(...)
// ... deploy contracts, then let agents run simulated days
```

```js
import identity from "alberta-buck-core/identity";

const scalar = identity.reduceModOrder(12345n);
```

Canonical cash math uses `createNetMath` and the initialized math kernel:

```js
import { createRequire } from "node:module";
import { createNetMath } from "alberta-buck-core/net-math";
const math = createNetMath(createRequire(import.meta.url)("alberta-buck-kernel/math"));
const cash = 10n ** 42n;      // one BUCK in internal cash units
const remaining = math.decay(cash, 31_557_600n);
```

For a browser, initialize `alberta-buck-kernel/web/math` with its explicit
WASM source, then pass that module to `createNetMath`. Inputs/results are
uint256 `BigInt`; full-width products are computed in Rust. `fundNetAfter`
uses the same units as cash. `targetRelief(principal, buckSeconds, reduction)`
uses raw NFT principal and raw BUCK-seconds and returns a baseline, not an
executable relief quote. `mulDiv(a,b,denominator,roundUp=false)` implements
floor/ceiling division. Missing functions, invalid units, division by zero
and overflowing results fail explicitly. Older `fee_owing` /
`carrying_transfer` kernel functions retain the legacy linear model.
Run `make nix-venv-core-net-math-test` in a checkout to compare all bindings
and actual Solidity, including the browser build in Chromium.

The existing `scenarios/eqworld` machine builder now bootstraps that corrected
stack, reusing its one linked basket and Jubilee fund. The demo artifact
bundle includes production BuckCredit; each funding/household proxy opts
into its actual insurer. `make nix-core-equilibrium-bootstrap-test` checks
the real opening pools, wheel/fund roles and a premium-bearing issuance.
The existing debtor, saver and investor setup now creates ordinary proxies
and distinct, inactive facilities on that graph. The household decision
lifecycle remains gated and full economy snapshots
are rejected until their machine and pending-outcome state is migrated.
This bootstrap check is not a five-year economy result.

`world.netCreditOps.markHousehold(owner, tokenIds)` returns a same-block
`state` with cash and NFT principal separately, `creditLimit`,
`premiumNetScaled`, `reliefScaled`, `availableReliefScaled` and
`exceedsLiquidityLimit`. Scaled fields use `1e36` per raw six-decimal BUCK.
Each nominal premium uses its own facility's actual insurer BUCK-pool
ratio; unobserved strategies and future funding are excluded. Marked
relief is distinct from the shared liquidity limit. Required guarded-mark
or maturity failures propagate. `{blockNumber}` selects a historical
snapshot; duplicate/foreign facilities and Carrying wallets are rejected.
`markNetBasketPositions` separately reads exact receipt `valueOf` and
remaining basis before exit costs, avoiding a second rounding of
`sharePrice`. Python exposes the matching `mark_household` and receipt
reader. `make nix-venv-economy-accounting-test` covers both role setups,
actual insurer-pool changes, receipt marks and the short JSON/PDF path.

An initialized household's `record()` now reads its complete personal book:
all ordinary custody accounts' USDC, net BUCK and constituent TOKENs, exact
receipt marks/basis, insurer refunds and guarded Jubilee claims, less actual
principal, external mortgage and arrears. Amounts stay `BigInt` through the
signed USD division; the mark precedes exit costs. Records retain their
`markBlock`, auxiliary cash and relief-liquidity status. Python captures the
matching book daily. Missing required marks refuse the record, and reading
it does not execute a mortgage payment or update the trade book. Normal
household decisions and complete saved-world restoration remain gated.

The primary economy's `netMarket` reuses the wallet's guarded buy and
bounded user-paid sale helpers, with real Permit2 and an explicit `execute`
dispatcher for its public simulation proxies. `openTrading` uses BUCK's
public-identity fallback for those accounts; private holders still need the
normal identity proof. `buyBuck`, `buyBuckExact` and `sellBuck` accept an
optional `recipient`; receipt authentication checks that recipient separately
from the payer. EOA callers retain the ordinary session dispatcher. These
dispatchers are not serialized wallet authority. `netSettlementOps` verifies
exact USDC payments separately; a confirmed sale never reduces either
external mortgage or NFT principal by itself. The existing partial repayment
adapter supplies the primary household's receipt recovery and selected-NFT
operations; normal issuance/exit scheduling remains gated.

`agent.startNetRefinance(world, plan, day)` now executes an assessed plan
through an optional net-reserve purchase followed by selected-facility issue,
bounded export, exact USDC bank payment. That final payment combines mortgage
principal, switch cost and a rational prepayment penalty; the agent records
the allocations separately. Only confirmed principal reduces its mortgage.
Lost replies preserve the phase/terms/hash and are reconciled before another
action; an unknown or mismatched receipt cannot trigger a second send.
`reserve_buy_net` and `reserve_buy_max_usdc` are optional BigInt bounds;
both must be positive together. The purchase retains the cash buffer, and
issuance rechecks the original terms after funding. An unavailable view
waits without buying twice; changed economics refuse while retaining actual
holdings. Normal decision scheduling and the complete calendar/exit lifecycle
remain gated.

`agent.assessNetRefinance(world, day, forecasts)` now reads a bounded
per-facility menu and returns `{plan, verdict, context}` without transactions.
Supply explicit `expected_relief_rate`, `expected_refund_growth` and
`expected_wallet_growth`. It compares wallet/new/mixed funding, actual
insurer refund values, incremental insurance savings and reserve costs;
raw budgets retain utilization, market-impact and cash limits. Exact
activated room is included even below the usual size grid. An ordinary
reserve's age does not create a new premium-buffer requirement for a
zero-premium issue. Pass a selected plan and context to `startNetRefinance`.

Canonical calendar insurance uses only the uncovered nominal asset share;
`externalPremiumDue` rounds the remaining raw cost up. `refinanceQuoted`
accepts `externalPremiumSaving` (annual USD, default zero) for each candidate's
new coverage; wallet-only prepayments have neither that saving nor a switch
cost. Existing coverage does not earn the same saving twice.

`world.netCreditOps.quoteGlobalReliefRate({blockNumber})` observes the whole
fund even when a household's own NFT is new. `totalSeconds`, `cashScaled`,
guarded raw `positionMark` and `annualRateWad` use BigInt (`null` rate at zero
aggregate age); `availableReliefScaled` is separate. Deferred matured debt
stays in the age denominator. Missing/unsafe publications and pending
maturity checkpoints fail instead of supplying a fabricated rate. This
uncapped global ratio is an observation for forecasting, not a promised ROI.

`agent.householdEventsSince(seq)` returns detached, JSON-safe transition
records after the given cursor. Each has stable member/decision/sequence IDs,
current status, accounts/ratios, selected terms and actual receipt evidence.
Money is a decimal string; unavailable readings and undefined ratios remain
explicit. Daily personal records expose `machine` status. Python emits the
matching `household_events` in saved and both live frame modes. See the
checkout's `README-households.org` for the state invariants and data contract;
no new dashboard or runner is required to use these observations.

Households default to one facility per insured asset (`asset_count: 1`);
explicit portfolios retain multiple-facility coverage. Investors resolve
their asset count to zero. The same keyed draws for all other thresholds
are preserved.

`observeNetMarket(session, buck, settlement, pool, feePpm)` returns a pinned
integer snapshot. `netPurchaseExpected`, `netPurchaseCost` and
`netSaleExpected` apply current pool carried fees and trading fees to its
active virtual reserves. Cost returns `null` when no local quote exists.
These estimates can change at crossed ticks and require bounded actual
execution; they do not replace the net-output guard or receipt evidence.
The primary household's market event reuses this reader.

Subpaths map onto the modules directly: `alberta-buck-core/session`,
`/journal`, `/identity`, `/wallet`, `/buckworld`, `/observer`, `/v3`, and
so on.

The Alberta Buck contracts come from
[`alberta-buck-contracts`](https://www.npmjs.com/package/alberta-buck-contracts),
a dependency: `loadArtifact` (`alberta-buck-core/nodefs`) reads it outside a
repository checkout. Third-party contracts (Uniswap, WETH9) are not bundled;
they come from their own packages.

## Status

0.4.0, prototype. Unaudited software for a monetary system, published so the
simulation and identity work can be reproduced and built on -- not for
custody of anything real.

## Licence

CAL-1.0 (Cryptographic Autonomy License v1.0). Beyond the usual copyleft,
CAL requires that anyone you provide this software's functionality to
receives their own data and is not locked out of it by cryptographic or
technical means. See
[LICENSING.md](https://github.com/alberta-buck/alberta-buck/blob/master/LICENSING.md).

Repository: <https://github.com/alberta-buck/alberta-buck>
