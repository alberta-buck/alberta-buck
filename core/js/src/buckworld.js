// buildBuckWorld: the REAL BUCK stack on any session (Tevm in-process or
// anvil), mirroring the Python sim's deploy.py order -- IdentityRegistry
// (+ a kernel-keyed trusted issuer), BuckCredit, BuckKControllerDirect,
// Buck, and their wiring.  The V3 pool world composes alongside via
// buildOnePool (scenarios/onepool.js); the basket lands with the
// background-rebalancing stage.
//
// Identity onboarding runs the FULL wallet ceremony on the wasm
// buck-identity kernel (canonical identity -> m -> PS sign/rerandomize ->
// ElGamal -> registration NIZK) and the real Solidity verifier accepts it
// on-chain -- the capability the in-browser demo rests on.
//
// Each ceremony also comes in the halves its parties perform, for tools
// that give every role its own screen: issueCredential (the issuer,
// off-chain) + registerWallet (the holder) = onboard; insureAsset (the
// insurer) + activateCredit (the holder) = createCredit.
//
// Environment-free by injection (the artifacts(name) pattern): pass the
// identity kernel API as opts.identity -- node code loads it from
// ./identity.js, the browser from ./identity-web.js -- and optionally
// opts.rng (a () => bigint scalar drawer) to reproduce a world; the
// default draws WebCrypto.

import { encodeFunctionData, keccak256, parseEventLogs, toBytes } from "viem";
import { privateKeyToAccount } from "viem/accounts";

// BN254.sol struct components are UPPERCASE X/Y at the ABI.
const g = (p) => ({ X: p.x, Y: p.y });
const ct = (E) => ({ R: g(E.R), C: g(E.C) });
const g2 = (p) => ({ X: [p.x[0], p.x[1]], Y: [p.y[0], p.y[1]] });

// Controller deploy defaults -- the SAME values experiment.deploy_params
// derives for the Python sim (gains real*1e12: Kp = kp_frac*dk_rail/e_max
// = 0.1 -> 1e11; Ki = dk_rail/(e_max*tau_I) = 0.5/777600s -> 643004;
// dt 1800s; K rails 0..0.95, K0 0.75).
export const DEPLOY_DEFAULTS = {
  kp: 100_000_000_000n,
  ki: 643_004n,
  kd: 0n,
  dt: 1800n,
  kmin: 0n,
  kmax: 950_000_000_000_000_000n,
  k0: 750_000_000_000_000_000n,
};

export const DAY = 86_400;

/** The world operator: a registered account that binds the world's own
 *  infrastructure (the insurance pool) through the registry.  A fixed key of
 *  its own, so it never collides with a caller's accounts. */
export const WORLD_OPERATOR_KEY = keccak256(toBytes("alberta-buck: world operator"));
export const WORLD_OPERATOR_FIELDS = {
  given_name: "World", family_name: "Operator",
  jurisdiction: "Alberta, Canada", id_type: "Operator",
  id_number: "OP-0000000", date_of_birth: "1990-01-01",
  issued_at: "2026-01-01T00:00:00Z", epoch: 42,
};

/**
 * Deploy the BUCK identity + monetary stack.
 *
 * @param session   a Session (deployer = session.account)
 * @param artifacts (name) => {abi, bytecode}
 * @param opts.identity the buck-identity kernel API (REQUIRED: import
 *                  from ./identity.js in node, loadIdentity() in the browser)
 * @param opts.gov      governance address    (default: deployer)
 * @param opts.poolAcct the insurance pool (default: a SimLP contract the
 *                  world operator binds Carrying through the registry, as
 *                  Buck's pool is meant to be; a caller-supplied pool is the
 *                  caller's to bind)
 * @param opts.params   controller overrides over DEPLOY_DEFAULTS
 * @param opts.rng      scalar drawer for the issuer PS keypair
 * @param opts.registryArtifact "IdentityRegistry" (default) or the test
 *                  harness for synthetic worlds
 * @param opts.creditArtifact   "BuckCredit" (default) or the test harness
 * @returns world {session, artifacts, id, reg, credit, kctrl, buck, gov,
 *                 poolAcct, pool (its handle, when the world made it),
 *                 operator (the world operator's handle, likewise),
 *                 issuer:{addr, skX, skY, pkX, pkY}}
 */
