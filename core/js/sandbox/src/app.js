// The sandbox CONTROLLER -- all the chain logic, none of the DOM.
//
// One private world in the visitor's tab: an issuer that certifies people,
// wallets that register, pay and trade, an insurer that turns simulated
// assets into credit at a real premium, a BUCK/USDC market (src/market.js:
// production identity contracts, the unmodified Uniswap periphery, trades
// through Permit2), and an observer's view of everything the chain shows.
// main.js (the page) constructs it with browser-loaded dependencies; the
// node gate (test/sandbox.app.webevm.test.js) drives the SAME class with
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
import { keccak256 } from "viem";

import {
  buildBuckWorld, attachNetBuckWorld, worldRecord, issueCredential, registerWallet, onboard, createInsurerPool,
  identityApprove, insureAsset, activateCredit, quoteActivation, creditView, accountView,
  fundAccount, sendEth, advanceTime, DEPRECIATION, DAY,
} from "../../src/buckworld.js";
import {
  buildMarket, attachNetMarket, marketRecord, marketContracts, fiatIn, buckPrice, poolReserves,
  openTrading, tradingOpen, buyBuck, buyBuckExact, sellBuck,
} from "../../src/market.js";
import { snapshotWebevm, restoreWebevm } from "../../src/webevm.js";
import { observe } from "../../src/observer.js";
import { encodeJSON, decodeJSON } from "../../src/codec.js";
import { JournalWriter } from "../../src/journal.js";

const PROTOCOL_REASONS = {
  InsufficientMintFunding: "insufficient mint funding: buy the required BUCK reserve before issuing credit",
  SenderNotVerified: "sender not verified", RecipientNotVerified: "recipient not verified",
  SenderApprovalRequired: "sender must identity-approve recipient",
  RecipientApprovalRequired: "recipient must identity-approve sender",
  InsufficientSpendableBalance: "insufficient spendable BUCK balance",
  InsufficientNominalBalance: "insufficient nominal BUCK balance",
  InsufficientCreditAllocation: "insufficient credit capacity for this issuance",
  InsufficientCoverage: "insufficient cash or eligible credit for this retirement",
  DemurrageSponsorUnderfunded: "demurrage sponsor needs funding; fund it or detach sponsorship",
};

export const FORMAT = "alberta-buck-sandbox";
export const VERSION = 5; // the chain on webevm (its dumpState: blocks and logs kept); 4 was Tevm's
export const BUCK = 1_000_000n;                  // 6 decimals
export const USDC = 1_000_000n;                  // 6 decimals

/** What every new wallet starts with: gas money, and dollars to buy BUCK. */
export const ENDOWMENT = { eth: 10n ** 19n, usdc: 10_000n * USDC };

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
 * sensible default schedule and premium.  depRate is basis points a year (of
 * the part above the floor, for LINEAR); floorBp is the floor as basis points
 * of face; premiumRate is basis points of insured value a year.  Activating
 * credit deposits ten years' worth with the insurance pool, which invests it
 * (Buck.POOL_ROI_INV: an assumed 10 % return) to earn the premiums and returns
 * it when the insurance is dropped; so the rate must stay under 1,000.
 */
