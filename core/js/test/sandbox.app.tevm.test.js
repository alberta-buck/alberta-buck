// The sandbox CONTROLLER end to end on Tevm, with no DOM
// (doc/review/sandbox-plan.org, S2): the page drives SandboxApp with
// browser-loaded dependencies and IndexedDB; this gate drives the SAME
// class with node-loaded ones and a memory store.
//
// The whole story: issue two credentials, register two wallets, insure a
// home and activate credit, introduce, send (and be refused), advance 30
// days (demurrage accrues, the home depreciates), the observer's rows --
// then the world saved, reopened and imported, equal each time, and
// carrying on.

import { describe, it, before } from "node:test";
import assert from "node:assert/strict";

import { tevmSession } from "../src/backends.js";
import { loadArtifact } from "../src/nodefs.js";

let id = null;
try {
  id = await import("../src/identity.js");
} catch {
  // kernels not built
}
let contracts = true;
try {
  loadArtifact("IdentityRegistry");
} catch {
  contracts = false;
}
const skip = !id
  ? "kernels not built (make nix-core-build-wasm)"
  : !contracts ? "no contracts (make nix-build, or npm ci for alberta-buck-contracts)" : false;

const BUCK = 1_000_000n;

describe("sandbox controller: the whole story, saved and restored", { skip }, () => {
  let S;                  // ../sandbox/src/app.js
  let memoryStore;
  let deps;
  let app;
  let store;
  let changes = 0;

  const head = async (a = app) => (await a.session.client.getBlock()).number;

  before(async () => {
    S = await import("../sandbox/src/app.js");
    ({ memoryStore } = await import("../sandbox/src/store.js"));
    let seed = 0x5a4db0c5n;
    let keys = 0x1000n;
    deps = {
      identity: id.default ?? id,
      artifacts: loadArtifact,
      newSession: () => tevmSession(),
      rng: () => {
        seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
        const v = seed % id.ORDER;
        return v === 0n ? 1n : v;
      },
      newKey: () => "0x" + (keys++).toString(16).padStart(64, "0"),
    };
    store = memoryStore();
    app = await S.SandboxApp.open({ ...deps, store });
    app.onChange(() => { changes += 1; });
  });

  it("opens a fresh world: the stack deployed, Sandbox Mutual open, saved", async () => {
    const v = await app.view();
    assert.equal(v.wallets.length, 0);
    assert.deepEqual(v.insurers.map((i) => i.label), ["Sandbox Mutual"]);
    assert.equal(v.status.day, 0n);
    assert.equal(v.status.buckK, 750_000_000_000_000_000n);
    assert.equal(v.status.supply, 0n);
    assert.ok(v.observed.some((r) => r.fn === "deploy" && r.contract === "Buck"));
    assert.ok(v.journal.length > 0);
    assert.ok(await store.load(), "the new world is saved");
  });

  let chloe;
  let bob;
  it("the issuer certifies two people, off-chain", async () => {
    const before = await head();
    chloe = await app.issue({ given_name: "Chloé", family_name: "Bélanger-李",
                              date_of_birth: "1994-11-02" });
    bob = await app.issue({ given_name: "Bob", family_name: "Tremblay",
                            date_of_birth: "1981-06-09" });
    assert.equal(await head(), before, "issuance sends no transaction");
    assert.match(chloe.card.fields.id_number, /^AB-P-\d{4}-\d{4}$/);
    assert.notEqual(chloe.card.fields.id_number, bob.card.fields.id_number);
    assert.equal(chloe.card.fields.id_type, "Alberta Identity");
    assert.match(chloe.card.fields.issued_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    await assert.rejects(app.issue({ given_name: "X", family_name: "", date_of_birth: "2000-01-01" }),
      (e) => e instanceof S.SandboxError && /family name is required/.test(e.reason));

    // The card travels as JSON; handing the same card back is the same credential.
    const again = await app.importCredential(app.credentialText(bob.id));
    assert.equal(again.id, bob.id);
    await assert.rejects(app.importCredential("{\"x\": 1}"), /not a credential card/);
  });

  let W1;
  let W2;
  let W3;
  it("two wallets register with their credentials; a third stays a stranger", async () => {
    W1 = (await app.createWallet("Chloé's wallet")).id;
    W2 = (await app.createWallet("Bob's wallet")).id;
    W3 = (await app.createWallet("")).id;
    await app.register(W1, chloe.id);
    await app.register(W2, bob.id);
    await assert.rejects(app.register(W1, bob.id), /already registered/);
    const v = await app.view();
    const [w1, w2, w3] = v.wallets;
    assert.equal(w1.registered, true);
    assert.equal(w1.verified, true);
    assert.equal(w2.credential, bob.id);
    assert.equal(w3.label, "Wallet 3");
    assert.equal(w3.verified, false);
    assert.ok(w3.eth > 0n, "every new wallet gets gas money");
    assert.deepEqual(v.credentials.find((c) => c.id === chloe.id).wallets, [W1]);
  });

  let home;
  it("Sandbox Mutual insures Chloé's home on the class defaults; she activates credit", async () => {
    home = await app.insure(W1, { assetClass: "home", face: 400_000n * BUCK });
    const minted = await app.activate(W1, 50_000n * BUCK);
    assert.equal(minted.premium, 0n);
    const v = await app.view();
    const c = v.credits.find((x) => x.tokenId === home);
    assert.equal(c.className, "home");
    assert.equal(c.assetClass, S.assetClass("home").code);
    assert.equal(c.wallet, W1);
    assert.equal(c.insurerId, "I1");
    assert.equal(c.insurer, v.insurers[0].address);
    assert.equal(c.holder, v.wallets[0].address);
    assert.equal(c.floor, 120_000n * BUCK, "the land: 30 % of face");
    assert.equal(c.depRate, 250);
    assert.equal(c.activated, minted.coverage);
    assert.equal(v.wallets[0].creditLimit, minted.newLimit);
    await assert.rejects(app.insure(W3, { assetClass: "gold", face: 1n }), /not registered yet/);
  });

  it("introduced wallets pay each other; the protocol's refusals come back as reasons", async () => {
    await assert.rejects(app.send(W1, W2, 1_234n * BUCK),
      (e) => e instanceof S.SandboxError && /identity-approve/.test(e.reason));
    await app.introduce(W1, W2);
    await app.introduce(W2, W1);                  // already done: no more transactions
    await app.send(W1, W2, 1_234n * BUCK);
    await assert.rejects(app.send(W1, W3, 1n * BUCK), /recipient not verified/);
    const v = await app.view();
    assert.equal(v.wallets[0].signedBalance, -1_234n * BUCK);
    assert.equal(v.wallets[1].balance, 1_234n * BUCK);
    assert.deepEqual(v.wallets[0].introduced, [W2]);
    assert.equal(v.observed.filter((r) => r.fn === "approve").length, 2);
    const refused = v.journal.filter((e) => e.outcome === "revert");
    assert.equal(refused.length, 2);
  });

  it("a premium credit activates on top of existing credit", async () => {
    // Named: left to itself, Buck.mint draws the cheapest credit first (the home's).
    const car = await app.insure(W1, { assetClass: "vehicle", face: 30_000n * BUCK,
                                       premiumRate: 200 });
    const minted = await app.activate(W1, 10_000n * BUCK, [car]);
    assert.ok(minted.premium > 0n, "the premium's principal goes to the pool");
  });

  it("thirty days later: demurrage owing, the home depreciated", async () => {
    const before = await app.view();
    await app.advance(30 * 86_400);
    const v = await app.view();
    assert.equal(v.status.day, 30n);
    assert.ok(v.wallets[1].feeOwing > 0n);
    const was = before.credits.find((x) => x.tokenId === home);
    const now = v.credits.find((x) => x.tokenId === home);
    assert.ok(now.currentValue < was.currentValue);
  });

  it("the observer has every transaction, and no name", async () => {
    const v = await app.view();
    assert.equal(v.observed.at(-1).block, v.status.block - 1n, "the last tx; then the clock block");
    const fns = new Set(v.observed.map((r) => r.fn));
    for (const fn of ["deploy", "trustIssuer", "transfer ETH", "register", "setCreditIssuer",
                      "createCredit", "mint", "approve", "transfer"]) {
      assert.ok(fns.has(fn), fn);
    }
    const { encodeJSON } = await import("../src/codec.js");
    const text = encodeJSON(v.observed);
    for (const s of ["Chloé", "Bélanger", "Tremblay", "1994-11-02", chloe.card.fields.id_number]) {
      assert.ok(!text.includes(s), `observer leaks ${s}`);
    }
    const labels = app.labels();
    assert.equal(labels[v.wallets[0].address], "Chloé's wallet");
    assert.equal(labels[v.insurers[0].address], "Sandbox Mutual");
    assert.ok(changes > 10, "listeners hear every action");
  });

  let exported;
  it("reopening from the store gives back the same world, which carries on", async (t) => {
    const v1 = await app.view();
    const t0 = performance.now();
    const saved = await app.exportText();
    t.diagnostic(`saved world: ${(saved.length / 1024).toFixed(0)} KiB, ` +
                 `${v1.observed.length} observed rows, serialized in ` +
                 `${(performance.now() - t0).toFixed(0)} ms`);
    const app2 = await S.SandboxApp.open({ ...deps, store: memoryStore(await store.load()) });
    assert.equal(app2.notice, "");
    const v2 = await app2.view();
    assert.deepEqual(v2, v1);
    exported = await app2.exportText();

    await app2.send(W2, W1, 100n * BUCK);
    const v3 = await app2.view();
    assert.equal(v3.wallets[1].balance < v2.wallets[1].balance, true);
    assert.equal(v3.observed.length, v2.observed.length + 1);
    assert.equal(v3.journal.at(-1).i, v2.journal.at(-1).i + 1, "the journal numbers on");
  });

  it("import replaces a world; a bad file leaves it untouched", async () => {
    const other = await S.SandboxApp.open({ ...deps, store: memoryStore() });
    const blank = await other.view();
    assert.equal(blank.wallets.length, 0);
    await assert.rejects(other.importText("{}"), /not a sandbox world/);
    await assert.rejects(other.importText("nonsense"), /not JSON/);
    assert.deepEqual(await other.view(), blank);

    await other.importText(exported);
    const v = await other.view();
    assert.equal(v.wallets.length, 3);
    assert.deepEqual(v, await (await S.SandboxApp.open(
      { ...deps, store: memoryStore(exported) })).view());
  });

  it("a saved world that cannot be read starts over, and says so", async () => {
    const broken = await S.SandboxApp.open({ ...deps, store: memoryStore("{\"format\": 1}") });
    assert.match(broken.notice, /could not be restored/);
    assert.equal((await broken.view()).wallets.length, 0);
  });

  it("reset deploys a fresh world", async () => {
    await app.reset();
    const v = await app.view();
    assert.equal(v.wallets.length, 0);
    assert.equal(v.credentials.length, 0);
    assert.equal(v.status.day, 0n);
    assert.ok(!v.observed.some((r) => r.fn === "register"), "a new world's history");
  });
});