export async function buildBuckWorld(session, artifacts, opts = {}) {
  const id = opts.identity;
  if (!id) {
    throw new Error(
      "buildBuckWorld needs opts.identity (the buck-identity kernel API: " +
      "import from src/identity.js in node, loadIdentity() in the browser)");
  }
  const gas = 15_000_000n;
  const gov = opts.gov ?? session.account.address;
  const p = { ...DEPLOY_DEFAULTS, ...(opts.params ?? {}) };
  const rng = opts.rng ?? id.randScalar;

  // --- identity layer (deploy.py order) ---------------------------------
  // Production worlds deploy IdentityRegistry.  Simulation worlds that bind
  // synthetic infrastructure (SimLP, routers, basket variants) with no
  // production binding-authorizer surface pass registryArtifact =
  // "IdentityRegistryHarness", exactly as alberta_buck/sim/deploy.py does.
  const regName = opts.registryArtifact ?? "IdentityRegistry";
  const reg = await session.deploy(artifacts(regName), [gov],
    { name: regName, gas });
  const [skX, skY] = [rng(), rng()];
  const issuer = {
    addr: "0x" + "15".repeat(20),   // the trust-anchor address
    skX, skY,
    pkX: id.g2Mul(id.G2, skX),
    pkY: id.g2Mul(id.G2, skY),
    pkY1: id.g1Mul(id.G1, skY),     // G1 image of y: the A' blinding base
  };
  await session.send(reg, "trustIssuer",
    [issuer.addr, { X: g2(issuer.pkX), Y: g2(issuer.pkY), Y1: g(issuer.pkY1) }],
    { tag: "world:trustIssuer" });

  // --- the insurance pool ------------------------------------------------
  // A contract bound Carrying through the registry, as Buck's pool is meant
  // to be: it holds premium deposits on its members' behalf, so the
  // demurrage they accrue travels with them instead of eroding the reserve.
  // SimLP is a plain holder whose exec() lets the pool act for itself.  The
  // world operator registers, the pool authorizes the exact binding, and the
  // operator binds it.
  let pool = null;
  let operator = null;
  let poolAcct = opts.poolAcct;
  if (!poolAcct) {
    pool = await session.deploy(artifacts("SimLP"), [], { name: "InsurancePool", gas });
    poolAcct = pool.address;
    const early = { session, id, reg, issuer };
    const opAcct = privateKeyToAccount(WORLD_OPERATOR_KEY);
    await fundAccount(early, opAcct.address);
    operator = await onboard(early, opAcct, WORLD_OPERATOR_FIELDS, { rng });
    const bindArgs = [g(operator.kp.pk), ct(operator.E), true, true];
    await session.send(pool, "exec", [reg.address, encodeFunctionData({
      abi: reg.abi, functionName: "authorizeContractBinding",
      args: [opAcct.address, ...bindArgs] })], { tag: "world:pool.authorize" });
    await session.send(reg, "bindContract", [pool.address, ...bindArgs],
      { tag: "world:pool.bind", account: opAcct });
  }

  // --- Direct BUCK stack -------------------------------------------------
  // Production worlds deploy BuckCredit, whose recipient opt-in gate
  // (setCreditIssuer) every holder passes through createCredit() below.
  // Simulation worlds that hand credits to proxies which never send a
  // transaction of their own pass creditArtifact = "BuckCreditHarness",
  // exactly as alberta_buck/sim/notes_stack.py does.
  const creditName = opts.creditArtifact ?? "BuckCredit";
  const credit = await session.deploy(artifacts(creditName), [],
    { name: creditName, gas });
  const kctrl = await session.deploy(artifacts("BuckKControllerDirect"),
    [p.kp, p.ki, p.kd, p.dt, p.kmin, p.kmax, p.k0, gov],
    { name: "BuckKControllerDirect", gas });
  // Production Buck has no basket hooks; a world that wires one of the
  // pro-rata baskets (eqworld) passes buckArtifact = "BuckWithBasketHooks",
  // the sims' subclass that keeps them.  Either way it is labelled "Buck".
  const buck = await session.deploy(artifacts(opts.buckArtifact ?? "Buck"),
    [credit.address, kctrl.address, reg.address, poolAcct],
    { name: "Buck", gas });
  await session.send(reg, "setBuck", [buck.address], { tag: "world:reg.setBuck" });
  await session.send(credit, "setBuck", [buck.address], { tag: "world:credit.setBuck" });

  return { session, artifacts, id, reg, credit, kctrl, buck, gov, poolAcct, pool, operator,
           issuer, names: { reg: regName, credit: creditName } };
}

