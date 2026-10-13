// buildEquilibriumWorld: the minimal BUCK equilibrium world on any
// session -- deploy.py's basket world with 1..N tokens, on the REAL
// routing periphery.  Promoted from the eqworld.tevm.test.js spike.
//
//   * the proven identity + Direct stack (buildBuckWorld),
//   * the basket and its Uniswap venue facet (union ABI: facet views like
//     basketValueInBuck serve via the shell's fallback).  By default the
//     equity basket on the basket machine (doc/BASKET-MACHINE.org): a
//     credit holder that pairs every deposit with BUCKs it issues itself,
//     its upkeep facet, the pair observer and policy the facet asks, the
//     parity reference, and the work wheel that turns them (WheelKeeper).
//     opts.basket = "prorata" builds the pro-rata basket instead,
//   * per token: a TOKEN/USDC reference pool (SimLP-seeded, whale
//     territory) and the TOKEN/BUCK basket pool (addBasketToken),
//   * the floating BUCK/USDC pool, LP'd by SimLP as the public
//     BUCK-backed LP (zero-premium credit -> Buck.mint -> LP),
//   * the Universal Router, identity-bound public+carrying -- ALL
//     agent/user swaps route through it (the SimLP doctrine).
//
// The world carries the AGENT-FACING OP HELPERS the platform doctrine
// promises (agents never touch custody, identity binding, or router
// encoding): pledge / buyBuck / sellBuck / route / buyTokenBest /
// basketDeposit / basketRedeem / refUsd / receiptValueUsd / fiatIn,
// plus reads (spotUB / spotUsd / spotBuck / bvib / K) and a per-day
// series recorder for the charts.
//
// Custody pattern: EOA holders send their own txs (a private holder's BUCK
// transfer to a public contract still needs the holder's identity
// handshake with it -- Buck._identityCheckedTransfer).  CREDIT-drawing
// holders get a PROXY (a SimLP instance bound public+NON-carrying --
// carrying accounts cannot draw negative), created lazily by pledge();
// helpers dispatch on holderAddress().  Fiat legs (income, endowments)
// enter as MockERC20 mints via fiatIn -- the sim's fiat bridge.

import { encodeFunctionData, keccak256, toBytes } from "viem";

import { advanceTime, buildBuckWorld, DAY, identityApprove, identityOf, onboard }
  from "../buckworld.js";
import { deployUniversalRouter, encodePath, urExecArgs } from "../router.js";
import { Q96, fullRangeTicks, sqrtPriceX96, spotFromSqrtPriceX96 } from "../v3.js";
import { seededWalk } from "../prices.js";

export const FEES = { usdc: 3000, buck: 3000, ub: 500 };
const SPACING = { 3000: 60, 500: 10 };
const E18 = 10n ** 18n;
// Buck.mint(amount) raises the minter's spendable by `amount` at the K it
// runs at, and reverts past what the credits can give; mint(MINT_MAX)
// delivers all they can give, activating their whole faces.
const MINT_MAX = (1n << 256n) - 1n;
const POOL_ROI_INV = 10n;                 // Buck.sol: deposit = e x coverage
const DEPOSITED_TOPIC = keccak256(toBytes(
  "Deposited(address,uint256,address,uint256,uint256,uint128)"));

// Contract identities are bound by a REGISTERED operator (the certified
// binding repair of 2026-09-15): the deployer onboards first and every
// public contract copies its (pk, E), exactly as sim/identity.py's
// bind_as_operator does.  BN254.sol ABI struct components are UPPERCASE.
const g = (p) => ({ X: p.x, Y: p.y });
const ct = (E) => ({ R: g(E.R), C: g(E.C) });
const OPERATOR_FIELDS = {
  given_name: "Equilibrium", family_name: "Operator",
  jurisdiction: "Alberta, Canada", id_type: "Operator",
  id_number: "OP-0000001", date_of_birth: "1990-01-01",
  issued_at: "2026-01-01T00:00:00Z", epoch: 42,
};

const DEFAULT_TOKENS = [
  { sym: "CNST", name: "Construction", dec: 18, p0: 2_500_000n },
];
// The basket machine compares its constituents in pairs, so it needs two.
const MACHINE_TOKENS = [
  ...DEFAULT_TOKENS,
  { sym: "FOOD", name: "Food", dec: 18, p0: 4_000_000n },
];

// The basket machine's settings, at the control lab's values
// (alberta_buck/sim/basket_machine.py DEFAULTS and OBSERVER).
const LOG_TICK = Math.log(1.0001);
const RATE = BigInt(Math.round(Math.LN2 * 1e18));          // half a gap a day
export const MACHINE = {
  interval: 1800,              // seconds between responses, and between samples
  maxAge: 3600,                // seconds a parity sample stays fresh
  deviationBp: 500,            // a sample is refused when spot is this far from TWAP
  // the pair observer: response, error memory, longest gap, warmup (seconds), rate floor
  observer: [43_200, 86_400, 86_400, 172_800,
             BigInt(Math.round(0.0002 / LOG_TICK / 86_400 * 1e18))],
  sizing: { releaseSpeed: 0n, releaseRate: 4n * E18, closureGain: 0n,
            closureRate: RATE, maxRate: RATE, impactShare: E18,
            slack: 25n * 10n ** 14n, patience: 2n * 10n ** 16n },
  response: { leashBp: 3000, interval: 1800, tiltBp: 5000, leanCapBp: 1500 },
  parityGain: 5n * E18,        // while the BUCK is cheap the book holds five times the parity error, of equity
  parityGainDear: 0n,          // while it is dear the book lets none out for that
  creditFace: 10n ** 23n,      // the basket's own credit: no practical cap
  settle: 601,                 // seconds for the pools' averages to take in a trade
};

