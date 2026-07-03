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
// Deterministic by injection: pass opts.rng (a () => bigint scalar drawer)
// to reproduce a world; the default draws WebCrypto.

import * as id from "./identity.js";

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

/**
 * Deploy the BUCK identity + monetary stack.
 *
 * @param session   a Session (deployer = session.account)
 * @param artifacts (name) => {abi, bytecode}
 * @param opts.gov      governance address    (default: deployer)
 * @param opts.poolAcct funding-pool address  (default: deployer)
 * @param opts.params   controller overrides over DEPLOY_DEFAULTS
 * @param opts.rng      scalar drawer for the issuer PS keypair
 * @returns world {session, artifacts, reg, credit, kctrl, buck, gov,
 *                 poolAcct, issuer:{addr, skX, skY, pkX, pkY}}
 */
export async function buildBuckWorld(session, artifacts, opts = {}) {
  const gas = 15_000_000n;
  const gov = opts.gov ?? session.account.address;
  const poolAcct = opts.poolAcct ?? session.account.address;
  const p = { ...DEPLOY_DEFAULTS, ...(opts.params ?? {}) };
  const rng = opts.rng ?? id.randScalar;

  // --- identity layer (deploy.py order) ---------------------------------
  const reg = await session.deploy(artifacts("IdentityRegistry"), [gov],
    { name: "IdentityRegistry", gas });
  const [skX, skY] = [rng(), rng()];
  const issuer = {
    addr: "0x" + "15".repeat(20),   // the trust-anchor address
    skX, skY,
    pkX: id.g2Mul(id.G2, skX),
    pkY: id.g2Mul(id.G2, skY),
  };
  await session.send(reg, "trustIssuer",
    [issuer.addr, { X: g2(issuer.pkX), Y: g2(issuer.pkY) }],
    { tag: "world:trustIssuer" });

  // --- Direct BUCK stack -------------------------------------------------
  const credit = await session.deploy(artifacts("BuckCredit"), [],
    { name: "BuckCredit", gas });
  const kctrl = await session.deploy(artifacts("BuckKControllerDirect"),
    [p.kp, p.ki, p.kd, p.dt, p.kmin, p.kmax, p.k0, gov],
    { name: "BuckKControllerDirect", gas });
  const buck = await session.deploy(artifacts("Buck"),
    [credit.address, kctrl.address, reg.address, poolAcct],
    { name: "Buck", gas });
  await session.send(reg, "setBuck", [buck.address], { tag: "world:reg.setBuck" });
  await session.send(credit, "setBuck", [buck.address], { tag: "world:credit.setBuck" });

  return { session, artifacts, reg, credit, kctrl, buck, gov, poolAcct, issuer };
}

/**
 * Register `account` with a REAL cryptographic identity: the full wallet
 * ceremony on the wasm kernel, verified on-chain by IdentityRegistry.
 *
 * @param world    from buildBuckWorld
 * @param account  a viem account (signs the register tx)
 * @param fields   identity KYC fields (issuer_id is stamped)
 * @param opts.rng scalar drawer (default WebCrypto)
 * @returns handle {account, fields, canonical, m, M, kp:{sk, pk}, E}
 */
export async function onboard(world, account, fields, opts = {}) {
  const rng = opts.rng ?? id.randScalar;
  const full = { ...fields, issuer_id: fields.issuer_id ?? "atb-financial-ca" };
  const canonical = id.canonicalIdentity(full);
  const m = id.identityScalar(canonical);

  const sigma = id.psSign(world.issuer.skX, world.issuer.skY, m, rng());
  const sigmaP = id.psRerandomize(sigma, rng());
  const sk = rng();
  const pk = id.g1Mul(id.G1, sk);
  const M = id.g1Mul(id.G1, m);
  const r = rng();
  const E = id.elgamalEncrypt(M, pk, r);
  const proof = id.registrationProve(
    sigmaP, m, r, pk, E, BigInt(account.address), rng(), rng());

  await world.session.send(world.reg, "register", [
    world.issuer.addr, g(pk), ct(E),
    { sigma_1: g(sigmaP.sigma_1), sigma_2: g(sigmaP.sigma_2) },
    { e: proof.e, s_m: proof.s_m, s_r: proof.s_r,
      A_ps: g(proof.A_ps), T_C: g(proof.T_C), T_R: g(proof.T_R) },
  ], { tag: `onboard:${full.given_name ?? account.address}`, gas: 3_000_000n,
       account });

  return { account, fields: full, canonical, m, M, kp: { sk, pk }, E };
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
  const rng = opts.rng ?? id.randScalar;
  const chainid = BigInt(await world.session.client.getChainId());
  const rPrime = rng();
  const eForTo = id.elgamalEncrypt(from.M, to.kp.pk, rPrime);
  const proof = id.chaumPedersenProve(
    from.E, eForTo, from.kp.pk, to.kp.pk, from.kp.sk, rPrime,
    BigInt(from.account.address), BigInt(to.account.address), chainid,
    rng(), rng());
  await world.session.send(world.buck, "approve", [
    to.account.address, opts.allowance ?? 0n, ct(eForTo),
    { e: proof.e, s1: proof.s1, s2: proof.s2,
      T1: g(proof.T1), T2: g(proof.T2), T3: g(proof.T3) },
  ], { tag: `approve:${from.fields.given_name}->${to.fields.given_name}`,
       gas: 1_000_000n, account: from.account });
  return eForTo;
}

/**
 * Create a BuckCredit NFT for `holder` and activate its face through
 * Buck.mint (zero premium: activates credit headroom; BUCK circulates
 * when the holder draws by transferring).
 */
export async function createCredit(world, holder, face, opts = {}) {
  const now = (await world.session.client.getBlock()).timestamp;
  await world.session.send(world.credit, "createCredit",
    [holder.account.address, opts.assetClass ?? 0, face,
     opts.floor ?? 0n, opts.depType ?? 0, opts.depRate ?? 0,
     now, opts.premiumRate ?? 0],
    { tag: "world:createCredit" });
  await world.session.send(world.buck, "mint", [face],
    { tag: "world:activate", account: holder.account, gas: 3_000_000n });
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