/**
 * The world as plain data -- contract addresses, roles and the issuer's
 * keys -- for attachBuckWorld to rebuild the handles over a restored chain
 * (see snapshotTevm / restoreTevm in backends.js).  BigInts stay BigInts:
 * serialize with codec.js.
 */
export function worldRecord(world) {
  return {
    names: { ...world.names },
    addresses: {
      reg: world.reg.address, credit: world.credit.address,
      kctrl: world.kctrl.address, buck: world.buck.address,
    },
    gov: world.gov,
    poolAcct: world.poolAcct,
    pooled: !!world.pool,
    operator: world.operator?.account.address ?? null,
    issuer: { ...world.issuer },
  };
}

/** Rebuild a world's handles from worldRecord() over a chain that already
 *  holds its contracts -- no deployment, no transactions. */
export function attachBuckWorld(session, artifacts, record, opts = {}) {
  const id = opts.identity;
  if (!id) throw new Error("attachBuckWorld needs opts.identity (the buck-identity kernel API)");
  const at = (name, addr) => session.contractAt(artifacts(name).abi, addr);
  const a = record.addresses;
  return {
    pool: record.pooled ? at("SimLP", record.poolAcct) : null,
    operator: record.operator ? { account: { address: record.operator } } : null,
    session, artifacts, id,
    reg: at(record.names.reg, a.reg),
    credit: at(record.names.credit, a.credit),
    kctrl: at("BuckKControllerDirect", a.kctrl),
    buck: at("Buck", a.buck),
    gov: record.gov, poolAcct: record.poolAcct,
    issuer: { ...record.issuer },
    names: { ...record.names },
  };
}

/**
 * The issuer's half of onboarding, off-chain: the core record's canonical
 * form -> the identity scalar m -> the issuer's PS signature on m.  The
 * result is a credential card to hand to the holder; the chain learns
 * nothing at issuance.
 *
 * @param world       from buildBuckWorld (its issuer signs by default)
 * @param fields      identity KYC fields (issuer_id is stamped)
 * @param opts.issuer an issuer {addr, skX, skY} (default: world.issuer)
 * @param opts.rng    scalar drawer (default WebCrypto)
 * @returns credential {issuer, fields, canonical, m, sigma}
 */
export function issueCredential(world, fields, opts = {}) {
  const id = world.id;
  const rng = opts.rng ?? id.randScalar;
  const issuer = opts.issuer ?? world.issuer;
  const full = { ...fields, issuer_id: fields.issuer_id ?? "atb-financial-ca" };
  const canonical = id.canonicalIdentity(full);
  const m = id.identityScalar(canonical);
  const sigma = id.psSign(issuer.skX, issuer.skY, m, rng());
  return { issuer: issuer.addr, fields: full, canonical, m, sigma };
}

/** A trusted issuer's public key as the registry holds it, in kernel shapes. */
export async function issuerKey(world, issuerAddr) {
  const k = await world.session.call(world.reg, "trustedIssuerKey", [issuerAddr]);
  const p = (P) => ({ x: P.X, y: P.Y });
  const p2 = (P) => ({ x: [P.X[0], P.X[1]], y: [P.Y[0], P.Y[1]] });
  return { pkX: p2(k.X), pkY: p2(k.Y), pkY1: p(k.Y1) };
}

/**
 * The holder's half of onboarding: check the credential against the
 * issuer key the registry publishes, then the full wallet ceremony on the
 * wasm kernel -- hiding presentation, identity key, ElGamal, registration
 * proof -- verified on-chain by IdentityRegistry.register.
 *
 * @param world      from buildBuckWorld
 * @param account    a viem account (signs the register tx)
 * @param credential from issueCredential (or a decoded card)
 * @param opts.rng   scalar drawer (default WebCrypto)
 * @returns handle {account, fields, canonical, m, M, kp:{sk, pk}, E, issuer}
 */
