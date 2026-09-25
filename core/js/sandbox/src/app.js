// The sandbox CONTROLLER -- all the chain logic, none of the DOM.
//
// One private world in the visitor's tab: an issuer that certifies people,
// wallets that register and pay, an insurer that turns simulated assets
// into credit, and an observer's view of everything the chain shows.
// main.js (the page) constructs it with browser-loaded dependencies; the
// node gate (test/sandbox.app.tevm.test.js) drives the SAME class with
// node-loaded ones.
//
// Every action runs one at a time, then the app decodes the new blocks for
// the observer, saves the world (the chain's state and clock, and the app's
// own records) to its store, and tells its listeners.  A refusal by the
// protocol throws a SandboxError carrying the contract's reason; the world
// is saved all the same, the failed transaction included.
//
// Everything here is simulated: the issuer's signing keys, every wallet's
// keys and every credential live in the saved world, in the clear.

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import {
  buildBuckWorld, attachBuckWorld, worldRecord, issueCredential, registerWallet,
  identityApprove, insureAsset, activateCredit, creditView, accountView,
  fundAccount, advanceTime, DEPRECIATION, DAY,
} from "../../src/buckworld.js";
import { snapshotTevm, restoreTevm } from "../../src/backends.js";
import { observe } from "../../src/observer.js";
import { encodeJSON, decodeJSON } from "../../src/codec.js";
import { JournalWriter } from "../../src/journal.js";

export const FORMAT = "alberta-buck-sandbox";
export const VERSION = 1;
export const BUCK = 1_000_000n;                  // 6 decimals

/** The issuer's standing record stamps (the privacy paper's cast, alberta_buck/sim/cast.py). */
export const ISSUER = {
  name: "Alberta Identity (simulated)",
  stamp: {
    jurisdiction: "Alberta, Canada", id_type: "Alberta Identity",
    issuer_id: "alberta-identity", epoch: 42,
  },
};

/** The insurer every new world starts with. */
export const INSURER_LABEL = "Sandbox Mutual";

/**
 * Asset classes: sandbox labels over BuckCredit's opaque uint8, each with a
 * sensible default schedule.  depRate is basis points a year (of the part
 * above the floor, for LINEAR); floorBp is the floor as basis points of face.
 */
export const ASSET_CLASSES = [
  { code: 1, key: "home", label: "Home", depType: DEPRECIATION.LINEAR, depRate: 250, floorBp: 3_000,
    note: "the building wears out over 40 years; the land under it (30 %) does not" },
  { code: 2, key: "vehicle", label: "Vehicle", depType: DEPRECIATION.DECLINING_BALANCE,
    depRate: 1_500, floorBp: 1_000, note: "loses 15 % of its value a year, down to scrap (10 %)" },
  { code: 3, key: "equipment", label: "Equipment", depType: DEPRECIATION.DECLINING_BALANCE,
    depRate: 1_000, floorBp: 500, note: "loses 10 % of its value a year, down to 5 %" },
  { code: 4, key: "farmland", label: "Farmland", depType: DEPRECIATION.NONE, depRate: 0,
    floorBp: 0, note: "does not wear out" },
  { code: 5, key: "gold", label: "Gold", depType: DEPRECIATION.NONE, depRate: 0,
    floorBp: 0, note: "does not wear out" },
];

export const assetClass = (codeOrKey) => {
  const c = ASSET_CLASSES.find((a) => a.code === codeOrKey || a.key === codeOrKey);
  if (!c) throw new SandboxError(`no such asset class: ${codeOrKey}`);
  return c;
};

/** A refusal, by the protocol or by the sandbox: `reason` is for people. */
export class SandboxError extends Error {
  constructor(reason) {
    super(reason);
    this.name = "SandboxError";
    this.reason = reason;
  }
}