export const ASSET_CLASSES = [
  { code: 1, key: "home", label: "Home", depType: DEPRECIATION.LINEAR, depRate: 250, floorBp: 3_000,
    premiumRate: 35, note: "the building wears out over 40 years; the land under it (30 %) does not" },
  { code: 2, key: "vehicle", label: "Vehicle", depType: DEPRECIATION.DECLINING_BALANCE,
    depRate: 1_500, floorBp: 1_000, premiumRate: 300,
    note: "loses 15 % of its value a year, down to scrap (10 %)" },
  { code: 3, key: "equipment", label: "Equipment", depType: DEPRECIATION.DECLINING_BALANCE,
    depRate: 1_000, floorBp: 500, premiumRate: 100, note: "loses 10 % of its value a year, down to 5 %" },
  { code: 4, key: "farmland", label: "Farmland", depType: DEPRECIATION.NONE, depRate: 0,
    floorBp: 0, premiumRate: 20, note: "does not wear out" },
  { code: 5, key: "gold", label: "Gold", depType: DEPRECIATION.NONE, depRate: 0,
    floorBp: 0, premiumRate: 12, note: "does not wear out; vaulted and insured" },
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

/**
 * A wallet by name: its id, the label its holder gave it (if any) and the
 * registered name (when there is one to show) -- "W1", "W1: Savings",
 * "W1: Savings: Chloé Bélanger".  Another wallet shows a registered name only
 * once that wallet has introduced itself.
 */
export const walletTitle = ({ id, label, name }) => [id, label, name].filter(Boolean).join(": ");

/** What a wallet can pay in: BUCKs pass Buck's identity gate; USDC and ETH carry no identity. */
export const ASSETS = [
  { key: "BUCK", label: "BUCKs", decimals: 6, identity: true },
  { key: "USDC", label: "USDC", decimals: 6, identity: false },
  { key: "ETH", label: "ETH", decimals: 18, identity: false },
];

export class SandboxApp {
  /**
   * @param deps.identity   the buck-identity kernel API
   * @param deps.artifacts  (name) => {abi, bytecode}
   * @param deps.newSession async () => a FRESH webevm Session (deployer account)
   * @param deps.store      {load, save, clear} of JSON text (store.js), or null
   * @param deps.rng        scalar drawer (tests inject; default WebCrypto)
   * @param deps.newKey     () => a private key for a new account (tests inject)
   * @param deps.journalTail how many journal entries the world keeps
   * @param deps.onJournal  (entry) => void for every transaction as it lands
   *                        (a page shows progress while a world deploys)
   */
  constructor({ identity, artifacts, newSession, store = null, rng, newKey,
                journalTail = 500, onJournal = null }) {
    this.identity = identity;
    this.artifacts = artifacts;
    this.newSession = newSession;
    this.store = store;
    this.rng = rng ?? identity.randScalar;
    this.newKey = newKey ?? generatePrivateKey;
    this.journalTail = journalTail;
    this.onJournal = onJournal;
    this.session = null;
    this.world = null;
    this.market = null;
    this.state = null;
    this.notice = "";                    // e.g. why a saved world was not restored
    this.#accounts = new Map();
    this.#listeners = new Set();
    this.#queue = Promise.resolve();
  }

  #accounts;
  #listeners;
  #queue;
  #known = {};                           // the market's contracts, for observe()

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

  /** A fresh world: deploy, trust the issuer, open the market and Sandbox
   *  Mutual. */
  reset() {
    return this.#run(async () => {
      const session = await this.newSession();
      const journal = [];
      this.#attachJournal(session, journal);
      const world = await buildBuckWorld(session, this.artifacts,
        { identity: this.identity, rng: this.rng, accounting: "net" });
      const market = await buildMarket(world, this.artifacts, { rng: this.rng });
      const head = await session.client.getBlock();
      this.session = session;
      this.world = world;
      this.market = market;
      this.#known = marketContracts(market, this.artifacts);
      this.#accounts.clear();
      this.state = {
        world: worldRecord(world),
        market: marketRecord(market),
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

  /** A new wallet: a fresh account with the ENDOWMENT (gas money and USDC),
   *  and an optional label.  Not yet registered.  Returns {id, label, address,
   *  credential, name, title}. */
  createWallet(label) {
    return this.#act(async () => {
      const privateKey = this.newKey();
      const address = this.#account(privateKey).address;
      await fundAccount(this.world, address, ENDOWMENT.eth);
      await fiatIn(this.world, this.market, address, ENDOWMENT.usdc, { tag: "wallet:endow" });
      const w = { id: `W${this.state.wallets.length + 1}`, label: String(label ?? "").trim(),
                  privateKey, address, credential: null, handle: null };
      this.state.wallets.push(w);
      return this.#summary(w);
    });
  }

  /** Register a wallet with a credential: the identity is proved on chain
   *  without revealing whose it is. */
  register(walletId, credentialId) {
    return this.#act(async () => {
      const w = this.#wallet(walletId);
      if (w.handle) throw new SandboxError(`${this.#title(w)} is already registered`);
      const c = this.#credential(credentialId);
      const { account, ...handle } = await this.#refusing(() =>
        registerWallet(this.world, this.#account(w.privateKey), c.card, { rng: this.rng }));
      w.handle = handle;
      w.credential = c.id;
      c.wallets.push(w.id);
      return this.#summary(w);
    });
  }

  /** An introduction, one way: `from` re-encrypts its identity for `to`
   *  alone, so `to` can later say whom it dealt with -- and learns the
   *  registered name.  Paying BUCKs between two private wallets takes one
   *  each way.  Returns false when `from` had already introduced itself. */
  introduce(fromId, toId) {
    return this.#act(async () => {
      const from = this.#registered(fromId);
      const to = this.#registered(toId);
      if (from.id === to.id) throw new SandboxError("a wallet does not introduce itself to itself");
      const key = `${from.id}>${to.id}`;
      if (this.state.introduced.includes(key)) return false;
      await this.#refusing(() => identityApprove(this.world, this.#handle(from),
        this.#handle(to), { rng: this.rng }));
      this.state.introduced.push(key);
      return true;
    });
  }

  /**
   * Pay `amount` of `asset` (ASSETS: "BUCK" and "USDC" in 6-decimal base
   * units, "ETH" in wei) to a wallet id or an address.  BUCKs pass Buck's
   * identity gate -- both sides registered and, between two private wallets,
   * introduced each way; USDC and ETH go to anyone.
   */
  send(fromId, to, amount, asset = "BUCK") {
    return this.#act(async () => {
      const from = this.#wallet(fromId);
      const toAddr = /^0x[0-9a-fA-F]{40}$/.test(to) ? to : this.#wallet(to).address;
      const value = BigInt(amount);
      if (value <= 0n) throw new SandboxError("a payment must be positive");
      const account = this.#account(from.privateKey);
      const tag = `pay:${from.id}->${to}${asset === "BUCK" ? "" : `:${asset}`}`;
      await this.#refusing(() => {
        switch (asset) {
          case "BUCK":
            return this.session.send(this.world.buck, "transfer", [toAddr, value],
              { account, gas: 1_000_000n, tag });
          case "USDC":
            return this.session.send(this.market.usdc, "transfer", [toAddr, value],
              { account, gas: 200_000n, tag });
          case "ETH":
            return sendEth(this.world, account, toAddr, value);
          default:
            throw new SandboxError(`there is no paying in ${asset}`);
        }
      });
    });
  }

  /** Dollars in: mint `usdc` (6 decimals) to a wallet, as a bank transfer would. */
  deposit(walletId, usdc) {
    return this.#act(async () => {
      const w = this.#wallet(walletId);
      const amount = BigInt(usdc);
      if (amount <= 0n) throw new SandboxError("a deposit must be positive");
      await fiatIn(this.world, this.market, w.address, amount, { tag: `deposit:${w.id}` });
    });
  }

  /** Open trading, once per wallet: its identity handshake with the pool
   *  (whose operator can then decrypt who traded with it, as every private
   *  <-> public BUCK transfer requires), and its Permit2 approvals. */
  openTrading(walletId) {
    return this.#act(async () => {
      const w = this.#registered(walletId);
      await this.#refusing(() => openTrading(this.world, this.market, this.#handle(w),
        { rng: this.rng }));
    });
  }

  /**
   * Buy BUCK in the pool: `{usdc}` spends exactly that much, `{buck, maxUsdc}`
   * buys exactly that much.  Returns {paid, received}.
   */
  buy(walletId, order) {
    return this.#act(async () => {
      const w = await this.#trading(walletId);
      const acct = this.#account(w.privateKey);
      const price = await this.#netBuyPrice();
      if (order.buck !== undefined) {
        const wanted = BigInt(order.buck);
        const expected = (wanted * price + BUCK - 1n) / BUCK;
        const maximum = BigInt(order.maxUsdc ?? (expected * 101n + 99n) / 100n);
        return this.#refusing(() => buyBuckExact(this.world, this.market, acct, wanted, maximum));
      }
      const input = BigInt(order.usdc);
      const minimum = input * BUCK * 99n / (price * 100n);
      if (minimum <= 0n) throw new SandboxError("purchase is too small for a positive net BUCK minimum");
      return this.#refusing(() => buyBuck(this.world, this.market, acct, input, { minOut: minimum }));
    });
  }

  /** Sell exactly `buck` BUCK for USDC in the pool.  Returns {paid, received}. */
  sell(walletId, buck) {
    return this.#act(async () => {
      const w = await this.#trading(walletId);
      const input = BigInt(buck);
      const price = await buckPrice(this.world, this.market); // ordinary input is full value
      const minimum = input * price * 99n / (BUCK * 100n);
      if (minimum <= 0n) throw new SandboxError("sale is too small for a positive USDC minimum");
      return this.#refusing(() => sellBuck(this.world, this.market,
        this.#account(w.privateKey), input, { minOut: minimum }));
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
          premiumRate: terms.premiumRate ?? cls.premiumRate,
        }));
      // Insurance already establishes the client/provider relationship. Give
      // the private client's identity receipt to that insurer's actual pool,
      // for both fresh premiums and the original-pool nominal refund.
      await this.#refusing(() => identityApprove(this.world, this.#handle(w),
        { ...ins.handle, account: { address: ins.pool } }, { rng: this.rng }));
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

  /**
   * What activating `amount` would cost and whether it can go ahead now: the
   * premium's principal, what the funding gate requires the wallet to hold
   * first, and the shortfall -- with `buy`, the BUCK to buy to cover it (a
   * little over, for the seconds of drift before the mint).
   */
  quote(walletId, amount, tokenIds) {
    return this.#run(async () => {
      const w = this.#registered(walletId);
      const q = await this.#refusing(() => quoteActivation(this.world, w.address,
        BigInt(amount), tokenIds ? { tokenIds } : {}));
      const buy = q.shortfall === 0n ? 0n : q.shortfall + q.shortfall / 100n + BUCK;
      return { ...q, buy };
    });
  }

  // ---- what the screens show ----------------------------------------------

  /** Everything the tools render, read from the chain in one pass. */
  view() {
    return this.#run(async () => {
      const s = this.session;
      const w = this.world;
      const head = await s.client.getBlock();
      const [buckK, supply, price, pool, insured] = await Promise.all([
        s.call(w.kctrl, "buckK"), s.call(w.buck, "totalSupply"),
        this.#netBuyPrice(), poolReserves(w, this.market),
        Promise.all(this.state.insurers.map((ins) => s.call(w.buck, "balanceOf", [ins.pool])))
          .then((values) => values.reduce((sum, value) => sum + value, 0n)),
      ]);
      const wallets = [];
      for (const x of this.state.wallets) {
        wallets.push({
          id: x.id, label: x.label, credential: x.credential, registered: !!x.handle,
          name: this.#name(x),
          // introduced: the wallets this one has introduced itself to (they know
          // its name); introducedBy: those introduced to it (it knows theirs).
          introduced: this.state.introduced.filter((k) => k.startsWith(`${x.id}>`))
            .map((k) => k.split(">")[1]),
          introducedBy: this.state.introduced.filter((k) => k.endsWith(`>${x.id}`))
            .map((k) => k.split(">")[0]),
          ...await accountView(w, x.address),
          usdc: await s.call(this.market.usdc, "balanceOf", [x.address]),
          trading: x.handle ? await tradingOpen(w, this.market, x.address) : false,
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
          buckPrice: price, pool, insurancePool: insured,
        },
        issuer: { name: ISSUER.name, address: w.issuer.addr },
        credentials: this.state.credentials.map((c) => ({
          id: c.id, name: `${c.card.fields.given_name} ${c.card.fields.family_name}`,
          personNumber: c.card.fields.id_number, issuedAt: c.issuedAt,
          fields: c.card.fields, wallets: [...c.wallets],
        })),
        wallets,
        insurers: this.state.insurers.map(({ id, label, address, pool }) => ({ id, label, address, pool })),
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
    for (const i of this.state.insurers) {
      out[i.address] = i.label;
      out[i.pool] = `${i.label} premium pool`;
    }
    for (const x of this.state.wallets) out[x.address] = this.#title(x);
    const names = this.world.names;
    out[this.world.reg.address] = names.reg;
    out[this.world.credit.address] = names.credit;
    out[this.world.kctrl.address] = "BuckKControllerDirect";
    out[this.world.buck.address] = "Buck";
    out[this.world.poolAcct] = "Insurance pool";
    if (this.world.operator) out[this.world.operator.account.address] = "World operator";
    out[this.session.account.address] = "Deployer (governance, market operator)";
    for (const [addr, c] of Object.entries(this.#known)) out[addr] = c.name;
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
    const rows = await observe(this.world, from, { toBlock: head, extra: this.#known });
    this.state.observed.push(...rows);
    this.state.observedTo = head;
  }

  async #serialize() {
    return encodeJSON({ format: FORMAT, version: VERSION,
                        chain: await snapshotWebevm(this.session), app: this.state });
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
    await restoreWebevm(session, saved.chain);
    const world = await attachNetBuckWorld(session, this.artifacts, saved.app.world,
      { identity: this.identity });
    if ((await session.client.getCode({ address: world.buck.address }) ?? "0x") === "0x") {
      throw new SandboxError("the saved chain holds no Buck contract");
    }
    const market = await attachNetMarket(session, this.artifacts, saved.app.market, world);
    for (const ins of saved.app.insurers) {
      if (this.#account(ins.privateKey).address.toLowerCase() !== ins.address.toLowerCase()) {
        throw new SandboxError("saved insurer key mismatch");
      }
      const pool = await session.call(world.buck, "insurerPool", [ins.address]);
      const binder = await session.call(world.reg, "binderOf", [ins.pool]);
      const code = await session.client.getCode({ address: ins.pool });
      if (pool.toLowerCase() !== ins.pool.toLowerCase() || binder.toLowerCase() !== ins.address.toLowerCase()
          || !code || code === "0x" || keccak256(code) !== ins.poolCodeHash) {
        throw new SandboxError("saved insurer pool mismatch");
      }
    }
    saved.app.journal ??= [];
    this.#attachJournal(session, saved.app.journal);
    this.session = session;
    this.world = world;
    this.market = market;
    this.#known = marketContracts(market, this.artifacts);
    for (const ins of saved.app.insurers) this.#known[ins.pool] = {
      name: "InsurerPremiumPool", role: "premium pool", abi: this.artifacts("SimLP").abi,
    };
    this.state = saved.app;
    this.#accounts.clear();
  }

  // The session journals into `journal` (the world's own array, kept to its
  // tail), numbering on from its last entry.
  #attachJournal(session, journal) {
    const writer = new JournalWriter((line) => {
      const entry = JSON.parse(line);
      journal.push(entry);
      try {
        this.onJournal?.(entry);
      } catch (e) {
        console.error("sandbox onJournal failed", e);
      }
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
      const reason = this.session.lastRevertReason || e.shortMessage || e.message;
      const err = new SandboxError(PROTOCOL_REASONS[reason] ?? reason);
      err.cause = e;
      throw err;
    } finally {
      this.session.lastRevertReason = "";
    }
  }

  async #newInsurer(label) {
    const privateKey = this.newKey();
    const account = this.#account(privateKey);
    await fundAccount(this.world, account.address);
    const id = `I${this.state.insurers.length + 1}`;
    const now = (await this.session.client.getBlock()).timestamp;
    const registered = await onboard(this.world, account, {
      ...ISSUER.stamp, given_name: label, family_name: "Insurer",
      id_number: `SANDBOX-INSURER-${id}`, date_of_birth: "1990-01-01", issued_at: isoOf(now),
    }, { rng: this.rng });
    const pool = await createInsurerPool(this.world, registered);
    const { account: _account, ...handle } = registered;
    const code = await this.session.client.getCode({ address: pool.address });
    const ins = { id, label, privateKey, address: account.address, handle,
      pool: pool.address, poolCodeHash: keccak256(code) };
    this.state.insurers.push(ins);
    this.#known[pool.address] = {
      name: "InsurerPremiumPool", role: "premium pool", abi: this.artifacts("SimLP").abi,
    };
    return ins;
  }

  // Spot purchase cost of one full-value BUCK, before pool fees/impact.
  async #netBuyPrice() {
    const [nominalPrice, [gross, net]] = await Promise.all([
      buckPrice(this.world, this.market),
      this.session.call(this.world.buck, "accountBalancesScaled", [this.market.pool.address]),
    ]);
    if (gross === 0n || net === 0n) throw new SandboxError("pool has no full-value BUCK liquidity");
    return (nominalPrice * gross + net - 1n) / net;
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

  // A registered wallet that has opened trading.
  async #trading(id) {
    const w = this.#registered(id);
    if (!await tradingOpen(this.world, this.market, w.address)) {
      throw new SandboxError(`${this.#title(w)} has not opened trading yet`);
    }
    return w;
  }

  #registered(id) {
    const w = this.#wallet(id);
    if (!w.handle) throw new SandboxError(`${this.#title(w)} is not registered yet`);
    return w;
  }

  // The registered name: the credential's given and family names.
  #name(w) {
    if (!w.credential) return null;
    const f = this.#credential(w.credential).card.fields;
    return `${f.given_name} ${f.family_name}`;
  }

  #title(w) {
    return walletTitle({ id: w.id, label: w.label, name: this.#name(w) });
  }

  // A wallet as the actions return it: no keys.
  #summary(w) {
    return { id: w.id, label: w.label, address: w.address, credential: w.credential,
             name: this.#name(w), title: this.#title(w) };
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
