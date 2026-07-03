// The buckworld demo CONTROLLER -- all the logic, none of the DOM.
//
// main.js (the page) constructs it with browser-loaded dependencies
// (tevmSession, loadIdentity, the bundled artifacts); the node gate
// (test/app.tevm.test.js) drives the SAME class with node-loaded ones.
// Every mutation goes through the Session, so the journal records the
// whole interactive story -- including the protocol's refusals.

import {
  buildBuckWorld, onboard, identityApprove, createCredit,
  fundAccount, advanceTime, DAY,
} from "../../src/buckworld.js";

/** Rotating sample identities (unicode deliberately represented). */
export const SAMPLE_CITIZENS = [
  { given_name: "Chloé", family_name: "Bélanger-李",
    id_type: "Alberta Identity Card", id_number: "AIC-2026-0007744" },
  { given_name: "Bob", family_name: "Smith",
    id_type: "Corporate Registration", id_number: "AB-CORP-2026-00182" },
  { given_name: "Zoë", family_name: "Müller",
    id_type: "Alberta Identity Card", id_number: "AIC-2026-0011830" },
  { given_name: "花子", family_name: "田中",
    id_type: "Alberta Identity Card", id_number: "AIC-2026-0021871" },
  { given_name: "Amir", family_name: "Haddad",
    id_type: "Alberta Identity Card", id_number: "AIC-2026-0034192" },
];

export class BuckWorldApp {
  /**
   * @param deps.session       a Session (deployer account)
   * @param deps.identity      the buck-identity kernel API
   * @param deps.artifacts     (name) => {abi, bytecode}
   * @param deps.makeAccount   () => viem account for a new citizen
   * @param deps.rng           optional scalar drawer (tests inject)
   * @param deps.math          optional buck-math wasm (demurrage checks)
   */
  constructor({ session, identity, artifacts, makeAccount, rng, math = null }) {
    this.session = session;
    this.identity = identity;
    this.artifacts = artifacts;
    this.makeAccount = makeAccount;
    this.rng = rng ?? identity.randScalar;
    this.math = math;
    this.world = null;
    this.citizens = [];            // {name, fields, handle, touched:{}}
    this.approved = new Set();     // "from->to" fragments recorded
  }

  async boot() {
    this.world = await buildBuckWorld(this.session, this.artifacts,
      { identity: this.identity, rng: this.rng });
    return this.world;
  }

  citizen(name) {
    const c = this.citizens.find((c) => c.name === name);
    if (!c) throw new Error(`no such citizen: ${name}`);
    return c;
  }

  /** Register a new citizen: fresh funded account + the FULL kernel
   *  ceremony verified on-chain.  Returns the roster entry. */
  async addCitizen(fields) {
    const account = this.makeAccount();
    await fundAccount(this.world, account.address);
    const stamped = {
      jurisdiction: "Alberta, Canada",
      date_of_birth: "1990-01-01",
      issued_at: "2026-07-03T00:00:00Z",
      epoch: 42,
      ...fields,
    };
    const handle = await onboard(this.world, account, stamped, { rng: this.rng });
    const c = {
      name: `${stamped.given_name} ${stamped.family_name}`,
      fields: stamped,
      handle,
    };
    this.citizens.push(c);
    return c;
  }

  /** The bilateral identity-approve handshake between two citizens
   *  (each re-encrypts their registered identity for the other). */
  async approvePair(a, b) {
    await identityApprove(this.world, a.handle, b.handle, { rng: this.rng });
    await identityApprove(this.world, b.handle, a.handle, { rng: this.rng });
    this.approved.add(`${a.name}->${b.name}`);
    this.approved.add(`${b.name}->${a.name}`);
  }

  /** Insure a credit line: BuckCredit NFT + face activation. */
  async credit(c, face) {
    await createCredit(this.world, c.handle, face);
  }

  /** Attempt a payment.  Refusals are part of the story: they come back
   *  as {ok:false, reason} (and land in the journal as mismatches). */
  async pay(from, to, amount) {
    try {
      await this.session.send(this.world.buck, "transfer",
        [to.handle.account.address, amount],
        { tag: `pay:${from.name}->${to.name}`, gas: 1_000_000n,
          account: from.handle.account });
      return { ok: true };
    } catch {
      return { ok: false, reason: this.session.lastRevertReason };
    }
  }

  /** Advance the simulated clock. */
  async jump(seconds) {
    return advanceTime(this.world, seconds);
  }

  /** Chain truth for the roster (+ the clock). */
  async snapshot() {
    const buck = this.world.buck;
    const out = { clock: (await this.session.client.getBlock()).timestamp,
                  citizens: [] };
    for (const c of this.citizens) {
      const a = c.handle.account.address;
      out.citizens.push({
        name: c.name,
        address: a,
        balance: await this.session.call(buck, "balanceOf", [a]),
        signed: await this.session.call(buck, "signedBalanceOf", [a]),
        feeOwing: await this.session.call(buck, "feeOwing", [a]),
        creditLimit: await this.session.call(buck, "creditLimit", [a]),
      });
    }
    return out;
  }

  /** buck-math kernel demurrage prediction for a FRESH receipt (bs = 0 at
   *  the receipt block): the page's bit-exact badge.  null without math. */
  predictFee(raw, elapsed) {
    return this.math ? this.math.fee_owing(0n, raw, elapsed) : null;
  }

  static DAY = DAY;
}