const isoOf = (ts) => new Date(Number(ts) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");

export class SandboxApp {
  /**
   * @param deps.identity   the buck-identity kernel API
   * @param deps.artifacts  (name) => {abi, bytecode}
   * @param deps.newSession async () => a FRESH tevm Session (deployer account)
   * @param deps.store      {load, save, clear} of JSON text (store.js), or null
   * @param deps.rng        scalar drawer (tests inject; default WebCrypto)
   * @param deps.newKey     () => a private key for a new account (tests inject)
   * @param deps.journalTail how many journal entries the world keeps
   */
  constructor({ identity, artifacts, newSession, store = null, rng, newKey,
                journalTail = 500 }) {
    this.identity = identity;
    this.artifacts = artifacts;
    this.newSession = newSession;
    this.store = store;
    this.rng = rng ?? identity.randScalar;
    this.newKey = newKey ?? generatePrivateKey;
    this.journalTail = journalTail;
    this.session = null;
    this.world = null;
    this.state = null;
    this.notice = "";                    // e.g. why a saved world was not restored
    this.#accounts = new Map();
    this.#listeners = new Set();
    this.#queue = Promise.resolve();
  }

  #accounts;
  #listeners;
  #queue;

  /** Restore the stored world, or deploy a fresh one. */
  static async open(deps) {
    const app = new SandboxApp(deps);
    const text = deps.store ? await deps.store.load() : null;
    if (text) {
      try {
        await app.#run(() => app.#load(text));
        return app;
      } catch (e) {
        app.notice = `The saved world could not be restored (${e.message}); started a new one.`;
      }
    }
    await app.reset();
    return app;
  }

  /** Call `fn(app)` after every action; returns the unsubscribe. */
  onChange(fn) {
    this.#listeners.add(fn);
    return () => this.#listeners.delete(fn);
  }

  // ---- the world ------------------------------------------------------------

  /** A fresh world: deploy, trust the issuer, open Sandbox Mutual. */
  reset() {
    return this.#run(async () => {
      const session = await this.newSession();
      const journal = [];
      this.#attachJournal(session, journal);
      const world = await buildBuckWorld(session, this.artifacts,
        { identity: this.identity, rng: this.rng });
      const head = await session.client.getBlock();
      this.session = session;
      this.world = world;
      this.#accounts.clear();
      this.state = {
        world: worldRecord(world),
        origin: head.timestamp,
        issued: [],                      // person numbers, never reused
        credentials: [],
        wallets: [],
        insurers: [],
        credits: [],
        introduced: [],                  // "W1>W2": W1 has identity-approved W2
        observed: [],
        observedTo: -1n,
        journal,
      };
      await this.#newInsurer(INSURER_LABEL);
      await this.#settle();
    });
  }

  /** The whole world as JSON text: the file Export writes. */
  exportText() {
    return this.#run(() => this.#serialize());
  }

  /** Replace the world with an exported one.  A bad file leaves the current
   *  world untouched. */
  importText(text) {
    return this.#run(async () => {
      await this.#load(text);
      await this.#save();
      this.#emit();
    });
  }

  /** Move the clock forward and mine a block. */
  advance(seconds) {
    return this.#act(() => advanceTime(this.world, seconds));
  }

  // ---- the issuer -----------------------------------------------------------

  /**
   * Certify a person: the core record (name at first certification, date of
   * birth, a person number the registry never reuses, today's date) signed
   * into a credential card.  Off-chain: no transaction.
   *
   * @param person {given_name, family_name, date_of_birth}
   * @returns the credential record {id, card, issuedAt, wallets}
   */
  issue(person) {
    return this.#act(async () => {
      for (const k of ["given_name", "family_name", "date_of_birth"]) {
        if (!String(person[k] ?? "").trim()) throw new SandboxError(`${k.replace("_", " ")} is required`);
      }
      if (!/^\d{4}-\d{2}-\d{2}$/.test(person.date_of_birth)) {
        throw new SandboxError("date of birth must be YYYY-MM-DD");
      }
      const now = (await this.session.client.getBlock()).timestamp;
      const fields = {
        ...ISSUER.stamp,
        given_name: person.given_name.trim(),
        family_name: person.family_name.trim(),
        date_of_birth: person.date_of_birth,
        id_number: this.#personNumber(),
        issued_at: isoOf(now),
      };
      const card = issueCredential(this.world, fields, { rng: this.rng });
      return this.#addCredential(card, now);
    });
  }

  /** A credential card as the JSON a holder would carry away. */
  credentialText(credentialId) {
    return encodeJSON(this.#credential(credentialId).card, 2);
  }

  /** Accept a card from its JSON (pasted or a file).  The same card twice
   *  is the same credential. */
  importCredential(text) {
    return this.#act(async () => {
      let card;
      try {
        card = decodeJSON(text);
      } catch {
        throw new SandboxError("that is not a credential card (not JSON)");
      }
      const shape = card && typeof card.issuer === "string" && card.fields
        && typeof card.canonical === "string" && typeof card.m === "bigint"
        && card.sigma?.sigma_1 && card.sigma?.sigma_2;
      if (!shape) throw new SandboxError("that is not a credential card");
      const same = this.state.credentials.find((c) => c.card.canonical === card.canonical
        && c.card.sigma.sigma_1.x === card.sigma.sigma_1.x);
      return same ?? this.#addCredential(card, null);
    });
  }

  // ---- wallets --------------------------------------------------------------

  /** A new wallet: a fresh account, given gas money.  Not yet registered. */
  createWallet(label) {
    return this.#act(async () => {
      const privateKey = this.newKey();
      const address = this.#account(privateKey).address;
      await fundAccount(this.world, address);
      const w = { id: `W${this.state.wallets.length + 1}`, label: String(label || "").trim()
                  || `Wallet ${this.state.wallets.length + 1}`, privateKey, address,
                  credential: null, handle: null };
      this.state.wallets.push(w);
      return w;
    });
  }

  /** Register a wallet with a credential: the identity is proved on chain
   *  without revealing whose it is. */
  register(walletId, credentialId) {
    return this.#act(async () => {
      const w = this.#wallet(walletId);
      if (w.handle) throw new SandboxError(`${w.label} is already registered`);
      const c = this.#credential(credentialId);
      const { account, ...handle } = await this.#refusing(() =>
        registerWallet(this.world, this.#account(w.privateKey), c.card, { rng: this.rng }));
      w.handle = handle;
      w.credential = c.id;
      c.wallets.push(w.id);
      return w;
    });
  }

  /** The identity handshake, both ways: each wallet re-encrypts its identity
   *  for the other, which private payments between them require. */
  introduce(aId, bId) {
    return this.#act(async () => {
      const a = this.#registered(aId);
      const b = this.#registered(bId);
      if (a.id === b.id) throw new SandboxError("a wallet does not introduce itself");
      for (const [from, to] of [[a, b], [b, a]]) {
        const key = `${from.id}>${to.id}`;
        if (this.state.introduced.includes(key)) continue;
        await this.#refusing(() => identityApprove(this.world, this.#handle(from),
          this.#handle(to), { rng: this.rng }));
        this.state.introduced.push(key);
      }
    });
  }

  /** Pay `amount` (base units: 6 decimals) to a wallet id or an address. */
  send(fromId, to, amount) {
    return this.#act(async () => {
      const from = this.#wallet(fromId);
      const toAddr = /^0x[0-9a-fA-F]{40}$/.test(to) ? to : this.#wallet(to).address;
      await this.#refusing(() => this.session.send(this.world.buck, "transfer",
        [toAddr, BigInt(amount)],
        { account: this.#account(from.privateKey), gas: 1_000_000n,
          tag: `pay:${from.id}->${to}` }));
    });
  }

  // ---- credit ---------------------------------------------------------------

  /**
   * Insure an asset for a wallet: the holder accepts the insurer, the insurer
   * issues a BuckCredit NFT.  Terms default from the asset class.
   *
   * @param terms {assetClass (code or key), face, floor?, depType?, depRate?,
   *               premiumRate? (basis points a year), insurer? (id)}
   * @returns the token id
   */
  insure(walletId, terms) {
    return this.#act(async () => {
      const w = this.#registered(walletId);
      const cls = assetClass(terms.assetClass ?? "home");
      const face = BigInt(terms.face ?? 0);
      if (face <= 0n) throw new SandboxError("face value must be positive");
      const ins = this.#insurer(terms.insurer ?? this.state.insurers[0].id);
      const tokenId = await this.#refusing(() => insureAsset(this.world,
        this.#account(ins.privateKey), this.#account(w.privateKey), {
          face, assetClass: cls.code,
          floor: terms.floor ?? face * BigInt(cls.floorBp) / 10_000n,
          depType: terms.depType ?? cls.depType,
          depRate: terms.depRate ?? cls.depRate,
          premiumRate: terms.premiumRate ?? 0,
        }));
      this.state.credits.push({ tokenId, wallet: w.id, insurer: ins.id, assetClass: cls.key });
      return tokenId;
    });
  }

  /** Activate `amount` of a wallet's insured credit (Buck.mint). */
  activate(walletId, amount, tokenIds) {
    return this.#act(async () => {
      const w = this.#registered(walletId);
      return this.#refusing(() => activateCredit(this.world, this.#account(w.privateKey),
        BigInt(amount), tokenIds ? { tokenIds } : {}));
    });
  }

  // ---- what the screens show ----------------------------------------------

  /** Everything the tools render, read from the chain in one pass. */
  view() {
    return this.#run(async () => {
      const s = this.session;
      const w = this.world;
      const head = await s.client.getBlock();
      const [buckK, supply] = await Promise.all([
        s.call(w.kctrl, "buckK"), s.call(w.buck, "totalSupply"),
      ]);
      const wallets = [];
      for (const x of this.state.wallets) {
        wallets.push({
          id: x.id, label: x.label, credential: x.credential, registered: !!x.handle,
          introduced: this.state.introduced.filter((k) => k.startsWith(`${x.id}>`))
            .map((k) => k.split(">")[1]),
          ...await accountView(w, x.address),
        });
      }
      const credits = [];
      for (const c of this.state.credits) {
        credits.push({ ...await creditView(w, c.tokenId), wallet: c.wallet,
                       insurerId: c.insurer, className: c.assetClass });
      }
      return {
        status: {
          block: head.number, timestamp: head.timestamp, date: isoOf(head.timestamp),
          day: (head.timestamp - this.state.origin) / BigInt(DAY),
          buckK, supply, wallets: this.state.wallets.length,
        },
        issuer: { name: ISSUER.name, address: w.issuer.addr },
        credentials: this.state.credentials.map((c) => ({
          id: c.id, name: `${c.card.fields.given_name} ${c.card.fields.family_name}`,
          personNumber: c.card.fields.id_number, issuedAt: c.issuedAt,
          fields: c.card.fields, wallets: [...c.wallets],
        })),
        wallets,
        insurers: this.state.insurers.map(({ id, label, address }) => ({ id, label, address })),
        credits,
        observed: [...this.state.observed],
        journal: [...this.state.journal],
        notice: this.notice,
      };
    });
  }

  /** Who is who, for the observer's "you know this; Mallory does not" layer. */
  labels() {
    const out = { [this.world.issuer.addr]: ISSUER.name };
    for (const i of this.state.insurers) out[i.address] = i.label;
    for (const x of this.state.wallets) out[x.address] = x.label;
    const names = this.world.names;
    out[this.world.reg.address] = names.reg;
    out[this.world.credit.address] = names.credit;
    out[this.world.kctrl.address] = "BuckKControllerDirect";
    out[this.world.buck.address] = "Buck";
    return out;
  }

  // ---- internals --------------------------------------------------------------

  // One action at a time: a click during a registration waits its turn.
  #run(fn) {
    const p = this.#queue.then(fn);
    this.#queue = p.catch(() => {});
    return p;
  }

  // An action: run it, then observe, save and notify -- whether it
  // succeeded or was refused.
  #act(fn) {
    return this.#run(async () => {
      try {
        return await fn();
      } finally {
        await this.#settle();
      }
    });
  }

  async #settle() {
    await this.#observe();
    await this.#save();
    this.#emit();
  }

  #emit() {
    for (const fn of this.#listeners) {
      try {
        fn(this);
      } catch (e) {
        console.error("sandbox listener failed", e);
      }
    }
  }

  async #observe() {
    const from = this.state.observedTo + 1n;
    const head = (await this.session.client.getBlock()).number;
    if (head < from) return;
    const rows = await observe(this.world, from, { toBlock: head });
    this.state.observed.push(...rows);
    this.state.observedTo = head;
  }

  async #serialize() {
    return encodeJSON({ format: FORMAT, version: VERSION,
                        chain: await snapshotTevm(this.session), app: this.state });
  }

  async #save() {
    if (this.store) await this.store.save(await this.#serialize());
  }

  // Build the saved world in a new session; swap it in only once it stands.
  async #load(text) {
    let saved;
    try {
      saved = decodeJSON(text);
    } catch {
      throw new SandboxError("that is not a sandbox world (not JSON)");
    }
    if (saved?.format !== FORMAT || !saved.chain || !saved.app) {
      throw new SandboxError("that is not a sandbox world");
    }
    if (saved.version !== VERSION) {
      throw new SandboxError(`sandbox world version ${saved.version}; this sandbox reads ${VERSION}`);
    }
    const session = await this.newSession();
    await restoreTevm(session, saved.chain);
    const world = attachBuckWorld(session, this.artifacts, saved.app.world,
      { identity: this.identity });
    if ((await session.client.getCode({ address: world.buck.address }) ?? "0x") === "0x") {
      throw new SandboxError("the saved chain holds no Buck contract");
    }
    saved.app.journal ??= [];
    this.#attachJournal(session, saved.app.journal);
    this.session = session;
    this.world = world;
    this.state = saved.app;
    this.#accounts.clear();
  }

  // The session journals into `journal` (the world's own array, kept to its
  // tail), numbering on from its last entry.
  #attachJournal(session, journal) {
    const writer = new JournalWriter((line) => {
      journal.push(JSON.parse(line));
      const extra = journal.length - this.journalTail;
      if (extra > 0) journal.splice(0, extra);
    });
    writer.seq = journal.length ? journal[journal.length - 1].i : 0;
    session.journal = writer;
  }

  // Run a chain step; a revert becomes a SandboxError with the contract's reason.
  async #refusing(fn) {
    try {
      return await fn();
    } catch (e) {
      if (e instanceof SandboxError) throw e;
      const err = new SandboxError(this.session.lastRevertReason || e.shortMessage || e.message);
      err.cause = e;
      throw err;
    } finally {
      this.session.lastRevertReason = "";
    }
  }

  async #newInsurer(label) {
    const privateKey = this.newKey();
    const address = this.#account(privateKey).address;
    await fundAccount(this.world, address);
    const ins = { id: `I${this.state.insurers.length + 1}`, label, privateKey, address };
    this.state.insurers.push(ins);
    return ins;
  }

  #addCredential(card, issuedAt) {
    const c = { id: `C${this.state.credentials.length + 1}`, card, issuedAt, wallets: [] };
    this.state.credentials.push(c);
    return c;
  }

  // AB-P-NNNN-NNNN, drawn at random and never reused.
  #personNumber() {
    for (;;) {
      const n = (this.rng() % 100_000_000n).toString().padStart(8, "0");
      const id = `AB-P-${n.slice(0, 4)}-${n.slice(4)}`;
      if (!this.state.issued.includes(id)) {
        this.state.issued.push(id);
        return id;
      }
    }
  }

  #account(privateKey) {
    let a = this.#accounts.get(privateKey);
    if (!a) {
      a = privateKeyToAccount(privateKey);
      this.#accounts.set(privateKey, a);
    }
    return a;
  }

  #handle(w) {
    return { ...w.handle, account: this.#account(w.privateKey) };
  }

  #wallet(id) {
    const w = this.state.wallets.find((x) => x.id === id);
    if (!w) throw new SandboxError(`no such wallet: ${id}`);
    return w;
  }

  #registered(id) {
    const w = this.#wallet(id);
    if (!w.handle) throw new SandboxError(`${w.label} is not registered yet`);
    return w;
  }

  #credential(id) {
    const c = this.state.credentials.find((x) => x.id === id);
    if (!c) throw new SandboxError(`no such credential: ${id}`);
    return c;
  }

  #insurer(id) {
    const i = this.state.insurers.find((x) => x.id === id);
    if (!i) throw new SandboxError(`no such insurer: ${id}`);
    return i;
  }
}
