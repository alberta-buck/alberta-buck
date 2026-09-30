// buildEquilibriumWorld: the minimal BUCK equilibrium world on any
// session -- deploy.py's basket world with 1..N tokens, on the REAL
// routing periphery.  Promoted from the eqworld.tevm.test.js spike.
//
//   * the proven identity + Direct stack (buildBuckWorld),
//   * BuckBasketProRata + its Uniswap venue facet (union ABI: facet
//     views like basketValueInBuck serve via the shell's fallback),
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

import { advanceTime, buildBuckWorld, DAY, onboard } from "../buckworld.js";
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
 * @param opts.tokens     [{sym, name, dec, p0}] basket constituents
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
  const toks = opts.tokens ?? DEFAULT_TOKENS;
  const targetBuck = opts.targetBuck ?? 10n ** 13n;

  const world = await buildBuckWorld(session, artifacts,
    { identity: opts.identity, rng: opts.rng,
      // Synthetic infrastructure: the harnesses the Python sim deploys
      // (deploy.py / notes_stack.py) -- uncertified binds of SimLP and
      // the routers, credits to proxies that never opt in.
      registryArtifact: "IdentityRegistryHarness",
      creditArtifact: "BuckCreditHarness",
      // The pro-rata basket mints by the hooks: the sims' subclass.
      buckArtifact: "BuckWithBasketHooks" });
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
  const basketArt = artifacts("BuckBasketProRata");
  const basketShell = await session.deploy(basketArt,
    [buck.address, kctrl.address, v3f.address, me, FEES.buck, 600, 64, 500, 1000],
    { name: "BuckBasketProRata", gas });
  const venue = await session.deploy(artifacts("BuckBasketUniswapV3"), [],
    { name: "BuckBasketUniswapV3", gas });
  await session.send(basketShell, "setVenue", [venue.address], { tag: "eq:setVenue" });
  // The basket is wired by the insurance pool: the world's own pool contract
  // acts through its exec(); a caller-supplied pool must be the deployer.
  if (world.pool) {
    await session.send(world.pool, "exec", [buck.address, encodeFunctionData({
      abi: buck.abi, functionName: "setBasket", args: [basketShell.address] })],
      { tag: "eq:buck.setBasket" });
  } else {
    await session.send(buck, "setBasket", [basketShell.address], { tag: "eq:buck.setBasket" });
  }
  await session.send(kctrl, "setBasket", [basketShell.address], { tag: "eq:kctrl.setBasket" });
  await bind(basketShell.address, true, "eq:bind:basket");
  const facetAbi = artifacts("BuckBasketUniswapV3").abi;
  const seen = new Set(basketArt.abi.map((e) => `${e.type}:${e.name}`));
  const basket = session.contractAt(
    basketArt.abi.concat(facetAbi.filter((e) => !seen.has(`${e.type}:${e.name}`))),
    basketShell.address);

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
    const seedTok = (targetBuck * unit) / t.p0;
    await session.send(erc20, "mint", [me, seedTok],
                       { tag: `eq:bootstrap-stock:${t.sym}` });
    await session.send(erc20, "approve", [basket.address, seedTok],
                       { tag: `eq:bootstrap-approve:${t.sym}` });
    await session.send(basket, "depositToken", [erc20.address, seedTok, 0n],
                       { tag: `eq:bootstrap-deposit:${t.sym}`, gas: 3_000_000n });
    tokens.push({ ...t, erc20, poolUsdc, poolBuck: session.contractAt(poolAbi, pb) });
  }

  // --- floating BUCK/USDC pool: SimLP is the BUCK-backed LP ------------
  // Mint targetBuck with a 20% margin, on a face that covers it at the
  // resting K with a further 20%.
  const k0 = await session.call(kctrl, "buckK");
  const mintAmt = (targetBuck * 12n) / 10n;
  const face2 = (((mintAmt * E18) / k0) * 12n) / 10n;
  const FACE = 2n * targetBuck > face2 ? 2n * targetBuck : face2;
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
   *  spendable: quoteMint's insurance principal scaled by the live
   *  fundingFactor, less what the holder's balanceOf already covers. */
  world.gateShortfall = async (account, tranche) => {
    const me = world.holderAddress(account);
    const ids = world._credits.get(account.address) ?? [];
    const arg = await world.mintArg(account, tranche);
    if (arg === 0n) return { required: 0n, shortfall: 0n };
    const quote = await session.call(buck, "quoteMint", [arg, ids]);
    const ff = await session.call(kctrl, "fundingFactor");
    const required = (quote[1] * ff) / E18;
    const bal = await session.call(buck, "balanceOf", [me]);
    return { required, shortfall: required > bal ? required - bal : 0n };
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

  /** Deposit TOKEN i into the basket (the basket mints BUCK and LPs the
   *  pair); returns {receiptId}. */
  world.basketDeposit = async (i, amount, account, { tag } = {}) => {
    const t = tokens[i];
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

  /** Mark a receipt to market: principals valued at today's references
   *  (v1: LP-fee drift inside the position is not counted). */
  world.receiptValueUsd = async (receiptId, day) => {
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