/** Integer square root (Newton), for the unwind's impact cap. */
function isqrt(n) {
  if (n < 2n) return n;
  let x = n;
  let y = (x + 1n) / 2n;
  while (y < x) { x = y; y = (x + n / x) / 2n; }
  return x;
}

/**
 * @param session    a Session (deployer = session.account)
 * @param artifacts  (name) => {abi, bytecode}
 * @param opts.identity   the buck-identity kernel API (REQUIRED)
 * @param opts.urArtifact the parsed UniversalRouter.json (REQUIRED)
 * @param opts.basket     "machine" (default: the equity basket on the basket
 *                        machine) or "prorata" (the pro-rata basket)
 * @param opts.tokens     [{sym, name, dec, p0}] basket constituents (the
 *                        machine needs two or more)
 * @param opts.targetBuck common pool depth, 6-dec (default $10M)
 * @param opts.feedSeed / opts.stepBp / opts.feedDays  the reference walks
 * @param opts.rng        scalar drawer for the issuer keypair
 */
export async function buildEquilibriumWorld(session, artifacts, opts = {}) {
  if (!opts.urArtifact) {
    throw new Error("buildEquilibriumWorld needs opts.urArtifact " +
                    "(alberta_buck/sim/artifacts/UniversalRouter.json)");
  }
  const gas = 15_000_000n;
  const kind = opts.basket ?? "machine";
  if (kind !== "machine" && kind !== "prorata") {
    throw new Error(`buildEquilibriumWorld: opts.basket is "machine" or "prorata", not ${kind}`);
  }
  const machine = kind === "machine";
  const toks = opts.tokens ?? (machine ? MACHINE_TOKENS : DEFAULT_TOKENS);
  if (machine && toks.length < 2) {
    throw new Error("the basket machine needs two constituents or more: " +
                    "its policy compares them in pairs (opts.tokens)");
  }
  const targetBuck = opts.targetBuck ?? 10n ** 13n;

  const world = await buildBuckWorld(session, artifacts,
    { identity: opts.identity, rng: opts.rng,
      // Synthetic infrastructure: the harnesses the Python sim deploys
      // (deploy.py / notes_stack.py) -- uncertified binds of SimLP and
      // the routers, credits to proxies that never opt in.
      registryArtifact: "IdentityRegistryHarness",
      creditArtifact: "BuckCreditHarness",
      // The equity basket is an ordinary credit holder on production Buck.
      // The pro-rata basket mints by the hooks: the sims' subclass.
      buckArtifact: machine ? "Buck" : "BuckWithBasketHooks" });
  const { reg, credit, kctrl, buck } = world;
  const me = session.account.address;
  // The deployer registers a real identity, then binds contracts as the
  // certified operator with its own registered (pk, E).
  const operator = await onboard(world, session.account, OPERATOR_FIELDS,
    { rng: opts.rng });
  const bind = (addr, carrying, tag) =>
    session.send(reg, "bindContract",
      [addr, g(operator.kp.pk), ct(operator.E), true, carrying], { tag });

  // --- factory, basket + venue, wiring (deploy.py order) --------------
  const v3f = await session.deploy(artifacts("UniswapV3Factory"), [],
    { name: "UniswapV3Factory", gas });
  // The machine's venue (BasketVenueFacet) refuses a price from a pool that
  // is empty or has no history, so the shell starts on the plain venue,
  // registers its constituents, and takes the machine's venue once the
  // pools are seeded and aged (below).
  const shellName = machine ? "BuckBasketEquity" : "BuckBasketProRata";
  const venueName = "BuckBasketUniswapV3";
  const basketArt = artifacts(shellName);
  const basketShell = await session.deploy(basketArt,
    [buck.address, kctrl.address, v3f.address, me, FEES.buck, 600, 64, 500, 1000],
    { name: shellName, gas });
  const venue = await session.deploy(artifacts(venueName), [], { name: venueName, gas });
  await session.send(basketShell, "setVenue", [venue.address], { tag: "eq:setVenue" });
  await session.send(kctrl, "setBasket", [basketShell.address], { tag: "eq:kctrl.setBasket" });
  if (machine) {
    // A credit holder: bound public and NON-Carrying (it holds a lien), and
    // it issues itself its MARKED credit.
    await bind(basketShell.address, false, "eq:bind:basket");
    await session.send(basketShell, "openCredit", [credit.address, MACHINE.creditFace],
                       { tag: "eq:basket.openCredit" });
  } else {
    // The basket is wired by the insurance pool: the world's own pool contract
    // acts through its exec(); a caller-supplied pool must be the deployer.
    if (world.pool) {
      await session.send(world.pool, "exec", [buck.address, encodeFunctionData({
        abi: buck.abi, functionName: "setBasket", args: [basketShell.address] })],
        { tag: "eq:buck.setBasket" });
    } else {
      await session.send(buck, "setBasket", [basketShell.address], { tag: "eq:buck.setBasket" });
    }
    await bind(basketShell.address, true, "eq:bind:basket");
  }
  // The shell's entry wins a shared name; the facets add their own.
  const union = [...basketArt.abi];
  const seen = new Set(union.map((e) => `${e.type}:${e.name}`));
  for (const name of machine ? [venueName, "BasketVenueFacet", "BasketUpkeepFacet"] : [venueName]) {
    for (const e of artifacts(name).abi) {
      if (!seen.has(`${e.type}:${e.name}`)) { seen.add(`${e.type}:${e.name}`); union.push(e); }
    }
  }
  const basket = session.contractAt(union, basketShell.address);

  // --- USDC, tokens, SimLP ---------------------------------------------
  const erc20Art = artifacts("MockERC20");
  const usdc = await session.deploy(erc20Art, ["USD Coin", "USDC", 6],
    { name: "USDC", gas });
  const simlp = await session.deploy(artifacts("SimLP"), [], { name: "SimLP", gas });
  await bind(simlp.address, false, "eq:bind:simlp");   // public, NON-carrying
  await session.send(usdc, "mint", [simlp.address, 10n ** 30n],
                     { tag: "eq:stock-usdc" });

  const poolAbi = artifacts("UniswapV3Pool").abi;
  const tokens = [];
  for (const t of toks) {
    const erc20 = await session.deploy(erc20Art, [t.name, t.sym, t.dec],
      { name: t.sym, gas });
    await session.send(erc20, "mint", [simlp.address, 10n ** 30n],
                       { tag: `eq:stock-${t.sym}` });

    // TOKEN/USDC reference pool, whale territory.
    await session.send(v3f, "createPool", [erc20.address, usdc.address, FEES.usdc],
                       { tag: `eq:createPool:${t.sym}-usdc` });
    const pu = await session.call(v3f, "getPool",
      [erc20.address, usdc.address, FEES.usdc]);
    const poolUsdc = session.contractAt(poolAbi, pu);
    const unit = 10n ** BigInt(t.dec);
    const sp = sqrtPriceX96(erc20.address, unit, usdc.address, t.p0);
    await session.send(poolUsdc, "initialize", [sp], { tag: `eq:init:${t.sym}-usdc` });
    const u0 = await session.call(poolUsdc, "token0");
    const u1 = await session.call(poolUsdc, "token1");
    const Lu = usdc.address.toLowerCase() === u0.toLowerCase()
      ? (targetBuck * sp) / Q96 : (targetBuck * Q96) / sp;
    const [lo, hi] = fullRangeTicks(SPACING[FEES.usdc]);
    await session.send(simlp, "mint", [pu, lo, hi, Lu, u0, u1],
                       { tag: `eq:seed:${t.sym}-usdc` });

    // TOKEN/BUCK basket pool (created + initialized by the basket).
    await session.send(basket, "addBasketToken",
      [erc20.address, t.dec, t.p0, 0, FEES.buck], { tag: `eq:addBasketToken:${t.sym}` });
    const pb = await session.call(v3f, "getPool",
      [erc20.address, buck.address, FEES.buck]);
    await bind(pb, true, `eq:bind:pool-${t.sym}-buck`);

    // Bootstrap the basket pool to real depth (the Python sim's pre-tick
    // DM bootstrap phase): a targetBuck-sized deposit from the deployer,
    // so agent-scale trades and the arb's daily corrections move the
    // pool by basis points, not percent -- the redeem path's spot/TWAP
    // guard (defaultMaxDeviationBp) assumes exactly this.
    // The pro-rata basket pairs the deposit itself, here.  The equity basket
    // takes it once its machine is wired, below, and the wheel pairs it.
    const seedTok = (targetBuck * unit) / t.p0;
    await session.send(erc20, "mint", [me, seedTok],
                       { tag: `eq:bootstrap-stock:${t.sym}` });
    await session.send(erc20, "approve", [basket.address, seedTok],
                       { tag: `eq:bootstrap-approve:${t.sym}` });
    if (!machine) {
      await session.send(basket, "depositToken", [erc20.address, seedTok, 0n],
                         { tag: `eq:bootstrap-deposit:${t.sym}`, gas: 3_000_000n });
    }
    tokens.push({ ...t, erc20, poolUsdc, seedTok,
                  poolBuck: session.contractAt(poolAbi, pb) });
  }

  // --- floating BUCK/USDC pool: SimLP is the BUCK-backed LP ------------
  // Mint targetBuck with a 20% margin, on a face that covers it at the
  // resting K with a further 20%.
  const k0 = await session.call(kctrl, "buckK");
  // On the machine SimLP also seeds each TOKEN/BUCK pool, a tenth as deep.
  const seedBuck = machine ? targetBuck / 10n : 0n;
  const mintAmt = ((targetBuck + seedBuck * BigInt(tokens.length)) * 12n) / 10n;
  const face2 = (((mintAmt * E18) / k0) * 12n) / 10n;
  const depth = targetBuck + seedBuck * BigInt(tokens.length);
  const FACE = 2n * depth > face2 ? 2n * depth : face2;
  const now = (await session.client.getBlock()).timestamp;
  await session.send(credit, "createCredit",
    [simlp.address, 0, FACE, 0n, 0, 0, now, 0], { tag: "eq:credit:simlp" });
  await session.send(simlp, "exec",
    [buck.address, encodeFunctionData(
      { abi: buck.abi, functionName: "mint", args: [mintAmt] })],
    { tag: "eq:simlp-mint-buck", gas: 3_000_000n });
  await session.send(v3f, "createPool", [buck.address, usdc.address, FEES.ub],
                     { tag: "eq:createPool:buck-usdc" });
  const pub = await session.call(v3f, "getPool", [buck.address, usdc.address, FEES.ub]);
  const poolUB = session.contractAt(poolAbi, pub);
  const spU = sqrtPriceX96(buck.address, 1_000_000n, usdc.address, 1_000_000n);
  await session.send(poolUB, "initialize", [spU], { tag: "eq:init:buck-usdc" });
  await bind(pub, true, "eq:bind:pool-buck-usdc");
  const b0 = await session.call(poolUB, "token0");
  const b1 = await session.call(poolUB, "token1");
  const Lub = usdc.address.toLowerCase() === b0.toLowerCase()
    ? (targetBuck * spU) / Q96 : (targetBuck * Q96) / spU;
  const [loU, hiU] = fullRangeTicks(SPACING[FEES.ub]);
  await session.send(simlp, "mint", [pub, loU, hiU, Lub, b0, b1],
                     { tag: "eq:seed:buck-usdc" });

  // --- the basket machine: observer, policy, reference, upkeep, wheel ---
  // (BasketMachineIntegration.t.sol's wiring, at the control lab's settings.)
  let parts = null;
  if (machine) {
    // Seed and age the basket's pools: an independent LP's full-range
    // position in each, a tenth of the common depth, then the TWAP window.
    for (const t of tokens) {
      const unit = 10n ** BigInt(t.dec);
      const sp = sqrtPriceX96(t.erc20.address, unit, buck.address, t.p0);
      const t0 = await session.call(t.poolBuck, "token0");
      const t1 = await session.call(t.poolBuck, "token1");
      const L = buck.address.toLowerCase() === t0.toLowerCase()
        ? (seedBuck * sp) / Q96 : (seedBuck * Q96) / sp;
      const [lo, hi] = fullRangeTicks(SPACING[FEES.buck]);
      await session.send(simlp, "mint", [t.poolBuck.address, lo, hi, L, t0, t1],
                         { tag: `eq:seed:${t.sym}-buck` });
    }
    await advanceTime(world, MACHINE.settle);
    const machineVenue = await session.deploy(artifacts("BasketVenueFacet"), [],
      { name: "BasketVenueFacet", gas });
    await session.send(basket, "setVenue", [machineVenue.address],
                       { tag: "eq:setVenue:machine" });

    const observer = await session.deploy(artifacts("PairSignals"),
      [tokens.map((t) => t.erc20.address), [buck.address, usdc.address],
       [...tokens.map((t) => t.poolBuck.address), ...tokens.map((t) => t.poolUsdc.address)],
       ...MACHINE.observer],
      { name: "PairSignals", gas });
    // Each constituent's reference is the price it joined at, never a live one.
    const reference = tokens.map((t) =>
      BigInt(Math.round(Math.log(Number(t.p0) / 1e6) / LOG_TICK * 1e18)));
    const policy = await session.deploy(artifacts("PairRebalancePolicy"),
      [observer.address, basket.address, reference, MACHINE.sizing],
      { name: "PairRebalancePolicy", gas });
    const parity = await session.deploy(artifacts("BasketReference"),
      [basket.address, MACHINE.interval, MACHINE.maxAge, MACHINE.deviationBp],
      { name: "BasketReference", gas });
    const upkeep = await session.deploy(artifacts("BasketUpkeepFacet"),
      [policy.address, parity.address, MACHINE.parityGain, MACHINE.parityGainDear],
      { name: "BasketUpkeepFacet", gas });
    const wheel = await session.deploy(artifacts("BasketMachineWheel"),
      [buck.address, usdc.address, me, 0, 0n], { name: "BasketMachineWheel", gas });
    await bind(wheel.address, true, "eq:bind:wheel");
    await session.send(basket, "setEquityWheel", [upkeep.address], { tag: "eq:setEquityWheel" });
    // The upkeep facet is written for a credit marked net: K bounds the
    // BUCKs the basket has let out, not those it holds beside its TOKEN.
    await session.send(basket, "setNetMark", [true], { tag: "eq:setNetMark" });
    await session.send(basket, "setResponse", [MACHINE.response], { tag: "eq:setResponse" });
    await session.send(basket, "setWheel", [wheel.address], { tag: "eq:setWheel" });
    await session.send(wheel, "setSample", [parity.address], { tag: "eq:wheel.setSample" });
    await session.send(wheel, "setController", [kctrl.address], { tag: "eq:wheel.setController" });
    await session.send(wheel, "setEquity", [basket.address], { tag: "eq:wheel.setEquity" });
    parts = { observer, policy, parity, upkeep, wheel };
    world.turnWheel = async ({ tag } = {}) => {
      await session.send(wheel, "rearm", [], { tag: `${tag ?? "eq:wheel"}:rearm` });
      return session.send(wheel, "tick", [12n, 0n],
                          { tag: tag ?? "eq:wheel", gas: 12_000_000n });
    };

    // Bootstrap the basket's pools to real depth: a targetBuck-sized
    // deposit of each TOKEN from the deployer, which the wheel pairs with
    // BUCKs the basket issues.
    for (const t of tokens) {
      await session.send(basket, "depositToken", [t.erc20.address, t.seedTok, 0n],
                         { tag: `eq:bootstrap-deposit:${t.sym}`, gas: 3_000_000n });
    }
    for (let turn = 0; turn < 8; turn++) {
      await advanceTime(world, MACHINE.interval + 1);
      await world.turnWheel({ tag: `eq:bootstrap-wheel:${turn}` });
      const placed = await Promise.all(tokens.map((_, i) =>
        session.call(basket, "liquidityOf", [BigInt(i)])));
      if (placed.every((l) => l > 0n)) break;
      if (turn === 7) throw new Error("the wheel did not pair the bootstrap deposits");
    }
  }


  // --- the real router, identity-bound ---------------------------------
  const weth = await session.deploy(artifacts("WETH9"), [], { name: "WETH9", gas });
  const router = await deployUniversalRouter(session, opts.urArtifact,
    { weth: weth.address, v3Factory: v3f.address,
      poolInitCode: artifacts("UniswapV3Pool").bytecode });
  await bind(router.address, true, "eq:bind:router");

  // --- reference feeds (the whales' walks; refUsd's ground truth) ------
  const feeds = tokens.map((t, i) => seededWalk({
    seed: (opts.feedSeed ?? 0xF00D5) + i, start: t.p0,
    stepBp: opts.stepBp ?? 80, steps: opts.feedDays ?? 4000 }));

  Object.assign(world, {
    v3f, basket, usdc, simlp, tokens, poolUB, router, weth, feeds,
    fees: FEES, series: [], _proxies: new Map(),
    receipts: tokens.length,        // the bootstrap deposits' receipts
    basketKind: kind,
    machine: parts,                 // null on the pro-rata basket
    wheel: parts?.wheel ?? null,
  });

  // ==== agent-facing op helpers (the doctrine's plumbing layer) ========

  const erc20At = (addr) => session.contractAt(erc20Art.abi, addr);
  world.holderAddress = (account) =>
    world._proxies.get(account.address)?.address ?? account.address;

  world.spotUB = async () => spotFromSqrtPriceX96(
    (await session.call(poolUB, "slot0"))[0], buck.address, 6, usdc.address);
  world.spotUsd = async (i) => spotFromSqrtPriceX96(
    (await session.call(tokens[i].poolUsdc, "slot0"))[0],
    tokens[i].erc20.address, tokens[i].dec, usdc.address);
  world.spotBuck = async (i) => spotFromSqrtPriceX96(
    (await session.call(tokens[i].poolBuck, "slot0"))[0],
    tokens[i].erc20.address, tokens[i].dec, buck.address);
  world.bvib = async () => session.call(basket, "basketValueInBuck");
  world.K = async () => session.call(kctrl, "buckK");
  world.usdcForBuck = async (buckOut) =>
    (buckOut * (await world.spotUB()) / 1_000_000n) * 102n / 100n;
  world.refUsd = (i, day, amount) =>
    (amount * feeds[i][day % feeds[i].length]) / 10n ** BigInt(tokens[i].dec);

  /** The fiat bridge in: income, endowments (MockERC20 mint). */
  world.fiatIn = (account, amount, { tag } = {}) =>
    session.send(usdc, "mint", [world.holderAddress(account), amount],
                 { tag: tag ?? "eq:fiat-in" });

  /** Lazy per-holder proxy: SimLP instance, bound public+NON-carrying
   *  (carrying accounts cannot draw credit negative). */
  world.proxyFor = async (account) => {
    let p = world._proxies.get(account.address);
    if (!p) {
      p = await session.deploy(artifacts("SimLP"), [],
        { name: "HolderProxy", gas });
      await bind(p.address, false, "eq:bind:proxy");
      world._proxies.set(account.address, p);
    }
    return p;
  };

  /** Pledge insured assets: BuckCredit to the holder's proxy + activate
   *  the face as headroom (zero premium: the ff gate is inapplicable). */
  world.pledge = async (account, face, { tag } = {}) => {
    const p = await world.proxyFor(account);
    const ts = (await session.client.getBlock()).timestamp;
    await session.send(credit, "createCredit",
      [p.address, 0, face, 0n, 0, 0, ts, 0], { tag: `${tag}:credit` });
    await session.send(p, "exec",
      [buck.address, encodeFunctionData(
        { abi: buck.abi, functionName: "mint", args: [face] })],
      { tag: `${tag}:activate`, gas: 3_000_000n });
    return p;
  };

  // ==== insured-credit ops: the AUDITED debtor's plumbing ==============
  //
  // The honest issuance channel proven by the Python ledger audit
  // (test_debtor_ledger.py): premium-bearing credits make Buck.mint's
  // funding-factor gate REAL, tranches activate against unactivated
  // face, the liability is quoted net of BuckCredit's Jubilee aging,
  // and the unwind bites only below USD par, sized so the buy itself
  // cannot lift the pool past par.

  world._credits = new Map();

  /** Pledge insured assets as PREMIUM-BEARING credits (no upfront mint:
   *  tranches activate later through the live funding gate). */
  world.pledgeInsured = async (account, faces, premiumBp, { tag } = {}) => {
    const p = await world.proxyFor(account);
    const ts = (await session.client.getBlock()).timestamp;
    for (const face of faces) {
      await session.send(credit, "createCredit",
        [p.address, 0, face, 0n, 0, 0, ts, Number(premiumBp)],
        { tag: `${tag}:credit` });
    }
    const ids = [];
    for (let i = 0; i < faces.length; i++) {
      ids.push(await session.call(credit, "tokenOfOwnerByIndex",
                                  [p.address, BigInt(i)]));
    }
    world._credits.set(account.address, ids);
    return p;
  };

  /** The debtor's chain truth in one read: drawn / limit / held /
   *  unactivated face / the Jubilee relief quote (the lien melts:
   *  Buck.reliefOf, ~2%/yr of the drawn balance) / spendable (the
   *  activated headroom, limit - drawn) / capacity (the spendable the
   *  unactivated face can still deliver: K - e per unit, e the premium's
   *  deposit) / headroom (spendable + capacity: the most it can draw). */
  world.creditState = async (account) => {
    const me = world.holderAddress(account);
    const ids = world._credits.get(account.address) ?? [];
    const [signed, limit, jub, k] = await Promise.all([
      session.call(buck, "signedBalanceOf", [me]),
      session.call(buck, "creditLimit", [me]),
      session.call(buck, "reliefOf", [me]),
      session.call(kctrl, "buckK")]);
    let unactivated = 0n, capacity = 0n;
    for (const tid of ids) {
      const info = await session.call(credit, "creditInfo", [tid]);
      const [face, act, rate] = [info[0], info[1], BigInt(info[2])];
      const room = face > act ? face - act : 0n;
      const per = k - rate * POOL_ROI_INV * 10n ** 14n;     // K - e, 1e18
      unactivated += room;
      capacity += per > 0n ? (room * per) / E18 : 0n;
    }
    const drawn = signed < 0n ? -signed : 0n;
    const spendable = limit > drawn ? limit - drawn : 0n;
    return { drawn, limit, held: signed > 0n ? signed : 0n, unactivated, jub,
             spendable, capacity, headroom: spendable + capacity };
  };

  /** The Buck.mint argument that makes `tranche` spendable: what it
   *  needs beyond the headroom already activated, or MINT_MAX (all the
   *  credits give) once that is within 1% of their capacity -- K moves a
   *  little before the mint runs.  0n when the headroom covers it. */
  world.mintArg = async (account, tranche) => {
    const cs = await world.creditState(account);
    const need = tranche > cs.spendable ? tranche - cs.spendable : 0n;
    if (need === 0n) return 0n;
    return need * 100n >= cs.capacity * 99n ? MINT_MAX : need;
  };

  /** What the funding gate demands before the mint that makes `tranche`
   *  spendable: quoteMint's insurance principal scaled by the
   *  fundingFactor a mint sent now would face, less what the holder's
   *  balanceOf already covers.  Buck.mint runs the controller's cycle
   *  before it reads the factor, so this runs it first (anyone may): a
   *  factor from the last cycle is up to a day old, and at its slope half
   *  a percent in the BUCK is an eighth of a deposit.  Half a percent
   *  over, since the gate is strict. */
  world.gateShortfall = async (account, tranche) => {
    const me = world.holderAddress(account);
    const ids = world._credits.get(account.address) ?? [];
    const arg = await world.mintArg(account, tranche);
    if (arg === 0n) return { required: 0n, shortfall: 0n };
    await session.send(kctrl, "compute", [], { tag: "gate:compute", expect: "either" });
    const quote = await session.call(buck, "quoteMint", [arg, ids]);
    const ff = await session.call(kctrl, "fundingFactor");
    const required = (quote[1] * ff) / E18 * 1005n / 1000n;
    const bal = await session.call(buck, "balanceOf", [me]);
    return { required, shortfall: required > bal ? required - bal : 0n };
  };

  /** Buy BUCKs toward the gate's shortfall for `tranche`, spending at
   *  most `budget` dollars: the shortfall at the pool's price, a percent
   *  and a dollar over.  Returns {spend, got}. */
  world.buyForGate = async (account, tranche, budget, opts2 = {}) => {
    const { shortfall } = await world.gateShortfall(account, tranche);
    if (shortfall === 0n) return { spend: 0n, got: 0n };
    const want = shortfall * (await world.spotUB()) / 10n ** 6n * 101n / 100n + 10n ** 6n;
    const spend = want < budget ? want : budget;
    if (spend < 10n ** 6n) return { spend: 0n, got: 0n };
    return { spend, got: await world.buyBuck(spend, account, opts2) };
  };

  /** The dollars a household finds for the insurance deposit of
   *  refinancing `amount`, from savings or a relative: the deposit that
   *  mint would pay, at the pool's price.  Arrives on the fiat rail.  A
   *  household about to retire a debt that bears interest does the
   *  arithmetic and finds it; with none it cannot hold the BUCKs the
   *  funding gate asks, and is shut out while the BUCK is near parity. */
  world.findDepositCash = async (account, amount) => {
    const ids = world._credits.get(account.address) ?? [];
    const cs = await world.creditState(account);
    if (cs.capacity === 0n || amount === 0n) return 0n;
    const arg = amount * 100n >= cs.capacity * 99n ? MINT_MAX : amount;
    const quote = await session.call(buck, "quoteMint", [arg, ids]);
    const cash = quote[1] * (await world.spotUB()) / 10n ** 6n;
    if (cash > 0n) await world.fiatIn(account, cash, { tag: "debtor:found-cash" });
    return cash;
  };

  /** Activate what makes `tranche` spendable, through the REAL gate
   *  (nothing, when the activated headroom covers it).  Returns {ok,
   *  premium} -- premium is the insurance principal drawn (signed delta);
   *  a revert is the gate saying "save more" (the caller's throttle). */
  world.mintTranche = async (account, tranche, { tag } = {}) => {
    const p = world._proxies.get(account.address);
    const arg = await world.mintArg(account, tranche);
    if (arg === 0n) return { ok: true, premium: 0n };
    const before = await session.call(buck, "signedBalanceOf", [p.address]);
    const rcpt = await session.send(p, "exec",
      [buck.address, encodeFunctionData(
        { abi: buck.abi, functionName: "mint", args: [arg] })],
      { tag, gas: 3_000_000n, expect: "either" });
    if (rcpt.status !== "success") return { ok: false, premium: 0n };
    const after = await session.call(buck, "signedBalanceOf", [p.address]);
    return { ok: true, premium: before > after ? before - after : 0n };
  };

  /** Sell capped by the live spendable balance (held + unused credit). */
  world.sellBuckCapped = async (buckIn, account, opts2 = {}) => {
    const me = world.holderAddress(account);
    const sp = await session.call(buck, "balanceOf", [me]);
    const amt = buckIn < sp ? buckIn : sp;
    if (amt < 10n ** 6n) return { sold: 0n, got: 0n };
    return { sold: amt, got: await world.sellBuck(amt, account, opts2) };
  };

  /** The unwind bite: the pool's USDC spot and the largest BUCK buy
   *  that cannot lift it past par -- buying x of reserve r moves spot p
   *  to p*(r/(r-x))^2, which stays <= 1 for x <= r*(1-sqrt(p)). */
  world.unwindBite = async () => {
    const spot = await world.spotUB();
    const reserve = await session.call(buck, "balanceOf", [poolUB.address]);
    const E6 = 10n ** 6n;
    if (spot >= E6) return { spot, capBuck: 0n };
    const cap = (reserve * (E6 - isqrt(spot * E6))) / E6;
    return { spot, capBuck: cap };
  };

  /** Swap along a [token, fee, token, ...] path through the REAL router
   *  (pre-fund route); returns the output-token delta at the holder. */
  world.route = async (path, amountIn, account, { tag } = {}) => {
    const holder = world.holderAddress(account);
    const proxy = world._proxies.get(account.address);
    const tokenIn = erc20At(path[0]);
    const tokenOut = erc20At(path[path.length - 1]);
    const before = await session.call(tokenOut, "balanceOf", [holder]);
    if (proxy) {
      await session.send(proxy, "exec",
        [path[0], encodeFunctionData({ abi: erc20Art.abi,
          functionName: "transfer", args: [router.address, amountIn] })],
        { tag: `${tag}:fund` });
    } else {
      await session.send(tokenIn, "transfer", [router.address, amountIn],
                         { account, tag: `${tag}:fund` });
    }
    const [commands, inputs] = urExecArgs(holder, amountIn, encodePath(path));
    await session.send(router, "execute", [commands, inputs],
                       { tag, gas: 1_500_000n });
    return (await session.call(tokenOut, "balanceOf", [holder])) - before;
  };

  world.buyBuck = (usdcIn, account, opts2 = {}) => world.route(
    [usdc.address, FEES.ub, buck.address], usdcIn, account, opts2);
  world.sellBuck = (buckIn, account, opts2 = {}) => world.route(
    [buck.address, FEES.ub, usdc.address], buckIn, account, opts2);

  /** Buy TOKEN i with USDC via the better-quoted route (direct vs
   *  through BUCK), quoted client-side from spots + fees. */
  world.buyTokenBest = async (i, usdcIn, account, opts2 = {}) => {
    const t = tokens[i];
    const unit = 10n ** BigInt(t.dec);
    const [pTU, pUB, pTB] = await Promise.all(
      [world.spotUsd(i), world.spotUB(), world.spotBuck(i)]);
    const direct = (usdcIn * unit / pTU) * 9970n / 10000n;
    const buckOut = (usdcIn * 1_000_000n / pUB) * 9995n / 10000n;
    const viaBuck = (buckOut * unit / pTB) * 9970n / 10000n;
    const path = viaBuck > direct
      ? [usdc.address, FEES.ub, buck.address, FEES.buck, t.erc20.address]
      : [usdc.address, FEES.usdc, t.erc20.address];
    const out = await world.route(path, usdcIn, account, opts2);
    return { out, viaBuck: viaBuck > direct };
  };

  /** The equity basket pays its exits in BUCK, and Buck lets a public
   *  contract send a private holder BUCK only once that holder has made the
   *  identity-bound approval of it.  Made once, at the holder's first
   *  deposit, for a holder registered through this world. */
  const approved = new Set();
  const basketParty = { kp: operator.kp, account: { address: basket.address },
                        fields: { given_name: "BuckBasket" } };
  world.approveBasket = async (account) => {
    const key = account.address.toLowerCase();
    if (!machine || approved.has(key) || world._proxies.has(account.address)) return;
    const holder = identityOf(world, account.address);
    if (!holder) throw new Error(`${account.address} is not registered: it cannot be paid BUCK`);
    await identityApprove(world, holder, basketParty);
    approved.add(key);
  };

  /** Deposit TOKEN i into the basket; returns {receiptId}.  The pro-rata
   *  basket mints BUCK and LPs the pair at once; the equity basket holds
   *  the TOKEN until its wheel next turns and pairs it. */
  world.basketDeposit = async (i, amount, account, { tag } = {}) => {
    const t = tokens[i];
    await world.approveBasket(account);
    await session.send(t.erc20, "approve", [basket.address, amount],
                       { account, tag: `${tag}:approve` });
    const rcpt = await session.send(basket, "depositToken",
      [t.erc20.address, amount, 0n],
      { account, tag, gas: 3_000_000n });
    const log = rcpt.logs.find((l) =>
      l.address.toLowerCase() === basket.address.toLowerCase()
      && l.topics[0] === DEPOSITED_TOPIC);
    world.receipts += 1;
    return { receiptId: BigInt(log.topics[2]) };
  };

  /** basketDeposit for proxy holders (credit-drawing debtors): the
   *  tokens live at the proxy, so approval and deposit exec through it;
   *  the receipt lands on the proxy.  EOA holders fall through. */
  world.basketDepositAs = async (i, amount, account, { tag } = {}) => {
    const p = world._proxies.get(account.address);
    if (!p) return world.basketDeposit(i, amount, account, { tag });
    const t = tokens[i];
    await session.send(p, "exec",
      [t.erc20.address, encodeFunctionData({ abi: erc20Art.abi,
        functionName: "approve", args: [basket.address, amount] })],
      { tag: `${tag}:approve` });
    const rcpt = await session.send(p, "exec",
      [basket.address, encodeFunctionData({ abi: basket.abi,
        functionName: "depositToken", args: [t.erc20.address, amount, 0n] })],
      { tag, gas: 3_000_000n });
    const log = rcpt.logs.find((l) =>
      l.address.toLowerCase() === basket.address.toLowerCase()
      && l.topics[0] === DEPOSITED_TOPIC);
    world.receipts += 1;
    return { receiptId: BigInt(log.topics[2]) };
  };

  /** Redeem a receipt in full; returns the USD value received (token at
   *  the day's spot + BUCK at the floating spot), 6-dec. */
  world.basketRedeem = async (receiptId, account, { tag } = {}) => {
    const holder = world.holderAddress(account);
    const before = await Promise.all([
      session.call(buck, "balanceOf", [holder]),
      ...tokens.map((t) => session.call(t.erc20, "balanceOf", [holder]))]);
    await session.send(basket, "redeem", [receiptId, 10_000n],
                       { account, tag, gas: 5_000_000n });
    const after = await Promise.all([
      session.call(buck, "balanceOf", [holder]),
      ...tokens.map((t) => session.call(t.erc20, "balanceOf", [holder]))]);
    let usd = (after[0] - before[0]) * (await world.spotUB()) / 1_000_000n;
    for (let i = 0; i < tokens.length; i++) {
      usd += (after[i + 1] - before[i + 1]) * (await world.spotUsd(i))
           / 10n ** BigInt(tokens[i].dec);
    }
    return usd;
  };

  /** Mark a receipt to market.  On the equity basket: its shares at the
   *  share price, in BUCK, at the floating spot.  On the pro-rata basket:
   *  principals valued at today's references (v1: LP-fee drift inside the
   *  position is not counted). */
  world.receiptValueUsd = async (receiptId, day) => {
    if (machine) {
      return (await session.call(basket, "valueOf", [receiptId]))
             * (await world.spotUB()) / 1_000_000n;
    }
    const dep = await session.call(basket, "deposits", [receiptId]);
    const [principalBuck, principalTok, tokenAddr] = [dep[0], dep[1], dep[2]];
    const i = tokens.findIndex((t) =>
      t.erc20.address.toLowerCase() === String(tokenAddr).toLowerCase());
    let usd = principalBuck * (await world.spotUB()) / 1_000_000n;
    if (i >= 0) usd += world.refUsd(i, day, principalTok);
    return usd;
  };

  /** Sample the loop's observables into world.series (chart food):
   *  the controller pair (K, fundingFactor), the peg observable, and
   *  every pool's spot beside its reference. */
  world.record = async (day) => {
    const [K, bvib, spotUB, ff] = await Promise.all(
      [world.K(), world.bvib(), world.spotUB(),
       session.call(kctrl, "fundingFactor")]);
    const spots = await Promise.all(tokens.map((_, i) => world.spotUsd(i)));
    const spotsBuck = await Promise.all(tokens.map((_, i) => world.spotBuck(i)));
    const refs = tokens.map((_, i) => feeds[i][day % feeds[i].length]);
    world.series.push({ day, K, bvib, spotUB, ff, spots, spotsBuck, refs });
  };

  return world;
}