export async function registerWallet(world, account, credential, opts = {}) {
  const id = world.id;
  const rng = opts.rng ?? id.randScalar;
  const { issuer, fields, canonical, m, sigma } = credential;
  // The card must be self-consistent and signed by a key the chain trusts;
  // refuse here, before any transaction, rather than as a failed proof.
  if (id.canonicalIdentity(fields) !== canonical || id.identityScalar(canonical) !== m) {
    throw new Error("credential does not match its own record");
  }
  if (!await world.session.call(world.reg, "isTrustedIssuer", [issuer])) {
    throw new Error(`credential issuer ${issuer} is not trusted by this registry`);
  }
  const key = await issuerKey(world, issuer);
  if (!id.psVerify(key.pkX, key.pkY, sigma, m)) {
    throw new Error("credential signature does not verify under the issuer's key");
  }

  // A' hiding presentation: fresh a, b per account; b is a NIZK witness.
  const [a, b] = [rng(), rng()];
  const pres = id.psPresent(sigma, key.pkY1, a, b);
  const sk = rng();
  const pk = id.g1Mul(id.G1, sk);
  const M = id.g1Mul(id.G1, m);
  const r = rng();
  const E = id.elgamalEncrypt(M, pk, r);
  const chainid = BigInt(await world.session.client.getChainId());
  const registry = BigInt(world.reg.address);
  const proof = id.registrationProve(
    pres, b, m, r, pk, E, BigInt(account.address), sk, chainid,
    registry,
    rng(), rng(), rng(), rng());

  await world.session.send(world.reg, "register", [
    issuer, g(pk), ct(E),
    { A: g(pres.A), B: g(pres.B) },
    { e: proof.e, s_m: proof.s_m, s_b: proof.s_b, s_r: proof.s_r, s_sk: proof.s_sk,
      C1: g(proof.C1), T_C: g(proof.T_C), T_R: g(proof.T_R),
      T_key: g(proof.T_key) },
  ], { tag: `onboard:${fields.given_name ?? account.address}`, gas: 3_000_000n,
       account });

  return { account, fields, canonical, m, M, kp: { sk, pk }, E, issuer };
}

/**
 * Register `account` with a REAL cryptographic identity: the issuer's half
 * (issueCredential) then the holder's (registerWallet), in one call.
 *
 * @param world    from buildBuckWorld
 * @param account  a viem account (signs the register tx)
 * @param fields   identity KYC fields (issuer_id is stamped)
 * @param opts.rng scalar drawer (default WebCrypto)
 * @returns handle {account, fields, canonical, m, M, kp:{sk, pk}, E, issuer}
 */
export async function onboard(world, account, fields, opts = {}) {
  return registerWallet(world, account, issueCredential(world, fields, opts), opts);
}

/**
 * The identity-approve handshake: `from` re-encrypts their REGISTERED
 * identity under `to`'s key with a Chaum-Pedersen equality proof
 * (IdentityRegistry.verifyApprove), storing the receipt fragment that
 * gates Buck transfers.  Private<->private payments need BOTH directions
 * (each party approves the other); a public counterparty is exempt.
 *
 * @returns the re-encryption {R, C} handed to `to` (their decryption key
 *          recovers `from.M` -- the bilateral-disclosure half).
 */
export async function identityApprove(world, from, to, opts = {}) {
  const id = world.id;
  const rng = opts.rng ?? id.randScalar;
  const chainid = BigInt(await world.session.client.getChainId());
  const registry = BigInt(world.reg.address);
  const rPrime = rng();
  const eForTo = id.elgamalEncrypt(from.M, to.kp.pk, rPrime);
  const proof = id.chaumPedersenProve(
    from.E, eForTo, from.kp.pk, to.kp.pk, from.kp.sk, rPrime,
    BigInt(from.account.address), BigInt(to.account.address), chainid,
    registry, rng(), rng());
  await world.session.send(world.buck, "approve", [
    to.account.address, opts.allowance ?? 0n, ct(eForTo),
    { e: proof.e, s1: proof.s1, s2: proof.s2,
      T1: g(proof.T1), T2: g(proof.T2), T3: g(proof.T3) },
  ], { tag: `approve:${from.fields.given_name}->${to.fields.given_name}`,
       gas: 1_000_000n, account: from.account });
  return eForTo;
}