/** The clock: FIRST in the agent order, it advances simulated time one
 *  day per tick -- demurrage accrues, the PID integrates real dT. */
export class DayClock {
  async act(world, day, tick) {
    if (tick === 0) await advanceTime(world, DAY);
  }
}

/** Turns the basket machine's work wheel once a tick, after the market's
 *  agents: it samples parity, steps K, and lets the basket pair what waits
 *  and work its constituents toward their targets.  Anyone may turn it.
 *  It first lets the pools' averages take in the tick's trades, as a real
 *  keeper arriving a few minutes later would find them.  The basket's own
 *  trades then move its pools, so the arbitrageur given as opts.arb makes
 *  a second pass after the turn (the control lab's order: arbitrage, the
 *  wheel, arbitrage).  Does nothing on the pro-rata basket, which has no
 *  wheel. */
export class WheelKeeper {
  constructor({ arb = null } = {}) {
    this.arb = arb;
  }
  async act(world, day, tick) {
    if (!world.wheel) return;
    await advanceTime(world, MACHINE.settle);
    await world.turnWheel({ tag: `wheel:d${day}` });
    if (this.arb) await this.arb.act(world, day, tick);
  }
}

/** Advances the PID every tick -- compute() is permissionless and
 *  no-ops until dT has elapsed, exactly the Python PidKeeperAgent. */
export class PidKeeper {
  async act(world, day, tick) {
    await world.session.send(world.kctrl, "compute", [],
                             { tag: `pid:compute:d${day}` });
  }
}

/** The fiat rail: a fixed USDC income minted to `account`'s holder
 *  every `everyDays` (agent order puts income before its spender). */
export class MonthlyIncome {
  constructor({ account, amount, everyDays = 30 }) {
    Object.assign(this, { account, amount, everyDays });
  }
  async act(world, day, tick) {
    if (tick !== 0 || day % this.everyDays !== 0) return;
    await world.fiatIn(this.account, this.amount, { tag: `income:d${day}` });
  }
}