/** BuckCredit.DepreciationType, by name. */
export const DEPRECIATION = { NONE: 0, LINEAR: 1, DECLINING_BALANCE: 2 };

// A holder handle (from onboard/registerWallet) or a bare viem account.
const acctOf = (who) => who.account ?? who;

/**
 * Insure an asset: the holder accepts the insurer (BuckCredit's recipient
 * opt-in, skipped when already given), then the insurer issues the
 * BuckCredit NFT.  Nothing is activated: see activateCredit.
 *
 * @param world   from buildBuckWorld
 * @param insurer a viem account (default: the session's deployer)
 * @param holder  a handle or viem account (receives the NFT)
 * @param terms   {face, assetClass=0, floor=0n, depType=NONE, depRate=0
 *                (basis points a year), depStartAt=now, premiumRate=0}
 * @returns the new token id
 */
export async function insureAsset(world, insurer, holder, terms) {
  const s = world.session;
  const ins = insurer ?? s.account;
  const h = acctOf(holder);
  // A credit only lands where its recipient asked for it: the holder names
  // the issuing account as one it accepts before the credit can be issued.
  if (!await s.call(world.credit, "acceptsCreditFrom", [h.address, ins.address])) {
    await s.send(world.credit, "setCreditIssuer", [ins.address, true],
      { tag: "credit:accept", account: h });
  }
  const now = (await s.client.getBlock()).timestamp;
  const rcpt = await s.send(world.credit, "createCredit",
    [h.address, terms.assetClass ?? 0, terms.face,
     terms.floor ?? 0n, terms.depType ?? DEPRECIATION.NONE, terms.depRate ?? 0,
     terms.depStartAt ?? now, terms.premiumRate ?? 0],
    { tag: "credit:create", account: ins });
  const [ev] = parseEventLogs({ abi: world.credit.abi, logs: rcpt.logs,
                                eventName: "CreditCreated" });
  return ev.args.tokenId;
}

/**
 * Activate `amount` of the holder's insured credit: Buck.mint, which walks
 * the holder's credits cheapest-first (or `opts.tokenIds`).  A zero-premium
 * credit activates headroom only; a premium pays its principal into the
 * insurance pool from the holder's balance.
 *
 * @returns what the Minted event reports: {coverage (face activated -- a
 *          depreciating credit needs a little more face than the present
 *          value asked for), premium (paid into the pool), creditValue
 *          (before), buckK, newLimit}
 */
export async function activateCredit(world, holder, amount, opts = {}) {
  const h = acctOf(holder);
  const args = opts.tokenIds ? [amount, opts.tokenIds] : [amount];
  const rcpt = await world.session.send(world.buck, "mint", args,
    { tag: "credit:activate", account: h, gas: 3_000_000n });
  const [ev] = parseEventLogs({ abi: world.buck.abi, logs: rcpt.logs, eventName: "Minted" });
  const a = ev.args;
  return { coverage: a.amount, premium: a.premium, creditValue: a.creditValue,
           buckK: a.buckKValue, newLimit: a.newLimit };
}

/**
 * Create a BuckCredit NFT for `holder` and activate its face through
 * Buck.mint (zero premium: activates credit headroom; BUCK circulates
 * when the holder draws by transferring).  The deployer insures.
 */
export async function createCredit(world, holder, face, opts = {}) {
  const tokenId = await insureAsset(world, world.session.account, holder,
    { ...opts, face });
  await activateCredit(world, holder, face);
  return tokenId;
}

/** One credit as its holder and insurer see it: the terms, what is
 *  activated, and today's depreciated values. */
export async function creditView(world, tokenId) {
  const s = world.session;
  const [c, holder, depreciatedFace, currentValue] = await Promise.all([
    s.call(world.credit, "credits", [tokenId]),
    s.call(world.credit, "ownerOf", [tokenId]),
    s.call(world.credit, "depreciatedFaceValue", [tokenId]),
    s.call(world.credit, "currentValue", [tokenId]),
  ]);
  // The credits() getter's tuple, by position: BuckCredit.CreditParams order.
  const [insurer, assetClass, createdAt, face, floor, depType, depRate, depStartAt,
         premiumRate, lastUpdated, activated] = c;
  return {
    tokenId: BigInt(tokenId), holder, insurer, assetClass,
    face: BigInt(face), floor: BigInt(floor), depType, depRate,
    depStartAt: BigInt(depStartAt), premiumRate,
    createdAt: BigInt(createdAt), lastUpdated: BigInt(lastUpdated),
    activated: BigInt(activated),
    depreciatedFace, currentValue,
  };
}

/** `owner`'s credits in the order Buck.mint(amount) draws them: cheapest
 *  premium first, ties in holding order (Buck._selectCheapest). */
export async function cheapestFirst(world, owner) {
  const ids = await creditsOf(world, owner);
  const rates = await Promise.all(ids.map(async (tid) =>
    (await world.session.call(world.credit, "creditInfo", [tid]))[2]));
  return ids.map((tid, i) => [tid, rates[i]])
    .sort((a, b) => a[1] - b[1])                 // stable: ties keep their order
    .map(([tid]) => tid);
}

/**
 * What activating `amount` would take (Buck.quoteMint), and whether the
 * funding gate lets it through: the premium's pool principal times the
 * controller's funding factor must already be covered by the holder's
 * balance (held BUCK plus unused credit) before the mint.
 *
 * @param opts.tokenIds the credits to draw (default: as Buck.mint does)
 * @returns {coverage (face to activate), principal (the premium, paid into
 *          the insurance pool), factor (1e18 scale), required, balance,
 *          shortfall, tokenIds}
 */
export async function quoteActivation(world, holder, amount, opts = {}) {
  const s = world.session;
  const addr = typeof holder === "string" ? holder : acctOf(holder).address;
  const tokenIds = opts.tokenIds ?? await cheapestFirst(world, addr);
  const [[coverage, principal], factor, balance] = await Promise.all([
    s.call(world.buck, "quoteMint", [amount, tokenIds]),
    s.call(world.kctrl, "fundingFactor"),
    s.call(world.buck, "balanceOf", [addr]),
  ]);
  const required = factor > 0n && principal > 0n ? (principal * factor) / 10n ** 18n : 0n;
  return { coverage, principal, factor, required, balance,
           shortfall: required > balance ? required - balance : 0n, tokenIds };
}

/** The token ids of every credit `owner` holds. */
export async function creditsOf(world, owner) {
  const s = world.session;
  const n = await s.call(world.credit, "balanceOf", [owner]);
  const ids = [];
  for (let i = 0n; i < n; i++) {
    ids.push(await s.call(world.credit, "tokenOfOwnerByIndex", [owner, i]));
  }
  return ids;
}

/** An account as its wallet shows it: registration, balances (signed:
 *  negative is credit drawn), credit limit and demurrage owing. */
export async function accountView(world, address) {
  const s = world.session;
  const [verified, balance, signedBalance, creditLimit, feeOwing, eth] = await Promise.all([
    s.call(world.reg, "isVerified", [address]),
    s.call(world.buck, "balanceOf", [address]),
    s.call(world.buck, "signedBalanceOf", [address]),
    s.call(world.buck, "creditLimit", [address]),
    s.call(world.buck, "feeOwing", [address]),
    s.client.getBalance({ address }),
  ]);
  return { address, verified, balance, signedBalance, creditLimit, feeOwing, eth };
}

/** Send `wei` from `account` to `to`: a plain value transfer, which works on
 *  tevm and anvil alike.  Returns the receipt. */
export async function sendEth(world, account, to, wei) {
  const hash = await world.session.client.sendTransaction({
    account, to, value: wei, gas: 21_000n, chain: null, ...world.session.txOverrides,
  });
  return world.session.client.waitForTransactionReceipt({ hash });
}

/** Fund a fresh account with ETH from the deployer (new demo citizens need
 *  gas money). */
export async function fundAccount(world, to, wei = 10n ** 19n) {
  await sendEth(world, world.session.account, to, wei);
}

/** Jump the chain clock forward and mine one block. */
export async function advanceTime(world, seconds) {
  const now = (await world.session.client.getBlock()).timestamp;
  await world.session.client.request({
    method: "evm_setNextBlockTimestamp",
    params: [Number(now) + Number(seconds)],
  });
  await world.session.client.request({ method: "evm_mine", params: [] });
  return (await world.session.client.getBlock()).timestamp;
}
